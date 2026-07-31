"""
sharelink.logger
~~~~~~~~~~~~~~~~

Thread-safe, JSON-formatted logging infrastructure for the sharelink package.

All loggers write exclusively to rotating hourly log files.  No output is
emitted to stdout or stderr.  Log files are stored under a configurable
directory (default: ``~/.sharelink/logs``).

Architecture
------------
* :class:`JsonFormatter`   – Formats every :class:`~logging.LogRecord` as a
                             single-line JSON object.
* :class:`LoggingManager`  – Thread-safe singleton that owns logger creation
                             and caches configured loggers so that handlers are
                             never duplicated.
* Module-level helpers     – :func:`get_logger`, :func:`configure_logging`,
                             :func:`get_request_logger`,
                             :func:`get_download_logger`,
                             :func:`get_system_logger`.

Usage
-----
::

    from sharelink.logger import configure_logging, get_system_logger
    from pathlib import Path
    import logging

    # Call once at application start-up, before any logger is first used.
    configure_logging(log_dir=Path("/var/log/sharelink"), log_level=logging.DEBUG)

    log = get_system_logger()
    log.info("Tunnel connected", extra={"tunnel_url": "https://example.trycloudflare.com"})
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Final

__all__: list[str] = [
    "configure_logging",
    "get_logger",
    "get_request_logger",
    "get_download_logger",
    "get_system_logger",
    "JsonFormatter",
    "LoggingManager",
    "REQUEST_LOGGER_NAME",
    "DOWNLOAD_LOGGER_NAME",
    "SYSTEM_LOGGER_NAME",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUEST_LOGGER_NAME: Final[str] = "sharelink.request"
"""Logger name for HTTP request events."""

DOWNLOAD_LOGGER_NAME: Final[str] = "sharelink.download"
"""Logger name for file download lifecycle events."""

SYSTEM_LOGGER_NAME: Final[str] = "sharelink.system"
"""Logger name for infrastructure and tunnel events."""

_ROTATION_WHEN: Final[str] = "h"
"""Rotate log files on an hourly boundary."""

_ROTATION_BACKUP_COUNT: Final[int] = 48
"""Number of rotated log files to retain (48 hours = 2 days of history)."""

_DEFAULT_LOG_DIR: Final[Path] = Path.home() / ".sharelink" / "logs"
"""Default directory where log files are written."""

_DEFAULT_LOG_LEVEL: Final[int] = logging.INFO
"""Default minimum log severity emitted by all sharelink loggers."""

# Attributes that are always present on a standard :class:`logging.LogRecord`.
# Any attribute on a record that is *not* in this set is treated as a
# caller-supplied extra field and included in the JSON payload.
_STANDARD_RECORD_KEYS: Final[frozenset[str]] = frozenset({
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",   # Added in Python 3.12 for asyncio task tracking.
    "thread",
    "threadName",
})


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Formats :class:`logging.LogRecord` instances as single-line JSON objects.

    Each emitted line is a complete, self-contained JSON document suitable for
    ingestion by structured-log pipelines (Elasticsearch, Loki, Datadog, etc.).
    Non-serialisable values are coerced to their ``str()`` representation via
    the ``default`` parameter of :func:`json.dumps`.

    Standard fields emitted on every record
    ----------------------------------------
    ``timestamp``
        ISO 8601 UTC datetime string (e.g. ``"2024-06-01T12:00:00+00:00"``).
    ``level``
        Severity name (e.g. ``"INFO"``, ``"ERROR"``).
    ``logger``
        Fully-qualified logger name (e.g. ``"sharelink.system"``).
    ``message``
        Rendered log message with all format arguments applied.
    ``module``
        Python source module name.
    ``function``
        Source function or method name.
    ``line``
        Source line number.
    ``thread_id``
        OS-level thread identifier.
    ``thread_name``
        Human-readable thread name.

    Optional fields
    ---------------
    ``exc_info``
        Formatted exception traceback string (only present when the record
        carries exception information).
    ``stack_info``
        Formatted stack trace string (only present when stack info was
        requested).
    *extra fields*
        Any key/value pairs supplied via ``extra={...}`` in the logging call
        are merged directly into the top-level JSON object.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Serialise *record* to a JSON line.

        Args:
            record: The :class:`~logging.LogRecord` produced by the logging
                    framework.

        Returns:
            A single-line JSON string with no trailing newline.
        """
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "thread_id": record.thread,
            "thread_name": record.threadName,
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Merge caller-supplied extra fields: any instance attribute not
        # belonging to the standard LogRecord schema and not prefixed with
        # an underscore is considered an intentional structured field.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Thread-safe singleton logging manager
# ---------------------------------------------------------------------------

class LoggingManager:
    """
    Thread-safe singleton that owns the lifecycle of all sharelink loggers.

    Responsibilities
    ----------------
    * Creates the log directory on demand.
    * Attaches a :class:`~logging.handlers.TimedRotatingFileHandler` to each
      named logger exactly once, even under heavy concurrent access.
    * Caches configured loggers so that no duplicate handlers are ever added.
    * Propagation to the root logger is disabled for all managed loggers to
      prevent accidental console output.

    Singleton access
    ----------------
    ::

        manager = LoggingManager.instance()

    Configuration (before any logger is first retrieved)
    -----------------------------------------------------
    ::

        LoggingManager.instance().configure(
            log_dir=Path("/var/log/sharelink"),
            log_level=logging.DEBUG,
        )
    """

    _instance: LoggingManager | None = None
    _class_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "LoggingManager":
        """
        Construct or return the singleton instance using double-checked locking.

        The outer ``if`` avoids acquiring the class-level lock on every call
        once the instance exists.  The inner ``if``, protected by the lock,
        guarantees that only one thread actually constructs the object.
        """
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    instance: LoggingManager = super().__new__(cls)
                    instance._initialise()
                    cls._instance = instance
        return cls._instance

    def _initialise(self) -> None:
        """
        Set up instance-level state.

        Called exactly once, inside the class-level lock, immediately after
        the raw object is allocated by :meth:`__new__`.
        """
        self._logger_lock: threading.Lock = threading.Lock()
        self._configured_names: set[str] = set()
        self._log_dir: Path = _DEFAULT_LOG_DIR
        self._log_level: int = _DEFAULT_LOG_LEVEL

    @classmethod
    def instance(cls) -> "LoggingManager":
        """
        Return the singleton :class:`LoggingManager` for this process.

        Returns:
            The single shared :class:`LoggingManager` instance.
        """
        return cls()

    def configure(
        self,
        log_dir: Path = _DEFAULT_LOG_DIR,
        log_level: int = _DEFAULT_LOG_LEVEL,
    ) -> None:
        """
        Override the default log directory and severity level.

        .. important::
            This method must be called **before** the first invocation of
            :meth:`get_logger`.  Settings applied here have no effect on
            loggers that have already been configured.

        Args:
            log_dir:   Directory in which log files will be written.
                       The directory is created automatically if it does not
                       exist.
            log_level: Minimum severity level to record.  Must be one of the
                       standard :mod:`logging` constants (e.g.
                       :data:`logging.DEBUG`, :data:`logging.INFO`).
        """
        with self._logger_lock:
            self._log_dir = log_dir
            self._log_level = log_level

    def get_logger(self, name: str) -> logging.Logger:
        """
        Return a configured :class:`logging.Logger` for *name*.

        On the first call for a given *name* the logger is configured and
        a :class:`~logging.handlers.TimedRotatingFileHandler` is attached.
        Subsequent calls for the same *name* return the already-configured
        logger without modifying it.

        This method is safe to call concurrently from any number of threads.

        Args:
            name: Dotted logger name (e.g. ``"sharelink.system"``).

        Returns:
            A fully configured :class:`logging.Logger` that writes JSON lines
            to a rotating hourly log file.
        """
        logger: logging.Logger = logging.getLogger(name)

        with self._logger_lock:
            if name not in self._configured_names:
                self._attach_handler(logger, name)
                self._configured_names.add(name)

        return logger

    def _attach_handler(self, logger: logging.Logger, name: str) -> None:
        """
        Configure *logger* with a :class:`~logging.handlers.TimedRotatingFileHandler`.

        The log filename is derived from the last segment of the dotted *name*
        (e.g. ``"sharelink.system"`` → ``system.log``).

        .. note::
            This method **must** be called with ``self._logger_lock`` held.

        Args:
            logger: :class:`logging.Logger` instance to configure in place.
            name:   Dotted logger name used to derive the output filename.
        """
        logger.setLevel(self._log_level)
        # Disable propagation to prevent the root logger from emitting these
        # records to any console handler that may be registered elsewhere.
        logger.propagate = False

        self._log_dir.mkdir(parents=True, exist_ok=True)

        # ``rpartition`` handles both simple names and dotted names cleanly:
        # "sharelink.system" → "system", "system" → "system".
        filename_stem: str = name.rpartition(".")[-1] or name
        log_file: Path = self._log_dir / f"{filename_stem}.log"

        handler = TimedRotatingFileHandler(
            filename=str(log_file),
            when=_ROTATION_WHEN,
            backupCount=_ROTATION_BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
        handler.setFormatter(JsonFormatter())
        handler.setLevel(self._log_level)

        logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Public API — module-level functions
# ---------------------------------------------------------------------------

def configure_logging(
    log_dir: Path | None = None,
    log_level: int = _DEFAULT_LOG_LEVEL,
) -> None:
    """
    Configure global logging parameters for the sharelink package.

    A convenience wrapper around :meth:`LoggingManager.configure`.  Must be
    called **once**, at application start-up, before any sharelink component
    creates a logger.

    Args:
        log_dir:   Directory for log files.  Defaults to
                   ``~/.sharelink/logs``.
        log_level: Minimum severity to record.  Defaults to
                   :data:`logging.INFO`.

    Example
    -------
    ::

        from pathlib import Path
        from sharelink.logger import configure_logging
        import logging

        configure_logging(
            log_dir=Path("/var/log/myapp"),
            log_level=logging.DEBUG,
        )
    """
    resolved_dir: Path = log_dir if log_dir is not None else _DEFAULT_LOG_DIR
    LoggingManager.instance().configure(log_dir=resolved_dir, log_level=log_level)


def get_logger(name: str) -> logging.Logger:
    """
    Retrieve a configured :class:`logging.Logger` by *name*.

    The primary entry-point for obtaining a logger anywhere inside the
    sharelink package.  Delegates to :class:`LoggingManager` which ensures
    that file handlers are registered exactly once per name.

    Args:
        name: Dotted logger name (e.g. ``"sharelink.mymodule"``).

    Returns:
        A thread-safe :class:`logging.Logger` that writes JSON lines to a
        rotating hourly log file.

    Example
    -------
    ::

        from sharelink.logger import get_logger

        log = get_logger("sharelink.server")
        log.warning(
            "Rate limit approached",
            extra={"share_id": "abc123", "downloads": 9, "max_downloads": 10},
        )
    """
    return LoggingManager.instance().get_logger(name)


def get_request_logger() -> logging.Logger:
    """
    Return the dedicated HTTP-request logger (``sharelink.request``).

    Intended for recording inbound HTTP request metadata: method, path,
    client address, ``Content-Range`` headers, response status code, and
    response latency.

    Writes to ``request.log`` in the configured log directory with hourly
    rotation and a 48-file retention window.

    Returns:
        Configured :class:`logging.Logger` for HTTP request events.

    Example
    -------
    ::

        log = get_request_logger()
        log.info(
            "GET /d/abc123",
            extra={
                "method": "GET",
                "path": "/d/abc123",
                "client_ip": "203.0.113.7",
                "status": 206,
                "bytes_sent": 524288,
                "range": "bytes=0-524287",
            },
        )
    """
    return get_logger(REQUEST_LOGGER_NAME)


def get_download_logger() -> logging.Logger:
    """
    Return the dedicated download-event logger (``sharelink.download``).

    Intended for recording file transfer lifecycle events: transfer
    initialisation, byte-range progress, successful completion, and
    per-transfer errors (e.g. client disconnection mid-transfer).

    Writes to ``download.log`` in the configured log directory with hourly
    rotation and a 48-file retention window.

    Returns:
        Configured :class:`logging.Logger` for download lifecycle events.

    Example
    -------
    ::

        log = get_download_logger()
        log.info(
            "Download completed",
            extra={
                "share_id": "abc123",
                "filename": "archive.tar.gz",
                "bytes_total": 104857600,
                "duration_seconds": 12.4,
                "client_ip": "203.0.113.7",
            },
        )
    """
    return get_logger(DOWNLOAD_LOGGER_NAME)


def get_system_logger() -> logging.Logger:
    """
    Return the dedicated system-event logger (``sharelink.system``).

    Intended for recording infrastructure-level events: Cloudflare Tunnel
    connection and reconnection, public URL changes, share creation and
    expiry, and unhandled errors that do not belong to a specific request
    or download.

    Writes to ``system.log`` in the configured log directory with hourly
    rotation and a 48-file retention window.

    Returns:
        Configured :class:`logging.Logger` for system and tunnel events.

    Example
    -------
    ::

        log = get_system_logger()
        log.info(
            "Tunnel URL changed",
            extra={
                "previous_url": "https://old.trycloudflare.com",
                "new_url": "https://new.trycloudflare.com",
                "active_shares": 14,
            },
        )
    """
    return get_logger(SYSTEM_LOGGER_NAME)

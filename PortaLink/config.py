"""
config.py – Central configuration store for the sharelink package.

This module is the single source of truth for every configurable value used
across the package.  All tuneable constants are defined here as module-level
:data:`~typing.Final` values, and the :class:`ShareConfig` dataclass exposes
them as a typed, immutable, validated snapshot that the caller can selectively
override at construction time.

Design decisions
----------------
* :class:`ShareConfig` uses ``frozen=True`` so that attribute re-assignment
  after construction raises :class:`dataclasses.FrozenInstanceError`.
* The ``mime_types`` mapping is normalised to a :class:`types.MappingProxyType`
  inside ``__post_init__`` (via :func:`object.__setattr__`) so that in-place
  mutation of the underlying dict is also prevented, even when the caller
  supplies a plain :class:`dict`.
* Validation is performed eagerly in ``__post_init__`` so that every invalid
  configuration is rejected at the point of construction with a descriptive
  :exc:`ValueError`.
* Module-level constants use underscore-separated thousands (``86_400``) for
  readability without sacrificing machine-parsability.
"""

from __future__ import annotations

import platform
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# Time / transfer defaults
# ---------------------------------------------------------------------------

DEFAULT_EXPIRE_SECONDS: Final[int] = 86_400
"""Share expiry window in seconds.  Defaults to 24 hours (60 × 60 × 24)."""

DEFAULT_MAX_DOWNLOADS: Final[int] = 10
"""Maximum number of completed downloads allowed before a share is
automatically invalidated."""

DEFAULT_CHUNK_SIZE: Final[int] = 65_536
"""Streaming read/write chunk size in bytes (64 KiB).

This value balances per-request memory pressure against the number of
``write()`` / ``read()`` system calls issued per download.  It is also the
granularity at which HTTP Range requests are satisfied during streaming."""

DEFAULT_SESSION_SWEEP_INTERVAL: Final[int] = 60
"""Seconds between successive background sweeps that reap expired or
download-exhausted sessions."""


# ---------------------------------------------------------------------------
# HTTP server defaults
# ---------------------------------------------------------------------------

DEFAULT_HOST: Final[str] = "127.0.0.1"
"""Loopback address on which the embedded HTTP server listens.

Cloudflare Tunnel proxies inbound public traffic to this address; the server
is never exposed directly to the internet."""

DEFAULT_PORT: Final[int] = 8080
"""TCP port on which the embedded HTTP server listens.

Must not conflict with other services on the host.  The Cloudflare Tunnel is
configured to forward traffic to ``DEFAULT_HOST:DEFAULT_PORT``."""

DEFAULT_HTTP_TIMEOUT: Final[float] = 30.0
"""Socket-level timeout in seconds applied to inbound HTTP connections.

Connections that do not produce or consume data within this window are closed
by the server, freeing thread / file-descriptor resources."""


# ---------------------------------------------------------------------------
# Cloudflare Tunnel reconnect defaults
# ---------------------------------------------------------------------------

DEFAULT_RECONNECT_DELAY: Final[float] = 5.0
"""Seconds to wait between consecutive cloudflared reconnect attempts.

A modest delay avoids hammering the Cloudflare edge network during transient
outages while keeping the tunnel recovery window short."""

DEFAULT_RECONNECT_RETRIES: Final[int] = 10
"""Maximum number of consecutive cloudflared reconnect attempts.

Pass ``-1`` to :class:`ShareConfig` to request unlimited retries."""


# ---------------------------------------------------------------------------
# Logging defaults
# ---------------------------------------------------------------------------

DEFAULT_LOG_DIRECTORY: Final[Path] = Path.home() / ".sharelink" / "logs"
"""Directory where :class:`~logging.handlers.TimedRotatingFileHandler` writes
rotating daily log files.  The directory is created automatically by the
logging subsystem on first use."""

DEFAULT_LOG_FILENAME: Final[str] = "sharelink.log"
"""Base filename for the active (current-day) log file.  Rotated files receive
a date suffix appended by :class:`~logging.handlers.TimedRotatingFileHandler`."""

DEFAULT_LOG_ROTATION_WHEN: Final[str] = "midnight"
"""Rotation schedule passed to the ``when`` parameter of
:class:`~logging.handlers.TimedRotatingFileHandler`.  ``"midnight"`` rolls
the file over once per calendar day at 00:00 local time."""

DEFAULT_LOG_BACKUP_COUNT: Final[int] = 7
"""Number of rotated log-file backups retained on disk.  Files older than this
limit are deleted automatically by the handler, providing a rolling 7-day
window."""


# ---------------------------------------------------------------------------
# Cloudflare – binary download URLs  (Linux only)
# ---------------------------------------------------------------------------

_CLOUDFLARED_RELEASE_BASE: Final[str] = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download"
)
"""Base URL for the cloudflared GitHub release artefacts."""

CLOUDFLARED_DOWNLOAD_URLS: Final[dict[str, str]] = {
    "x86_64":  f"{_CLOUDFLARED_RELEASE_BASE}/cloudflared-linux-amd64",
    "aarch64": f"{_CLOUDFLARED_RELEASE_BASE}/cloudflared-linux-arm64",
    "armv7l":  f"{_CLOUDFLARED_RELEASE_BASE}/cloudflared-linux-arm",
}
"""Mapping of Linux machine architecture (as returned by
:func:`platform.machine`) to the corresponding cloudflared static binary
download URL.

Supported architectures:

* ``x86_64``  – AMD/Intel 64-bit (amd64)
* ``aarch64`` – ARM 64-bit (arm64)
* ``armv7l``  – ARM 32-bit hard-float (armv7l)
"""

DEFAULT_CLOUDFLARED_BINARY_PATH: Final[Path] = (
    Path.home() / ".sharelink" / "bin" / "cloudflared"
)
"""Default filesystem path at which the tunnel subsystem caches the
cloudflared executable after downloading it.

The binary is stored under the user's home directory so that no root
privileges are required."""


def resolve_cloudflared_download_url() -> str:
    """Return the cloudflared binary download URL for the current machine.

    The CPU architecture is detected at call-time via :func:`platform.machine`
    and looked up in :data:`CLOUDFLARED_DOWNLOAD_URLS`.

    Returns:
        A fully-qualified HTTPS URL pointing to the cloudflared static binary
        appropriate for the detected architecture.

    Raises:
        RuntimeError: If the current CPU architecture is absent from
            :data:`CLOUDFLARED_DOWNLOAD_URLS`.

    Example::

        from sharelink.config import resolve_cloudflared_download_url

        url = resolve_cloudflared_download_url()
        # https://github.com/cloudflare/cloudflared/releases/latest/download/
        #   cloudflared-linux-amd64   (on x86_64)
    """
    arch: str = platform.machine()
    url: str | None = CLOUDFLARED_DOWNLOAD_URLS.get(arch)
    if url is None:
        supported: str = ", ".join(sorted(CLOUDFLARED_DOWNLOAD_URLS))
        raise RuntimeError(
            f"Unsupported CPU architecture '{arch}'. "
            f"The sharelink package supports: {supported}."
        )
    return url


# ---------------------------------------------------------------------------
# MIME type defaults
# ---------------------------------------------------------------------------

DEFAULT_MIME_FALLBACK: Final[str] = "application/octet-stream"
"""MIME type used when a file extension is absent from :data:`DEFAULT_MIME_TYPES`.

``application/octet-stream`` instructs browsers to treat the body as an
arbitrary binary stream and triggers a save-to-disk prompt rather than
attempting inline rendering."""

_BUILTIN_MIME_TYPES: Final[dict[str, str]] = {
    # ------------------------------------------------------------------ Text
    ".txt":    "text/plain",
    ".log":    "text/plain",
    ".ini":    "text/plain",
    ".cfg":    "text/plain",
    ".conf":   "text/plain",
    ".csv":    "text/csv",
    ".tsv":    "text/tab-separated-values",
    ".html":   "text/html",
    ".htm":    "text/html",
    ".css":    "text/css",
    ".js":     "application/javascript",
    ".mjs":    "application/javascript",
    ".cjs":    "application/javascript",
    ".json":   "application/json",
    ".jsonl":  "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".xml":    "application/xml",
    ".yaml":   "text/yaml",
    ".yml":    "text/yaml",
    ".toml":   "application/toml",
    ".md":     "text/markdown",
    ".rst":    "text/x-rst",
    # ------------------------------------------------------------- Source code
    ".py":     "text/x-python",
    ".sh":     "application/x-sh",
    ".bash":   "application/x-sh",
    ".zsh":    "application/x-sh",
    ".fish":   "application/x-sh",
    # --------------------------------------------------------------- Images
    ".png":    "image/png",
    ".jpg":    "image/jpeg",
    ".jpeg":   "image/jpeg",
    ".gif":    "image/gif",
    ".webp":   "image/webp",
    ".svg":    "image/svg+xml",
    ".ico":    "image/x-icon",
    ".bmp":    "image/bmp",
    ".tiff":   "image/tiff",
    ".tif":    "image/tiff",
    ".avif":   "image/avif",
    ".heic":   "image/heic",
    ".heif":   "image/heif",
    # ----------------------------------------------------------------- Audio
    ".mp3":    "audio/mpeg",
    ".wav":    "audio/wav",
    ".ogg":    "audio/ogg",
    ".oga":    "audio/ogg",
    ".flac":   "audio/flac",
    ".aac":    "audio/aac",
    ".m4a":    "audio/mp4",
    ".opus":   "audio/opus",
    ".aiff":   "audio/aiff",
    ".weba":   "audio/webm",
    # ----------------------------------------------------------------- Video
    ".mp4":    "video/mp4",
    ".m4v":    "video/mp4",
    ".webm":   "video/webm",
    ".avi":    "video/x-msvideo",
    ".mkv":    "video/x-matroska",
    ".mov":    "video/quicktime",
    ".wmv":    "video/x-ms-wmv",
    ".flv":    "video/x-flv",
    ".ogv":    "video/ogg",
    ".ts":     "video/mp2t",
    ".3gp":    "video/3gpp",
    ".3g2":    "video/3gpp2",
    # --------------------------------------------------------------- Documents
    ".pdf":    "application/pdf",
    ".rtf":    "application/rtf",
    ".docx":   (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
    ".xlsx":   (
        "application/vnd.openxmlformats-officedocument"
        ".spreadsheetml.sheet"
    ),
    ".pptx":   (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ),
    ".odt":    "application/vnd.oasis.opendocument.text",
    ".ods":    "application/vnd.oasis.opendocument.spreadsheet",
    ".odp":    "application/vnd.oasis.opendocument.presentation",
    ".epub":   "application/epub+zip",
    # -------------------------------------------------- Archives / packages
    ".zip":    "application/zip",
    ".tar":    "application/x-tar",
    ".gz":     "application/gzip",
    ".tgz":    "application/gzip",
    ".bz2":    "application/x-bzip2",
    ".tbz2":   "application/x-bzip2",
    ".xz":     "application/x-xz",
    ".txz":    "application/x-xz",
    ".7z":     "application/x-7z-compressed",
    ".rar":    "application/x-rar-compressed",
    ".zst":    "application/zstd",
    ".tar.gz": "application/gzip",
    ".tar.xz": "application/x-xz",
    ".deb":    "application/vnd.debian.binary-package",
    ".rpm":    "application/x-rpm",
    ".apk":    "application/vnd.android.package-archive",
    ".iso":    "application/x-iso9660-image",
    # ----------------------------------------------------------------- Fonts
    ".ttf":    "font/ttf",
    ".otf":    "font/otf",
    ".woff":   "font/woff",
    ".woff2":  "font/woff2",
    # --------------------------------------------- Data / structured formats
    ".sqlite":   "application/x-sqlite3",
    ".sqlite3":  "application/x-sqlite3",
    ".db":       "application/octet-stream",
    ".parquet":  "application/vnd.apache.parquet",
    ".arrow":    "application/vnd.apache.arrow.file",
    ".feather":  "application/vnd.apache.arrow.file",
    # --------------------------------------------------- Miscellaneous binary
    ".bin":    "application/octet-stream",
    ".dat":    "application/octet-stream",
    ".exe":    "application/x-msdownload",
    ".dmg":    "application/x-apple-diskimage",
    ".wasm":   "application/wasm",
}

DEFAULT_MIME_TYPES: Final[types.MappingProxyType[str, str]] = (
    types.MappingProxyType(_BUILTIN_MIME_TYPES)
)
"""Immutable mapping of lower-cased file extension → MIME content-type string.

The HTTP server uses this table to populate the ``Content-Type`` response
header.  Extensions absent from this table fall back to
:data:`DEFAULT_MIME_FALLBACK`.

The mapping is a :class:`types.MappingProxyType` so that it cannot be mutated
at runtime.  Callers that need additional entries should supply a merged
:class:`dict` to :class:`ShareConfig`; it will be wrapped automatically."""


# ---------------------------------------------------------------------------
# ShareConfig – primary public interface of this module
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShareConfig:
    """Immutable configuration snapshot for a ShareManager instance.

    Every attribute mirrors a module-level ``DEFAULT_*`` constant and may be
    overridden at construction time.  The object is frozen after construction:
    re-assigning any attribute raises :class:`dataclasses.FrozenInstanceError`.
    The ``mime_types`` mapping is additionally protected by a
    :class:`types.MappingProxyType` wrapper applied inside ``__post_init__``,
    preventing in-place mutation of the underlying dictionary even when the
    caller supplies a plain :class:`dict`.

    All values are validated eagerly in ``__post_init__``; any out-of-range or
    logically inconsistent value raises a :exc:`ValueError` at construction
    time rather than producing silent misbehaviour later.

    Example::

        from sharelink.config import ShareConfig
        from pathlib import Path

        cfg = ShareConfig(
            port=9000,
            expire_seconds=3_600,   # 1 hour
            max_downloads=5,
            log_directory=Path("/var/log/sharelink"),
        )

    Attributes:
        host:
            Loopback address for the embedded HTTP server.
        port:
            TCP port for the embedded HTTP server.  Must be in ``[1, 65535]``.
        expire_seconds:
            Seconds after creation until a share expires automatically.
            Must be a positive integer.
        max_downloads:
            Maximum number of completed downloads before a share is
            invalidated.  Must be a positive integer.
        chunk_size:
            Streaming read/write chunk size in bytes.  Must be a positive
            integer.  Directly controls per-request memory usage.
        http_timeout:
            Socket-level timeout in seconds for inbound HTTP connections.
            Must be a positive number.
        session_sweep_interval:
            Seconds between background sweeps that clean up expired or
            exhausted sessions.  Must be a positive integer.
        reconnect_delay:
            Seconds between consecutive cloudflared reconnect attempts.
            Must be a non-negative number.
        reconnect_retries:
            Maximum consecutive reconnect attempts.  Pass ``-1`` for
            unlimited retries.  Must be ``-1`` or a non-negative integer.
        log_directory:
            Directory for rotating log files.  Created automatically if it
            does not exist.
        log_filename:
            Base filename for the rotating log file.
        log_rotation_when:
            Rotation schedule for
            :class:`~logging.handlers.TimedRotatingFileHandler`.
            Valid values match the ``when`` parameter of that class
            (e.g. ``"midnight"``, ``"D"``, ``"H"``).
        log_backup_count:
            Number of rotated log-file backups retained on disk.
            Must be a non-negative integer.
        cloudflared_binary_path:
            Filesystem path to the cloudflared executable.  The tunnel
            subsystem downloads and caches the binary here on first run.
        mime_types:
            Extension-to-MIME-type look-up table used by the HTTP server.
            May be supplied as a plain :class:`dict`; it will be converted
            to an immutable :class:`types.MappingProxyType` automatically.
        mime_fallback:
            MIME type emitted for extensions absent from ``mime_types``.
    """

    # --- HTTP server -------------------------------------------------------
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    # --- Share lifetime ----------------------------------------------------
    expire_seconds: int = DEFAULT_EXPIRE_SECONDS
    max_downloads: int = DEFAULT_MAX_DOWNLOADS

    # --- Data transfer -----------------------------------------------------
    chunk_size: int = DEFAULT_CHUNK_SIZE
    http_timeout: float = DEFAULT_HTTP_TIMEOUT

    # --- Session housekeeping ----------------------------------------------
    session_sweep_interval: int = DEFAULT_SESSION_SWEEP_INTERVAL

    # --- Tunnel reconnect --------------------------------------------------
    reconnect_delay: float = DEFAULT_RECONNECT_DELAY
    reconnect_retries: int = DEFAULT_RECONNECT_RETRIES

    # --- Logging -----------------------------------------------------------
    log_directory: Path = field(
        default_factory=lambda: DEFAULT_LOG_DIRECTORY,
    )
    log_filename: str = DEFAULT_LOG_FILENAME
    log_rotation_when: str = DEFAULT_LOG_ROTATION_WHEN
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    # --- Cloudflare --------------------------------------------------------
    cloudflared_binary_path: Path = field(
        default_factory=lambda: DEFAULT_CLOUDFLARED_BINARY_PATH,
    )

    # --- MIME --------------------------------------------------------------
    mime_types: Mapping[str, str] = field(
        default_factory=lambda: DEFAULT_MIME_TYPES,
    )
    mime_fallback: str = DEFAULT_MIME_FALLBACK

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        """Validate all configuration values and enforce immutability invariants.

        Called automatically by the dataclass machinery immediately after
        ``__init__``.  Uses :func:`object.__setattr__` where necessary to
        mutate frozen fields (specifically to wrap ``mime_types`` in a
        :class:`types.MappingProxyType`).

        Raises:
            ValueError: If any attribute value is outside its permitted range
                or violates a type / logical constraint.
        """
        # --- host ----------------------------------------------------------
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError(
                f"host must be a non-empty string, got {self.host!r}."
            )

        # --- port ----------------------------------------------------------
        if not (1 <= self.port <= 65_535):
            raise ValueError(
                f"port must be in [1, 65535], got {self.port}."
            )

        # --- expire_seconds ------------------------------------------------
        if self.expire_seconds <= 0:
            raise ValueError(
                f"expire_seconds must be a positive integer, "
                f"got {self.expire_seconds}."
            )

        # --- max_downloads -------------------------------------------------
        if self.max_downloads <= 0:
            raise ValueError(
                f"max_downloads must be a positive integer, "
                f"got {self.max_downloads}."
            )

        # --- chunk_size ----------------------------------------------------
        if self.chunk_size <= 0:
            raise ValueError(
                f"chunk_size must be a positive integer, got {self.chunk_size}."
            )

        # --- http_timeout --------------------------------------------------
        if self.http_timeout <= 0.0:
            raise ValueError(
                f"http_timeout must be a positive number, "
                f"got {self.http_timeout}."
            )

        # --- session_sweep_interval ----------------------------------------
        if self.session_sweep_interval <= 0:
            raise ValueError(
                f"session_sweep_interval must be a positive integer, "
                f"got {self.session_sweep_interval}."
            )

        # --- reconnect_delay -----------------------------------------------
        if self.reconnect_delay < 0.0:
            raise ValueError(
                f"reconnect_delay must be non-negative, "
                f"got {self.reconnect_delay}."
            )

        # --- reconnect_retries ---------------------------------------------
        if self.reconnect_retries < -1:
            raise ValueError(
                f"reconnect_retries must be -1 (unlimited) or a non-negative "
                f"integer, got {self.reconnect_retries}."
            )

        # --- log_backup_count ----------------------------------------------
        if self.log_backup_count < 0:
            raise ValueError(
                f"log_backup_count must be non-negative, "
                f"got {self.log_backup_count}."
            )

        # --- log_rotation_when ---------------------------------------------
        _valid_when: frozenset[str] = frozenset({
            "S", "M", "H", "D", "W0", "W1", "W2", "W3", "W4", "W5", "W6",
            "midnight",
        })
        if self.log_rotation_when not in _valid_when:
            raise ValueError(
                f"log_rotation_when must be one of {sorted(_valid_when)}, "
                f"got {self.log_rotation_when!r}."
            )

        # --- mime_fallback -------------------------------------------------
        if not isinstance(self.mime_fallback, str) or not self.mime_fallback.strip():
            raise ValueError(
                f"mime_fallback must be a non-empty string, "
                f"got {self.mime_fallback!r}."
            )

        # --- mime_types – enforce immutability -----------------------------
        # If the caller supplied a plain dict (or any non-proxy mapping),
        # wrap it so that in-place mutation is prevented for the lifetime of
        # this config object.  object.__setattr__ bypasses the frozen guard
        # and is the prescribed pattern for this exact use-case.
        if not isinstance(self.mime_types, types.MappingProxyType):
            object.__setattr__(
                self,
                "mime_types",
                types.MappingProxyType(dict(self.mime_types)),
            )

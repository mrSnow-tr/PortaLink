"""
sharelink.api
~~~~~~~~~~~~~

Public interface for the sharelink package.

This is the only module that external applications should import.  All
internal implementation details — HTTP server management, Cloudflare Tunnel
integration, session tracking, download streaming, and share lifecycle logic
— are hidden behind the stable, type-safe abstractions defined here.

Quick start
-----------
::

    from sharelink import ShareManager

    # Context manager ensures clean shutdown on exit
    with ShareManager() as manager:

        # Share a local file
        share = manager.create_share("/home/user/dataset.tar.gz")
        print("Download URL:", share.public_url)

        # Share a local directory (served as a ZIP archive)
        dir_share = manager.create_share("/data/reports/q3/")
        print("Directory URL:", dir_share.public_url)

        # Share an FTP resource via URL
        ftp_share = manager.create_share(
            "ftp://user:password@ftp.example.com/pub/data.csv"
        )
        print("FTP URL:", ftp_share.public_url)

        # Inspect all active shares
        for s in manager.list_shares():
            print(s.share_id, s.state.name, s.public_url)

        # Revoke a specific share early
        manager.delete_share(share.share_id)

Custom configuration::

    from sharelink import ShareConfig, ShareManager

    config = ShareConfig(
        expire_seconds=3_600,   # 1 hour
        max_downloads=5,
    )
    with ShareManager(config=config) as manager:
        share = manager.create_share("/tmp/report.pdf")
        print(share.public_url)

Custom logging (must be called before creating a ShareManager)::

    import logging
    from pathlib import Path
    from sharelink import configure_logging, ShareManager

    configure_logging(
        log_dir=Path("/var/log/myapp/sharelink"),
        log_level=logging.DEBUG,
    )
    with ShareManager() as manager:
        share = manager.create_share("/tmp/data.bin")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ShareConfig
from .logger import configure_logging as _configure_logging
from .manager import ShareManager as _InternalShareManager
from .models import ShareState, SourceType
from .session import ShareSession as _ShareSession

__all__: list[str] = [
    "Share",
    "ShareConfig",
    "ShareManager",
    "ShareState",
    "ShareStatistics",
    "SourceType",
    "configure_logging",
]


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShareStatistics:
    """Immutable snapshot of download metrics for a single share.

    All fields reflect the state of the share at the instant the snapshot was
    taken.  Retrieve via :attr:`Share.statistics`.

    Attributes
    ----------
    total_downloads:
        Cumulative count of all download sessions initiated, including
        incomplete and interrupted transfers.
    completed_downloads:
        Count of sessions that delivered all requested bytes without
        interruption.
    active_downloads:
        Count of sessions currently streaming data to clients.
    total_bytes_transferred:
        Total bytes delivered to all clients across every session,
        including partial and interrupted transfers.
    unique_ips:
        Frozen set of distinct client IP addresses that have accessed this
        share.
    first_accessed:
        Timezone-aware UTC datetime of the first download session, or
        ``None`` if the share has never been accessed.
    last_accessed:
        Timezone-aware UTC datetime of the most recent download session, or
        ``None`` if the share has never been accessed.
    """

    total_downloads: int
    completed_downloads: int
    active_downloads: int
    total_bytes_transferred: int
    unique_ips: frozenset[str]
    first_accessed: datetime | None
    last_accessed: datetime | None


# ---------------------------------------------------------------------------
# Private module-level helpers
# ---------------------------------------------------------------------------


def _parse_optional_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string produced by :meth:`datetime.isoformat`.

    Accepts the strings emitted by :meth:`session.ShareSession.to_dict` and
    coerces naive datetimes to UTC.

    Parameters
    ----------
    value:
        An ISO 8601 datetime string, or ``None``.

    Returns
    -------
    datetime | None
        A timezone-aware UTC :class:`~datetime.datetime`, or ``None`` when
        *value* is ``None`` or cannot be parsed.
    """
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _build_statistics(session: _ShareSession) -> ShareStatistics:
    """Build a :class:`ShareStatistics` snapshot from a session's serialised dict.

    Uses :meth:`~session.ShareSession.to_dict` so that all counter fields are
    captured under the session's internal lock, guaranteeing a consistent
    point-in-time view.

    Parameters
    ----------
    session:
        The live session from which to extract statistics.

    Returns
    -------
    ShareStatistics
        An immutable statistics snapshot.
    """
    raw: dict[str, Any] = session.to_dict()["statistics"]
    return ShareStatistics(
        total_downloads=int(raw["total_downloads"]),
        completed_downloads=int(raw["completed_downloads"]),
        active_downloads=int(raw["active_downloads"]),
        total_bytes_transferred=int(raw["total_bytes_transferred"]),
        unique_ips=frozenset(raw["unique_ips"]),
        first_accessed=_parse_optional_datetime(raw.get("first_accessed")),
        last_accessed=_parse_optional_datetime(raw.get("last_accessed")),
    )


# ---------------------------------------------------------------------------
# Public share handle
# ---------------------------------------------------------------------------


class Share:
    """Live, read-only handle for a single active file share.

    Returned by :meth:`ShareManager.create_share`,
    :meth:`ShareManager.get_share`, and :meth:`ShareManager.list_shares`.
    Every property read is thread-safe and reflects the current state of the
    underlying session without exposing any internal implementation type.

    The :attr:`public_url` property is updated automatically whenever the
    Cloudflare Tunnel reconnects with a new hostname.  The download path
    segment and share identifier remain stable across reconnections.  Callers
    that need to block until the first URL is available should use
    :meth:`wait_for_url`.

    Identity
    --------
    Two ``Share`` objects wrapping the same underlying share compare equal
    (via :meth:`__eq__`) and produce the same hash, so they can be stored in
    sets and used as dictionary keys.
    """

    __slots__ = ("_session",)

    def __init__(self, session: _ShareSession) -> None:
        self._session: _ShareSession = session

    # ------------------------------------------------------------------
    # Immutable identity and metadata
    # ------------------------------------------------------------------

    @property
    def share_id(self) -> str:
        """Unique alphanumeric identifier embedded in the public download URL.

        Stable for the full lifetime of this share.
        """
        return self._session.share_id

    @property
    def source_path(self) -> str:
        """Absolute local filesystem path, or remote FTP path, for this share.

        For :attr:`~models.SourceType.LOCAL_FILE` and
        :attr:`~models.SourceType.LOCAL_DIRECTORY` sources this is an absolute
        POSIX path on the local machine.  For :attr:`~models.SourceType.FTP`
        sources this is the absolute path on the remote FTP server.
        """
        return self._session.share_info.source_path

    @property
    def source_type(self) -> SourceType:
        """Classification of the shared resource.

        One of :attr:`~models.SourceType.LOCAL_FILE`,
        :attr:`~models.SourceType.LOCAL_DIRECTORY`, or
        :attr:`~models.SourceType.FTP`.
        """
        return self._session.share_info.source_type

    @property
    def display_name(self) -> str | None:
        """Explicit filename override supplied at creation time, or ``None``.

        When set, this value is used verbatim in the ``Content-Disposition``
        response header instead of the basename derived from :attr:`source_path`.
        """
        return self._session.share_info.display_name

    @property
    def file_size(self) -> int | None:
        """Size of the resource in bytes, or ``None`` when not known at creation.

        ``None`` is returned for directory shares (the ZIP archive size is
        unknown until generation completes) and for FTP shares where the size
        must be queried from the remote server on demand.
        """
        return self._session.share_info.file_size

    @property
    def created_at(self) -> datetime:
        """Timezone-aware UTC datetime when this share was created."""
        return self._session.share_info.created_at

    @property
    def expires_at(self) -> datetime:
        """Timezone-aware UTC datetime after which the share becomes inaccessible.

        Once this moment is passed, all download attempts receive ``410 Gone``.
        """
        return self._session.share_info.expires_at

    @property
    def max_downloads(self) -> int:
        """Maximum number of download sessions permitted before exhaustion."""
        return self._session.share_info.max_downloads

    # ------------------------------------------------------------------
    # Derived metadata  (resolved under the session lock via to_dict)
    # ------------------------------------------------------------------

    @property
    def filename(self) -> str:
        """Suggested download filename sent in ``Content-Disposition`` headers.

        Resolution order:

        1. :attr:`display_name` when explicitly set at creation time.
        2. ``<directory_name>.zip`` for directory shares.
        3. Basename of :attr:`source_path` for local-file and FTP shares.
        4. ``"download"`` as an absolute fallback.
        """
        return str(self._session.to_dict()["filename"])

    @property
    def content_type(self) -> str:
        """MIME type string used in ``Content-Type`` response headers.

        Resolution order:

        1. Explicit override supplied at creation time.
        2. ``"application/zip"`` for directory shares.
        3. MIME type inferred from the file extension.
        4. ``"application/octet-stream"`` as the universal fallback.
        """
        return str(self._session.to_dict()["content_type"])

    # ------------------------------------------------------------------
    # Live state  (may change during the share's lifetime)
    # ------------------------------------------------------------------

    @property
    def state(self) -> ShareState:
        """Current lifecycle state of the share.

        Allowed transitions::

            PENDING → ACTIVE → EXPIRED
                              → EXHAUSTED
                              → REVOKED
            PENDING → REVOKED

        ``EXPIRED``, ``EXHAUSTED``, and ``REVOKED`` are terminal; no further
        transitions occur once a terminal state is reached.
        """
        return self._session.state

    @property
    def public_url(self) -> str:
        """Full HTTPS download URL for this share.

        Returns an empty string until the Cloudflare Tunnel establishes its
        first connection.  Updated automatically when the tunnel reconnects
        with a new hostname; the path segment and share identifier are stable
        across reconnections.

        Use :meth:`wait_for_url` to block until the URL becomes available.
        """
        return self._session.public_url

    @property
    def is_active(self) -> bool:
        """``True`` while the share is in ``ACTIVE`` state and accepting downloads."""
        return self._session.is_active

    @property
    def is_expired(self) -> bool:
        """``True`` when the share has passed its expiry time or entered ``EXPIRED`` state."""
        return self._session.is_expired

    @property
    def is_exhausted(self) -> bool:
        """``True`` when the share has reached its maximum allowed download count."""
        return self._session.is_exhausted

    @property
    def downloads_remaining(self) -> int:
        """Approximate number of download sessions still permitted.

        Computed from live counters; the value may be marginally stale under
        extreme concurrency but is accurate for all practical display and
        monitoring purposes.
        """
        return self._session.share_info.downloads_remaining

    @property
    def statistics(self) -> ShareStatistics:
        """Immutable snapshot of download metrics for this share.

        All counter fields and timestamps are captured under a thread-safe lock
        to guarantee a consistent point-in-time view.
        """
        return _build_statistics(self._session)

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def wait_for_url(self, timeout: float | None = None) -> str | None:
        """Block until the Cloudflare Tunnel announces a public URL.

        Returns immediately when a URL is already available.  This method is
        typically called when :meth:`ShareManager.create_share` was invoked
        with ``wait_for_url=False``.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` waits indefinitely.

        Returns
        -------
        str | None
            The public ``https://`` download URL once the tunnel connects, or
            ``None`` when *timeout* elapsed before a URL was received.
        """
        return self._session.wait_for_url(timeout=timeout)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of this share's complete state.

        All mutable fields are captured under a thread-safe lock, guaranteeing
        a consistent point-in-time view.  :class:`~datetime.datetime` values
        are encoded as ISO 8601 strings; the ``unique_ips`` collection is
        serialised as a sorted list.

        Returns
        -------
        dict[str, Any]
            A flat or nested dictionary suitable for direct JSON serialisation.
        """
        return self._session.to_dict()

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Share):
            return self.share_id == other.share_id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.share_id)

    def __repr__(self) -> str:
        return (
            f"<Share"
            f" id={self.share_id!r}"
            f" state={self.state.name}"
            f" url={self.public_url!r}"
            f">"
        )


# ---------------------------------------------------------------------------
# Public share manager
# ---------------------------------------------------------------------------


class ShareManager:
    """Central lifecycle coordinator for file shares.

    The primary public entry point for the sharelink package.  Creates,
    queries, and revokes shares; maintains a background session sweeper that
    automatically expires time-exceeded shares and evicts terminal sessions
    to release HTTP server sockets and Cloudflare Tunnel processes promptly.

    The sweeper starts automatically on construction; no explicit ``start()``
    call is required.  Call :meth:`shutdown` (or use the context manager
    protocol) to perform a clean shutdown that stops all active sessions.

    Thread safety
    -------------
    All public methods are safe to call concurrently from any number of threads.

    Parameters
    ----------
    config:
        Package configuration snapshot.  A default :class:`ShareConfig` is
        used when *None*, giving 24-hour expiry and a 10-download limit per
        share.

    Examples
    --------
    Minimal usage::

        manager = ShareManager()
        share = manager.create_share("/data/file.bin")
        print(share.public_url)
        manager.shutdown()

    Recommended pattern — context manager::

        with ShareManager() as manager:
            share = manager.create_share("/tmp/archive.tar.gz")
            print(share.public_url)

    Custom configuration::

        config = ShareConfig(expire_seconds=3_600, max_downloads=3)
        with ShareManager(config=config) as manager:
            share = manager.create_share("/tmp/report.pdf")
    """

    def __init__(self, config: ShareConfig | None = None) -> None:
        self._manager: _InternalShareManager = _InternalShareManager(config=config)

    # ------------------------------------------------------------------
    # Share creation
    # ------------------------------------------------------------------

    def create_share(
        self,
        source: str | Path,
        *,
        expire_seconds: int | None = None,
        max_downloads: int | None = None,
        display_name: str | None = None,
        content_type: str | None = None,
        ftp_host: str | None = None,
        ftp_port: int | None = None,
        ftp_username: str | None = None,
        ftp_password: str | None = None,
        ftp_passive: bool = True,
        wait_for_url: bool = True,
        url_timeout: float = 30.0,
    ) -> Share:
        """Create, register, and start a new file share.

        Resolves the source type, binds an embedded HTTP server on an ephemeral
        port, and launches the Cloudflare Tunnel process.  When *wait_for_url*
        is ``True`` (the default), this method blocks until the tunnel announces
        a public URL or *url_timeout* seconds elapse.

        Source resolution
        -----------------
        Three paths are evaluated in order:

        1. **FTP URL** — ``ftp://`` scheme detected; the URL is parsed for
           host, port, credentials, and remote path.  Keyword argument overrides
           take precedence over URL-parsed values.
        2. **Explicit FTP path** — *ftp_host* is set but *source* carries no
           ``ftp://`` scheme.  *source* is used as the absolute remote path on
           the FTP server.
        3. **Local filesystem** — the path is resolved via
           :meth:`pathlib.Path.resolve`.  Regular files have their byte size
           pre-fetched.  Directories are served as on-the-fly ZIP archives
           whose total size is unknown until generation completes.

        Parameters
        ----------
        source:
            Local file or directory path, or an FTP URL in the form
            ``ftp://[user[:pass]@]host[:port]/path``.
        expire_seconds:
            Seconds until the share expires automatically.  Must be a positive
            integer.  Defaults to the manager's configured value (86 400 s /
            24 hours by default).
        max_downloads:
            Maximum completed downloads before the share is invalidated.  Must
            be a positive integer.  Defaults to the manager's configured value
            (10 by default).
        display_name:
            Override for the ``Content-Disposition`` filename header.  When
            ``None``, the source basename is used.
        content_type:
            Explicit MIME type for the ``Content-Type`` header.  When ``None``,
            the type is guessed from the file extension.
        ftp_host:
            FTP server hostname or IP address.  Required when *source* is an
            FTP path without the ``ftp://`` scheme.
        ftp_port:
            FTP TCP port.  Defaults to ``21`` in the download layer when ``None``.
        ftp_username:
            FTP authentication username.  Defaults to ``"anonymous"`` when ``None``.
        ftp_password:
            FTP authentication password.  Defaults to ``"anonymous@"`` when ``None``.
        ftp_passive:
            ``True`` (default) to use PASV mode; ``False`` for active (PORT)
            mode.  Passive mode is strongly recommended when behind NAT or a
            firewall.
        wait_for_url:
            When ``True`` (default), block until the tunnel URL is available or
            *url_timeout* seconds elapse.  A warning is logged on timeout but
            no exception is raised; the tunnel continues connecting in the
            background and :meth:`Share.wait_for_url` can be called on the
            returned object to block further.
        url_timeout:
            Maximum seconds to wait for the tunnel URL when *wait_for_url* is
            ``True``.

        Returns
        -------
        Share
            A live :class:`Share` handle whose HTTP server socket is bound and
            whose Cloudflare Tunnel monitor thread is running.

        Raises
        ------
        FileNotFoundError
            When *source* is a local path that does not exist on disk.
        ValueError
            When the resolved local path is neither a regular file nor a
            directory, or when an FTP URL uses an unsupported scheme.
        OSError
            When the embedded HTTP server cannot bind to an available port.
        RuntimeError
            When the Cloudflare Tunnel cannot be started because the current
            CPU architecture is unsupported and no cached binary exists.
        urllib.error.URLError
            When the cloudflared binary must be downloaded and the HTTP request
            to GitHub Releases fails.
        """
        session: _ShareSession = self._manager.create_share(
            source,
            expire_seconds=expire_seconds,
            max_downloads=max_downloads,
            display_name=display_name,
            content_type=content_type,
            ftp_host=ftp_host,
            ftp_port=ftp_port,
            ftp_username=ftp_username,
            ftp_password=ftp_password,
            ftp_passive=ftp_passive,
            wait_for_url=wait_for_url,
            url_timeout=url_timeout,
        )
        return Share(session)

    # ------------------------------------------------------------------
    # Registry queries
    # ------------------------------------------------------------------

    def get_share(self, share_id: str) -> Share | None:
        """Return the live share handle for *share_id*, or ``None``.

        Parameters
        ----------
        share_id:
            Unique identifier of the share to look up.

        Returns
        -------
        Share | None
            The live :class:`Share` handle, or ``None`` when *share_id* is
            absent from the registry.
        """
        session: _ShareSession | None = self._manager.get_share(share_id)
        if session is None:
            return None
        return Share(session)

    def list_shares(self) -> list[Share]:
        """Return a snapshot of all sessions currently in the registry.

        Sessions in terminal states that have not yet been evicted by the
        background sweeper — notably ``EXHAUSTED`` sessions still serving an
        active download — may be included.

        Returns
        -------
        list[Share]
            Zero or more :class:`Share` handles in an unspecified order.  The
            returned list is a copy; modifying it does not affect the registry.
        """
        return [Share(s) for s in self._manager.list_shares()]

    # ------------------------------------------------------------------
    # Share deletion
    # ------------------------------------------------------------------

    def delete_share(self, share_id: str) -> bool:
        """Revoke a share immediately and remove it from the registry.

        Transitions the share to ``REVOKED`` state, stops its embedded HTTP
        server, and terminates its Cloudflare Tunnel process.  Safe to call
        from any thread.

        Parameters
        ----------
        share_id:
            Unique identifier of the share to revoke.

        Returns
        -------
        bool
            ``True`` when the share was found and revoked; ``False`` when
            *share_id* is absent from the registry.
        """
        return self._manager.delete_share(share_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop all active shares and shut down the background session sweeper.

        Signals the sweeper thread to exit, stops every registered session
        (releasing HTTP server sockets and Cloudflare Tunnel processes), and
        blocks until the sweeper thread exits or its join timeout elapses.

        This method is idempotent: calling it on an already-stopped manager
        is safe and has no additional effect.
        """
        self._manager.stop()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ShareManager":
        """Return *self*; the background sweeper is already running after construction."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Call :meth:`shutdown` on context exit, regardless of any exception."""
        self.shutdown()

    # ------------------------------------------------------------------
    # Monitoring properties
    # ------------------------------------------------------------------

    @property
    def active_share_count(self) -> int:
        """Number of sessions currently in ``ACTIVE`` state."""
        return self._manager.active_share_count

    @property
    def total_share_count(self) -> int:
        """Total sessions in the registry, including terminal sessions not yet
        evicted by the background sweeper."""
        return self._manager.total_share_count

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<ShareManager"
            f" total={self.total_share_count}"
            f" active={self.active_share_count}"
            f">"
        )


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def configure_logging(
    log_dir: Path | None = None,
    log_level: int = logging.INFO,
) -> None:
    """Configure the sharelink package logging system.

    Must be called **before** the first :class:`ShareManager` is instantiated
    so that all subsequently created loggers inherit the configured directory
    and severity level.  Calling it after loggers have already been created has
    no effect on those loggers.

    Sharelink writes structured JSON log lines exclusively to rotating hourly
    files inside *log_dir*.  No output is ever written to ``stdout`` or
    ``stderr``.

    Three log files are maintained:

    * ``request.log``  — inbound HTTP request metadata.
    * ``download.log`` — file transfer lifecycle events.
    * ``system.log``   — tunnel, session, and infrastructure events.

    Parameters
    ----------
    log_dir:
        Directory for log files.  Created automatically when absent.
        Defaults to ``~/.sharelink/logs``.
    log_level:
        Minimum severity level to record.  Use the constants from the
        :mod:`logging` standard library module (e.g. :data:`logging.DEBUG`,
        :data:`logging.INFO`, :data:`logging.WARNING`).
        Defaults to :data:`logging.INFO`.

    Examples
    --------
    ::

        import logging
        from pathlib import Path
        from sharelink import configure_logging

        configure_logging(
            log_dir=Path("/var/log/myapp"),
            log_level=logging.DEBUG,
        )
    """
    _configure_logging(log_dir=log_dir, log_level=log_level)

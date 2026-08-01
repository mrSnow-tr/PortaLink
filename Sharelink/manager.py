"""
sharelink.manager
~~~~~~~~~~~~~~~~~

Central registry and lifecycle coordinator for all active file shares.

This module provides :class:`ShareManager`, the primary public entry point for
the sharelink package.  It owns the complete collection of
:class:`~session.ShareSession` instances and exposes the user-facing API for
creating, querying, and revoking shares.  A background daemon thread sweeps
the registry on a configurable cadence, expiring stale shares and evicting
terminal sessions to release HTTP server sockets and Cloudflare tunnel
processes promptly.

Design decisions
----------------
Separation of concerns
    The manager performs no file I/O, HTTP handling, or tunnel management.
    Those responsibilities belong entirely to :class:`~session.ShareSession`,
    :class:`~download.DownloadHandler`, :class:`~server.ShareLinkServer`, and
    :class:`~tunnel.TunnelManager`, which are created and owned by individual
    sessions.

Thread-safe registry
    The ``share_id  ShareSession`` dictionary is guarded by a single
    :class:`threading.Lock`.  The lock is held **only** for brief dictionary
    operations; all I/O-bound work (session start / stop) is performed outside
    the lock so that one slow operation never stalls concurrent callers.

Session removal
    Sessions leave the registry through two paths:

    * **Immediate**  When a session transitions to ``EXPIRED`` or ``REVOKED``
      state, the :meth:`~ShareManager._on_session_state_change` callback
      removes it immediately so that :meth:`~ShareManager.list_shares` and
      :meth:`~ShareManager.get_share` reflect reality without waiting for the
      next sweeper cycle.  At the moment the callback fires, the session's
      :meth:`~session.ShareSession.stop` call has not yet completed, but the
      session will accept no further downloads (it is in a terminal state), so
      removing it from the registry is safe.

    * **Deferred**  ``EXHAUSTED`` sessions may still be serving an active
      download when the state transition fires.  They are retained in the
      registry until :meth:`~ShareManager.cleanup_finished` confirms they are
      no longer running (``is_running is False``), at which point the sweeper
      evicts them.

Auto-start
    The background sweeper starts automatically on construction; no explicit
    ``start()`` call is required.  :meth:`~ShareManager.stop` (or the context
    manager protocol) performs a clean shutdown.

Architecture
------------
``ShareManager``
    The single public class.

``_ResolvedSource``
    Module-private frozen dataclass returned by
    :meth:`ShareManager._resolve_source`.  Bundles all normalised
    :class:`~models.ShareInfo` fields derived from the raw *source* argument.

``_local_file_size``
    Module-level private helper.  Wraps ``Path.stat().st_size`` with
    :exc:`OSError` handling, returning ``None`` on failure.

Public API
----------
::

    from sharelink.manager import ShareManager

    manager = ShareManager()

    # Share a local file
    session = manager.create_share("/path/to/archive.tar.gz")
    print(session.public_url)

    # Share a local directory (served as a ZIP archive)
    session = manager.create_share("/data/exports/")

    # Share an FTP resource via full URL
    session = manager.create_share(
        "ftp://ftpuser:secret@ftp.example.com/pub/dataset.csv"
    )

    # Share an FTP resource with explicit connection parameters
    session = manager.create_share(
        "/pub/dataset.csv",
        ftp_host="ftp.example.com",
        ftp_username="ftpuser",
        ftp_password="secret",
    )

    # Custom expiry and download limit
    session = manager.create_share(
        "/tmp/report.pdf",
        expire_seconds=3_600,
        max_downloads=3,
    )

    manager.stop()

Context manager (recommended for scripts)::

    with ShareManager() as manager:
        session = manager.create_share("/data/file.bin")
        url = session.wait_for_url(timeout=30.0)
        print(url)
"""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from urllib.parse import urlparse
from .config import ShareConfig
from .logger import get_logger
from .models import ShareInfo, ShareState, SourceType
from .session import ShareSession
from .utils import (
    expiry_datetime,
    generate_share_id,
    is_ftp_url,
    parse_ftp_url,
    utc_now,
)

_logger = get_logger(__name__)

__all__: list[str] = ["ShareManager"]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SWEEPER_JOIN_TIMEOUT: Final[float] = 10.0
"""Maximum seconds to wait for the background sweeper thread to exit cleanly
during :meth:`ShareManager.stop`."""

_TOKEN_BYTES: Final[int] = 32
"""Number of random bytes used to generate each share's authorisation token.
Yields 256 bits of entropy when encoded by :func:`secrets.token_urlsafe`."""

_IMMEDIATE_REMOVAL_STATES: Final[frozenset[ShareState]] = frozenset({
    ShareState.EXPIRED,
    ShareState.REVOKED,
})
"""Terminal states that trigger immediate removal from the registry via the
:meth:`ShareManager._on_session_state_change` callback.

``EXHAUSTED`` is intentionally absent: an exhausted session may still be
actively transferring bytes to a client at the moment the state transition
fires.  Those sessions are evicted by :meth:`ShareManager.cleanup_finished`
once ``is_running`` becomes ``False``."""


# ---------------------------------------------------------------------------
# Private dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    """Normalised source fields produced by :meth:`ShareManager._resolve_source`.

    Bundles the information derived from the caller-supplied *source* argument
    (and any FTP keyword overrides) into a single immutable value that maps
    directly onto :class:`~models.ShareInfo` constructor parameters.

    Attributes
    ----------
    source_path:
        Resolved absolute filesystem path for local sources, or the remote
        path component (always beginning with ``'/'``) for FTP sources.
    source_type:
        Detected :class:`~models.SourceType`.
    file_size:
        Pre-fetched byte size for :attr:`~models.SourceType.LOCAL_FILE`
        sources; ``None`` for directories and FTP resources where the size
        is either unknown in advance or must be queried on demand.
    ftp_host:
        FTP server hostname or IP address for FTP sources; ``None`` for local
        sources.
    ftp_port:
        FTP TCP port; ``None`` for local sources (defaults to 21 in the
        download layer).
    ftp_username:
        FTP authentication username; ``None`` for local sources.
    ftp_password:
        FTP authentication password; ``None`` for local sources.
    """

    source_path: str
    source_type: SourceType
    file_size: int | None
    ftp_host: str | None
    ftp_port: int | None
    ftp_username: str | None
    ftp_password: str | None


# ---------------------------------------------------------------------------
# Module-level private helper
# ---------------------------------------------------------------------------


def _local_file_size(path: Path) -> int | None:
    """Return the byte size of *path*, or *None* on any :exc:`OSError`.

    Pre-populating :attr:`~models.ShareInfo.file_size` at share-creation time
    lets :meth:`~download.DownloadHandler.get_content_length` return the value
    immediately without issuing a redundant ``stat()`` system call on every
    download request.

    Parameters
    ----------
    path:
        Absolute path to an existing local file.

    Returns
    -------
    int | None
        File size in bytes, or ``None`` when the file cannot be stat'd.
    """
    try:
        return path.stat().st_size
    except OSError as exc:
        _logger.warning(
            "Cannot stat source file for size pre-population",
            extra={"path": str(path), "error": str(exc)},
        )
        return None


# ---------------------------------------------------------------------------
# ShareManager
# ---------------------------------------------------------------------------


class ShareManager:
    """Central registry and lifecycle coordinator for all active file shares.

    Owns the complete collection of :class:`~session.ShareSession` instances
    and provides the primary public API for creating, querying, and revoking
    shares.  A background daemon thread sweeps the registry periodically,
    expiring time-exceeded shares and evicting terminal sessions so that
    server sockets and Cloudflare tunnel processes are released promptly.

    The background sweeper starts automatically on construction; no explicit
    ``start()`` call is required.  Use :meth:`stop` or the context manager
    protocol to perform a clean shutdown that stops all active sessions and
    terminates the sweeper.

    Thread safety
    -------------
    The internal ``share_id  ShareSession`` registry is guarded by a single
    :class:`threading.Lock`.  The lock is held **only** for brief dictionary
    operations; all I/O-bound work (session start / stop, tunnel lifecycle) is
    performed outside the lock so that one slow operation never prevents
    concurrent callers from accessing the registry.

    Parameters
    ----------
    config:
        Package configuration snapshot.  A default :class:`~config.ShareConfig`
        is used when *None*.

    Examples
    --------
    Minimal usage::

        manager = ShareManager()
        session = manager.create_share("/home/user/report.pdf")
        print(session.public_url)
        manager.stop()

    Context manager (recommended)::

        with ShareManager() as manager:
            session = manager.create_share("/tmp/data.tar.gz")
            url = session.wait_for_url(timeout=30.0)
            print(url)

    Custom configuration::

        from sharelink.config import ShareConfig
        cfg = ShareConfig(expire_seconds=3_600, max_downloads=3)
        with ShareManager(config=cfg) as manager:
            session = manager.create_share("/tmp/report.pdf")
    """

    def __init__(self, config: ShareConfig | None = None) -> None:
        self._config: ShareConfig = config or ShareConfig()

        # Thread-safe session registry: share_id  ShareSession.
        self._sessions: dict[str, ShareSession] = {}
        self._lock: threading.Lock = threading.Lock()

        # Signalled by stop() to wake the sweeper immediately and cause it
        # to exit its wait loop without waiting for the full interval.
        self._stop_event: threading.Event = threading.Event()

        # Background sweeper daemon  started immediately so callers do not
        # need an explicit start() call.
        self._sweeper_thread: threading.Thread = threading.Thread(
            target=self._sweeper_loop,
            name="sharelink-session-sweeper",
            daemon=True,
        )
        self._sweeper_thread.start()

        _logger.info(
            "ShareManager initialised",
            extra={
                "sweep_interval_seconds": self._config.session_sweep_interval,
                "default_expire_seconds": self._config.expire_seconds,
                "default_max_downloads": self._config.max_downloads,
            },
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ShareManager":
        """Return *self*; the sweeper is already running after construction."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop all sessions and the sweeper on context exit, regardless of exceptions."""
        self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop all active sessions and shut down the background sweeper.

        Signals the sweeper thread to exit, atomically drains the session
        registry, and calls :meth:`~session.ShareSession.stop` on every
        registered session to release HTTP server sockets and Cloudflare
        tunnel processes.  Blocks until the sweeper thread exits or
        :data:`_SWEEPER_JOIN_TIMEOUT` seconds elapse.

        This method is idempotent.  Calling it when no sessions are registered
        or on an already-stopped manager is safe and has no effect beyond
        ensuring the sweeper thread is no longer alive.
        """
        self._stop_event.set()

        # Atomically drain the registry so no concurrent call can observe a
        # partially-stopped state or race to add sessions while we iterate.
        with self._lock:
            sessions: list[ShareSession] = list(self._sessions.values())
            self._sessions.clear()

        # Stop each session outside the lock; calls may block briefly while
        # server and tunnel daemon threads exit.
        for session in sessions:
            try:
                session.stop()
            except Exception:
                _logger.exception(
                    "Error stopping session during manager shutdown",
                    extra={"share_id": session.share_id},
                )

        # Wait for the sweeper daemon to acknowledge the stop signal.
        if self._sweeper_thread.is_alive():
            self._sweeper_thread.join(timeout=_SWEEPER_JOIN_TIMEOUT)
            if self._sweeper_thread.is_alive():
                _logger.warning(
                    "Session sweeper thread did not exit within %.1f s after stop().",
                    _SWEEPER_JOIN_TIMEOUT,
                )

        _logger.info(
            "ShareManager stopped",
            extra={"sessions_stopped": len(sessions)},
        )

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
    ) -> ShareSession:
        """Create, register, and start a new file share.

        Resolves the source type from *source*, constructs a
        :class:`~models.ShareInfo`, wraps it in a :class:`~session.ShareSession`,
        registers the session, and starts it (binding the HTTP server socket
        and launching the Cloudflare tunnel process).

        Source type resolution
        ----------------------
        Three paths are tried in order:

        1. **FTP URL**  when *source* carries the ``ftp://`` scheme it is
           parsed by :func:`~utils.parse_ftp_url`.  The URL path component
           becomes ``source_path``; connection details populate the ``ftp_*``
           fields.  Keyword argument overrides take precedence over URL-parsed
           values when both are provided.

        2. **Explicit FTP path**  when *ftp_host* is not ``None`` but *source*
           has no ``ftp://`` scheme, *source* is treated as the absolute remote
           path on the FTP server.  A leading ``'/'`` is prepended when absent.

        3. **Local filesystem**  the path is resolved to an absolute location
           via :meth:`pathlib.Path.resolve`.  Regular files have their byte
           size pre-fetched and stored in ``file_size`` to avoid redundant
           ``stat()`` calls on every subsequent download request.  Directories
           are served as on-the-fly ZIP archives whose total size is unknown in
           advance, so ``file_size`` is left as ``None`` and the server uses
           chunked transfer encoding.

        Parameters
        ----------
        source:
            Path to a local file or directory, or an FTP URL in the form
            ``ftp://[user[:pass]@]host[:port]/path``.
        expire_seconds:
            Seconds until the share expires automatically.  Must be a positive
            integer.  Defaults to :attr:`~config.ShareConfig.expire_seconds`.
        max_downloads:
            Maximum number of completed downloads before the share is
            invalidated.  Must be a positive integer.  Defaults to
            :attr:`~config.ShareConfig.max_downloads`.
        display_name:
            Override for the filename sent in the ``Content-Disposition``
            response header.  When *None*, the basename of *source* is used.
        content_type:
            Explicit MIME type for the ``Content-Type`` response header.
            When *None*, the MIME type is guessed from the file extension by
            :func:`~utils.guess_mime`.
        ftp_host:
            FTP server hostname or IP address.  Must be supplied when *source*
            is an FTP path without the ``ftp://`` scheme.  Overrides the host
            extracted from the URL when both are provided.
        ftp_port:
            FTP TCP port.  Defaults to ``21`` in the download layer when
            *None*.
        ftp_username:
            FTP authentication username.  Defaults to ``'anonymous'`` in the
            download layer when *None*.
        ftp_password:
            FTP authentication password.  Defaults to ``'anonymous@'`` in the
            download layer when *None*.
        ftp_passive:
            ``True`` (default) to use passive (PASV) mode; ``False`` for
            active (PORT) mode.  Passive mode is strongly recommended when the
            host is behind a NAT or firewall.
        wait_for_url:
            When ``True`` (default), block until the Cloudflare tunnel
            announces a public URL or *url_timeout* seconds elapse.  A warning
            is logged on timeout but no exception is raised; the tunnel
            continues its connection attempt in the background and the caller
            can invoke :meth:`~session.ShareSession.wait_for_url` on the
            returned session to block further.
        url_timeout:
            Maximum seconds to wait for the tunnel URL when *wait_for_url*
            is ``True``.

        Returns
        -------
        ShareSession
            A started session whose HTTP server socket is bound and whose
            Cloudflare tunnel monitor thread is running.  Access the public
            download URL via ``session.public_url``, or call
            ``session.wait_for_url(timeout=)`` to block until the URL is
            available.

        Raises
        ------
        FileNotFoundError
            When *source* is a local path that does not exist on disk.
        ValueError
            When the resolved local path is neither a regular file nor a
            directory, or when an FTP URL carries an unrecognised scheme.
        OSError
            When the HTTP server cannot bind to an available ephemeral port.
        RuntimeError
            When the Cloudflare tunnel cannot be started because the current
            CPU architecture is unsupported and no cached binary exists.
        urllib.error.URLError
            When the cloudflared binary must be downloaded but the request
            to GitHub Releases fails.
        """
        effective_expire: int = (
            expire_seconds
            if expire_seconds is not None
            else self._config.expire_seconds
        )
        effective_max: int = (
            max_downloads
            if max_downloads is not None
            else self._config.max_downloads
        )

        resolved: _ResolvedSource = self._resolve_source(
            str(source),
            ftp_host=ftp_host,
            ftp_port=ftp_port,
            ftp_username=ftp_username,
            ftp_password=ftp_password,
        )

        now = utc_now()
        share_info = ShareInfo(
            share_id=generate_share_id(),
            source_path=resolved.source_path,
            source_type=resolved.source_type,
            state=ShareState.PENDING,
            token=secrets.token_urlsafe(_TOKEN_BYTES),
            created_at=now,
            expires_at=expiry_datetime(
                hours=effective_expire / 3600.0,
                from_time=now,
            ),
            max_downloads=effective_max,
            display_name=display_name,
            content_type=content_type,
            file_size=resolved.file_size,
            ftp_host=resolved.ftp_host,
            ftp_port=resolved.ftp_port,
            ftp_username=resolved.ftp_username,
            ftp_password=resolved.ftp_password,
            ftp_passive=ftp_passive,
        )

        session = ShareSession(
            share_info=share_info,
            config=self._config,
            on_state_change=self._on_session_state_change,
        )

        # Register before starting so that stop() always discovers every
        # session  even one whose start() is blocking on a binary download.
        with self._lock:
            self._sessions[share_info.share_id] = session

        try:
            session.start()
        except Exception:
            # Roll back the registry entry so the failed session is not
            # returned by list_shares() or get_share().
            with self._lock:
                self._sessions.pop(share_info.share_id, None)
            raise

        if wait_for_url:
            url = session.wait_for_url(timeout=url_timeout)
            if url is None:
                _logger.warning(
                    "Share created but public URL not available within timeout",
                    extra={
                        "share_id": share_info.share_id,
                        "url_timeout_seconds": url_timeout,
                    },
                )

        _logger.info(
            "Share created",
            extra={
                "share_id": share_info.share_id,
                "source_type": resolved.source_type.name,
                "source_path": resolved.source_path,
                "expire_seconds": effective_expire,
                "max_downloads": effective_max,
            },
        )

        return session

    # ------------------------------------------------------------------
    # Registry queries
    # ------------------------------------------------------------------

    def get_share(self, share_id: str) -> ShareSession | None:
        """Return the registered session for *share_id*, or *None*.

        Parameters
        ----------
        share_id:
            Unique identifier of the share to look up.

        Returns
        -------
        ShareSession | None
            The live session, or ``None`` when *share_id* is absent from the
            registry.
        """
        with self._lock:
            return self._sessions.get(share_id)

    def list_shares(self) -> list[ShareSession]:
        """Return a snapshot of all sessions currently in the registry.

        Sessions in terminal states that have not yet been evicted by
        :meth:`cleanup_finished` may be included  notably ``EXHAUSTED``
        sessions that are still serving an active download.

        The returned list is a copy; modifications have no effect on the
        internal registry.

        Returns
        -------
        list[ShareSession]
            All registered sessions in an unspecified order.
        """
        with self._lock:
            return list(self._sessions.values())

    # ------------------------------------------------------------------
    # Share deletion
    # ------------------------------------------------------------------

    def delete_share(self, share_id: str) -> bool:
        """Revoke and remove a share from the registry.

        Removes the session from the registry atomically under the lock, then
        calls :meth:`~session.ShareSession.delete` outside the lock to
        transition the share to ``REVOKED`` state and stop its HTTP server and
        Cloudflare tunnel.  The resulting :meth:`_on_session_state_change`
        callback will attempt a second removal, but since the entry is already
        gone, the pop is a harmless no-op.

        Parameters
        ----------
        share_id:
            Unique identifier of the share to revoke.

        Returns
        -------
        bool
            ``True`` when the share was found and revoked; ``False`` when
            *share_id* is not present in the registry.
        """
        with self._lock:
            session = self._sessions.pop(share_id, None)

        if session is None:
            return False

        session.delete()

        _logger.info(
            "Share revoked via manager",
            extra={"share_id": share_id},
        )
        return True

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Force-expire all sessions whose expiry time has elapsed.

        Collects every registered session whose
        :attr:`~models.ShareInfo.is_past_expiry` returns ``True`` and whose
        state is not already terminal, then calls
        :meth:`~session.ShareSession.expire` on each one outside the registry
        lock.

        :meth:`~session.ShareSession.expire` is idempotent; calling it on a
        session that already transitioned to ``EXPIRED`` via its own internal
        timer is safe and has no visible side effect.

        Returns
        -------
        int
            The number of sessions on which :meth:`~session.ShareSession.expire`
            was invoked during this call.
        """
        candidates: list[ShareSession] = []
        with self._lock:
            for session in self._sessions.values():
                if (
                    not session.is_terminal
                    and session.share_info.is_past_expiry
                ):
                    candidates.append(session)

        for session in candidates:
            try:
                session.expire()
            except Exception:
                _logger.exception(
                    "Error force-expiring session during cleanup",
                    extra={"share_id": session.share_id},
                )

        return len(candidates)

    def cleanup_finished(self) -> int:
        """Remove terminal, non-running sessions from the registry.

        A session is eligible for removal when **both** of the following
        conditions hold:

        * Its state is terminal (``EXPIRED``, ``EXHAUSTED``, or ``REVOKED``).
        * Its HTTP server and Cloudflare tunnel are no longer running
          (``is_running is False``).

        ``EXHAUSTED`` sessions that are still actively transferring bytes to a
        client are intentionally retained until the transfer completes.  At
        that point :meth:`~session.ShareSession.record_download_complete`
        calls :meth:`~session.ShareSession.stop` internally and ``is_running``
        becomes ``False``, making the session eligible for the next sweep.

        Returns
        -------
        int
            The number of sessions removed from the registry by this call.
        """
        to_remove: list[str] = []
        with self._lock:
            for share_id, session in self._sessions.items():
                if session.is_terminal and not session.is_running:
                    to_remove.append(share_id)
            for share_id in to_remove:
                del self._sessions[share_id]

        if to_remove:
            _logger.debug(
                "Removed finished sessions from registry",
                extra={"count": len(to_remove), "share_ids": to_remove},
            )

        return len(to_remove)

    # ------------------------------------------------------------------
    # Monitoring properties
    # ------------------------------------------------------------------

    @property
    def active_share_count(self) -> int:
        """Number of sessions currently in ``ACTIVE`` state."""
        with self._lock:
            return sum(
                1 for session in self._sessions.values()
                if session.is_active
            )

    @property
    def total_share_count(self) -> int:
        """Total number of sessions currently in the registry."""
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------------
    # Private  background sweeper
    # ------------------------------------------------------------------

    def _sweeper_loop(self) -> None:
        """Drive the session cleanup lifecycle on the background daemon thread.

        Runs on the ``sharelink-session-sweeper`` daemon thread.  Sleeps for
        :attr:`~config.ShareConfig.session_sweep_interval` seconds between
        each iteration, or wakes immediately when :meth:`stop` sets the stop
        event.

        On each wake-up (that is not a stop signal), calls
        :meth:`cleanup_expired` followed by :meth:`cleanup_finished`.
        Exceptions raised inside either method are caught and logged so that
        a transient error never causes the sweeper to exit prematurely.
        """
        _logger.debug("Session sweeper started")

        interval = float(self._config.session_sweep_interval)

        # Event.wait(timeout) returns True when the event is set (stop signal)
        # and False when the timeout elapses normally.  The loop body runs on
        # each normal timeout and the loop exits immediately on the stop signal.
        while not self._stop_event.wait(timeout=interval):
            try:
                expired = self.cleanup_expired()
                finished = self.cleanup_finished()
                if expired or finished:
                    _logger.info(
                        "Session sweep completed",
                        extra={
                            "newly_expired": expired,
                            "evicted": finished,
                            "remaining": self.total_share_count,
                        },
                    )
            except Exception:
                _logger.exception("Unhandled error in session sweeper loop")

        _logger.debug("Session sweeper stopped")

    # ------------------------------------------------------------------
    # Private  session state change callback
    # ------------------------------------------------------------------

    def _on_session_state_change(
        self,
        share_id: str,
        new_state: ShareState,
    ) -> None:
        """React to a :class:`~session.ShareSession` entering a new state.

        Invoked by :class:`~session.ShareSession` **outside** its internal lock
        via :meth:`~session.ShareSession._notify_state_change` whenever the
        share transitions to a new :class:`~models.ShareState`.  This callback
        may be called from any thread (expiry timer, tunnel monitor, or an HTTP
        request-handler thread).

        Lock ordering
        -------------
        :meth:`~session.ShareSession._notify_state_change` guarantees the
        session's own lock is **not held** when this callback is invoked.
        Acquiring the manager's lock here therefore cannot produce a circular
        wait with any code path that holds the session lock and subsequently
        tries to acquire the manager's lock.

        For states in :data:`_IMMEDIATE_REMOVAL_STATES` (``EXPIRED`` and
        ``REVOKED``), the session is removed from the registry immediately.
        ``EXHAUSTED`` is excluded because the session may still be transferring
        bytes; :meth:`cleanup_finished` handles deferred eviction.

        Parameters
        ----------
        share_id:
            Unique identifier of the share that changed state.
        new_state:
            The :class:`~models.ShareState` the share just transitioned into.
        """
        _logger.debug(
            "Session state changed",
            extra={"share_id": share_id, "new_state": new_state.name},
        )

        if new_state in _IMMEDIATE_REMOVAL_STATES:
            with self._lock:
                self._sessions.pop(share_id, None)
            _logger.info(
                "Session removed from registry on terminal state",
                extra={"share_id": share_id, "state": new_state.name},
            )

    # ------------------------------------------------------------------
    # Private  source resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_source(
        source: str,
        *,
        ftp_host: str | None,
        ftp_port: int | None,
        ftp_username: str | None,
        ftp_password: str | None,
        
    ) -> _ResolvedSource:
        """Normalise *source* and FTP overrides into a :class:`_ResolvedSource`.

        Three resolution paths are evaluated in order:

        1. **FTP URL**  ``ftp://`` scheme detected by :func:`~utils.is_ftp_url`.
           Parsed by :func:`~utils.parse_ftp_url`; keyword argument overrides
           take precedence over URL-parsed values when both are provided.

        2. **Explicit FTP path**  *ftp_host* is not ``None`` but *source*
           carries no ``ftp://`` scheme.  *source* is used as the remote path;
           a leading ``'/'`` is prepended when absent to satisfy the FTP
           absolute-path requirement enforced by :class:`~utils.FTPComponents`.

        3. **Local filesystem**  path is resolved via :meth:`pathlib.Path.resolve`.
           Regular files have their byte size pre-fetched; directory size is
           ``None`` because the on-the-fly ZIP archive size is not known until
           the archive is fully generated.

        Parameters
        ----------
        source:
            Raw source string as supplied by the caller.
        ftp_host:
            Explicit FTP hostname override.
        ftp_port:
            Explicit FTP port override.
        ftp_username:
            Explicit FTP username override.
        ftp_password:
            Explicit FTP password override.

        Returns
        -------
        _ResolvedSource
            Normalised fields ready for :class:`~models.ShareInfo` construction.

        Raises
        ------
        FileNotFoundError
            When *source* is a local path that does not exist on disk.
        ValueError
            When the resolved local path is neither a regular file nor a
            directory, or when an FTP URL carries an unsupported scheme.
        """
        # ------------------------------------------------------------------
        # Path 1: FTP URL (ftp://[user[:pass]@]host[:port]/path)
        # ------------------------------------------------------------------
        if is_ftp_url(source):
            components = parse_ftp_url(source)
            return _ResolvedSource(
                source_path=components.path,
                source_type=SourceType.FTP,
                file_size=None,  # Queried on demand by DownloadHandler.
                ftp_host=(
                    ftp_host if ftp_host is not None else components.host
                ),
                ftp_port=(
                    ftp_port if ftp_port is not None else components.port
                ),
                ftp_username=(
                    ftp_username
                    if ftp_username is not None
                    else components.username
                ),
                ftp_password=(
                    ftp_password
                    if ftp_password is not None
                    else components.password
                ),
            )

        # ------------------------------------------------------------------
        # Path 2: Explicit FTP path (ftp_host provided, no ftp:// scheme)
        # ------------------------------------------------------------------
        if ftp_host is not None:
            ftp_path = source if source.startswith("/") else f"/{source}"
            return _ResolvedSource(
                source_path=ftp_path,
                source_type=SourceType.FTP,
                file_size=None,
                ftp_host=ftp_host,
                ftp_port=ftp_port,
                ftp_username=ftp_username,
                ftp_password=ftp_password,
            )
            
            
        parsed = urlparse(source)
        if parsed.scheme in ("http", "https"):
            return _ResolvedSource(
                source_path=source,
                source_type=SourceType.HTTP,
                file_size=None,
                ftp_host=None,
                ftp_port=None,
                ftp_username=None,
                ftp_password=None,
            )

            
        # ------------------------------------------------------------------
        # Path 3: Local filesystem path
        # ------------------------------------------------------------------
        local_path = Path(source).resolve()

        if not local_path.exists():
            raise FileNotFoundError(
                f"Source path does not exist: {local_path}"
            )

        if local_path.is_dir():
            return _ResolvedSource(
                source_path=str(local_path),
                source_type=SourceType.LOCAL_DIRECTORY,
                file_size=None,  # ZIP archive size is not known in advance.
                ftp_host=None,
                ftp_port=None,
                ftp_username=None,
                ftp_password=None,
            )

        if local_path.is_file():
            return _ResolvedSource(
                source_path=str(local_path),
                source_type=SourceType.LOCAL_FILE,
                file_size=_local_file_size(local_path),
                ftp_host=None,
                ftp_port=None,
                ftp_username=None,
                ftp_password=None,
            )

        raise ValueError(
            f"Source path is neither a regular file nor a directory: {local_path}"
        )

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        total = self.total_share_count
        active = self.active_share_count
        return (
            f"<ShareManager"
            f" sessions={total}"
            f" active={active}"
            f">"
        )

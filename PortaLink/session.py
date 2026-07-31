"""
sharelink.session
~~~~~~~~~~~~~~~~~

Session management for individual file shares.

Each :class:`ShareSession` owns the complete infrastructure required to serve
exactly one active share: a dedicated :class:`~server.ShareLinkServer`, a
:class:`~tunnel.TunnelManager`, and a :class:`~download.DownloadHandler`.
Sessions are created by the manager layer (``manager.py``) and are fully
self-contained: they drive their own state machine, enforce expiration and
download limits, propagate tunnel URL changes, and clean up resources when a
share reaches a terminal state.

Architecture
------------
:class:`ShareSession`
    Primary public class.  Implements ``ShareManagerProtocol`` structurally
    so it can be injected directly into the owned :class:`~server.ShareLinkServer`
    as the single-share authority without any additional adapter.  All mutable
    state is guarded by a :class:`threading.Lock`; I/O-bound operations (server
    and tunnel startup / shutdown) are always performed **outside** the lock to
    prevent prolonged contention.

:class:`_ShareAdapter`
    Module-private proxy returned by :meth:`ShareSession.get_share`.  Bridges
    two incompatible interfaces:

    * **ShareInfoProtocol** (consumed by :class:`~server.ShareLinkServer`) —
      requires ``is_expired``, ``is_exhausted``, ``filename``, ``content_type``,
      and ``to_dict()``, none of which appear on the raw :class:`~models.ShareInfo`.
    * **DownloadHandler duck-typing** — requires ``source_type``, ``source_path``,
      and all FTP connection fields, which :class:`ShareInfoProtocol` does not
      expose.

    Explicit ``@property`` descriptors satisfy the protocol; a ``__getattr__``
    fallback transparently delegates every other attribute lookup to the wrapped
    :class:`~models.ShareInfo`, so :class:`~download.DownloadHandler` can stream
    content unchanged.

Startup sequence
----------------
:meth:`ShareSession.start` uses a three-phase pattern to keep the internal lock
held only for brief state-mutation windows:

1. **Validate & reserve** (lock held): Confirm the share is ``PENDING``, set
   state to ``ACTIVE``, and mark ``_running = True`` atomically.  This prevents
   concurrent ``start()`` calls without holding the lock during slow I/O.
2. **Start components** (lock released): Bind the server socket and locate /
   download the cloudflared binary.  On any failure the lock is re-acquired to
   roll back state before propagating the exception.
3. **Store references** (lock held): Atomically store the live server and tunnel
   references.  If ``stop()`` was called between phases 1 and 3 the components
   are stopped immediately and the method returns without scheduling the expiry
   timer.

State machine
-------------
::

                start()              record_download_start()
                                     when total == max_downloads
    PENDING ──────────► ACTIVE ──────────────────────────────► EXHAUSTED
                          │                                         │
                          │  expires_at elapsed / expire()         │ active_downloads == 0
                          ├─────────────────────────────► EXPIRED ◄┘
                          │
                          └─────── delete() ──────────────────────► REVOKED

EXPIRED, EXHAUSTED, and REVOKED are all terminal states; no further transitions
occur.  EXHAUSTED takes precedence: a share that hits its download limit before
its expiry time is marked EXHAUSTED rather than being overwritten by EXPIRED.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .config import ShareConfig
from .download import DownloadHandler
from .logger import get_logger
from .models import ShareInfo, ShareState
from .server import ShareInfoProtocol, ShareLinkServer
from .tunnel import TunnelManager
from .utils import remaining_seconds, utc_now

_logger = get_logger(__name__)

__all__: list[str] = ["ShareSession"]


# ---------------------------------------------------------------------------
# Internal share proxy
# ---------------------------------------------------------------------------


class _ShareAdapter:
    """Proxy that adapts :class:`~models.ShareInfo` for both server and download layers.

    The :class:`~server.ShareLinkServer` requires objects satisfying
    ``ShareInfoProtocol`` (``is_expired``, ``is_exhausted``, ``filename``,
    ``content_type``, ``to_dict``).  :class:`~download.DownloadHandler` requires
    the raw :class:`~models.ShareInfo` fields it accesses via duck typing
    (``source_type``, ``source_path``, ``display_name``, and all ``ftp_*``
    fields).  :class:`~models.ShareInfo` satisfies neither interface on its own.

    This class bridges the gap:

    * Explicit ``@property`` descriptors cover every ``ShareInfoProtocol``
      requirement with correctly typed return values.
    * :meth:`__getattr__` delegates every other attribute lookup to the wrapped
      :class:`~models.ShareInfo`, making ``source_type``, ``source_path``,
      ``ftp_host``, ``ftp_port``, ``ftp_username``, ``ftp_password``,
      ``ftp_passive``, and ``display_name`` transparently accessible.

    Python's attribute-lookup order guarantees that descriptors (``@property``)
    take precedence over :meth:`__getattr__`, so there is no ambiguity for
    names that exist on both this class and :class:`~models.ShareInfo`.

    Parameters
    ----------
    info:
        The :class:`~models.ShareInfo` instance this adapter wraps.
    session:
        The owning :class:`ShareSession`; used to resolve ``is_expired``,
        ``is_exhausted``, and ``to_dict()`` from live session state rather
        than from the potentially stale ``ShareInfo`` fields.
    """

    def __init__(self, info: ShareInfo, session: "ShareSession") -> None:
        # Store directly to avoid any __setattr__ interaction.
        self.__dict__["_info"] = info
        self.__dict__["_session"] = session

    # ------------------------------------------------------------------
    # Fallback delegation
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Delegate any attribute not resolved by normal lookup to ShareInfo.

        ``object.__getattribute__`` is used to fetch ``_info`` without
        risking recursive calls to this method.

        Raises
        ------
        AttributeError
            When *name* is absent from both the adapter and the wrapped
            :class:`~models.ShareInfo`.
        """
        try:
            info: ShareInfo = object.__getattribute__(self, "_info")
        except AttributeError:
            raise AttributeError(
                f"'_ShareAdapter' object has no attribute {name!r}"
            ) from None
        try:
            return getattr(info, name)
        except AttributeError:
            raise AttributeError(
                f"'_ShareAdapter' object has no attribute {name!r}"
            ) from None

    # ------------------------------------------------------------------
    # ShareInfoProtocol interface
    # ------------------------------------------------------------------

    @property
    def share_id(self) -> str:
        """Unique identifier embedded in the public download URL."""
        return object.__getattribute__(self, "_info").share_id

    @property
    def is_expired(self) -> bool:
        """``True`` when the share has expired by time or by state."""
        return object.__getattribute__(self, "_session").is_expired

    @property
    def is_exhausted(self) -> bool:
        """``True`` when the share has reached its maximum download count."""
        return object.__getattribute__(self, "_session").is_exhausted

    @property
    def file_size(self) -> int | None:
        """Total resource size in bytes, or ``None`` if not known in advance."""
        return object.__getattribute__(self, "_info").file_size

    @property
    def filename(self) -> str:
        """Suggested download filename for ``Content-Disposition`` headers."""
        return DownloadHandler.resolve_filename(
            object.__getattribute__(self, "_info")
        )

    @property
    def content_type(self) -> str:
        """Resolved MIME type string for the ``Content-Type`` response header."""
        return DownloadHandler.resolve_content_type(
            object.__getattribute__(self, "_info")
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the owning session."""
        return object.__getattribute__(self, "_session").to_dict()


# ---------------------------------------------------------------------------
# ShareSession
# ---------------------------------------------------------------------------


class ShareSession:
    """Manages the complete lifecycle of exactly one file share.

    Owns a dedicated :class:`~server.ShareLinkServer` and
    :class:`~tunnel.TunnelManager` that are started and stopped as a unit
    with this session.  Implements the ``ShareManagerProtocol`` structural
    contract so it can be passed directly to :class:`~server.ShareLinkServer`
    without an intermediate manager object.

    Thread safety
    -------------
    All public methods are safe to call concurrently from any number of
    threads.  A single :class:`threading.Lock` serialises access to all
    mutable fields.  Server and tunnel shutdown (which may block briefly while
    threads exit) is always performed **outside** the lock so that other
    threads are never blocked waiting for I/O to complete.

    Parameters
    ----------
    share_info:
        A fully populated :class:`~models.ShareInfo` in ``PENDING`` state.
        This object becomes the authoritative store for all share metadata;
        the session owns it exclusively after construction.
    config:
        Package configuration snapshot.  A default :class:`~config.ShareConfig`
        is used when *None*.
    on_state_change:
        Optional callback invoked outside the internal lock whenever the
        share transitions to a new :class:`~models.ShareState`.  Receives
        the ``share_id`` string and the new state as positional arguments.
        Exceptions raised by this callback are caught and logged so that a
        misbehaving callback cannot interrupt session lifecycle management.

    Examples
    --------
    Typical usage from the manager layer::

        session = ShareSession(share_info, config=cfg, on_state_change=handler)
        session.start()
        public_url = session.wait_for_url(timeout=30.0)
        # … serve downloads …
        session.stop()
    """

    def __init__(
        self,
        share_info: ShareInfo,
        config: ShareConfig | None = None,
        on_state_change: Callable[[str, ShareState], None] | None = None,
    ) -> None:
        self._info: ShareInfo = share_info
        self._config: ShareConfig = config or ShareConfig()
        self._on_state_change: Callable[[str, ShareState], None] | None = on_state_change

        # Download handler is stateless beyond its configuration; create once.
        self._download_handler: DownloadHandler = DownloadHandler(self._config)

        # Live components — None until start() completes successfully.
        self._server: ShareLinkServer | None = None
        self._tunnel: TunnelManager | None = None
        self._expiry_timer: threading.Timer | None = None

        # Proxy used as the sole entry in get_share() / list_shares().
        self._adapter: _ShareAdapter = _ShareAdapter(share_info, self)

        # Guards all mutable fields below.
        self._lock: threading.Lock = threading.Lock()

        # True once start() sets up the server and tunnel; False after stop().
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Activate the share by starting its HTTP server and Cloudflare tunnel.

        Transitions the share from ``PENDING`` to ``ACTIVE``.  Returns as soon
        as the server socket is bound and the tunnel monitor thread is running.
        The public URL is not yet available at return time; call
        :meth:`wait_for_url` to block until the tunnel establishes its first
        connection.

        The startup follows a three-phase approach to avoid holding the
        internal lock during slow I/O (cloudflared binary lookup / download):

        1. Validate state and atomically mark the session as running (lock
           held for milliseconds).
        2. Bind the server socket and start the tunnel process (lock released).
        3. Store the live component references atomically; detect and handle
           a concurrent :meth:`stop` call that arrived between phases 1 and 2
           (lock held for milliseconds).

        Raises
        ------
        RuntimeError
            If the session is already running, or if the share is not in
            ``PENDING`` state.
        OSError
            If the server cannot bind to the configured host/port.
        RuntimeError
            If the CPU architecture is unsupported for cloudflared download.
        urllib.error.URLError
            If cloudflared must be downloaded but the request fails.
        FileNotFoundError
            If cloudflared cannot be located and cannot be downloaded.
        """
        # Phase 1 — Validate and reserve the session atomically.
        with self._lock:
            if self._running:
                raise RuntimeError(
                    f"Session for share {self._info.share_id!r} is already running."
                )
            if self._info.state != ShareState.PENDING:
                raise RuntimeError(
                    f"Cannot start share {self._info.share_id!r}: "
                    f"expected PENDING state, got {self._info.state.name}."
                )
            # Transition to ACTIVE and mark running before releasing the lock so
            # that concurrent start() calls are rejected immediately.
            self._info.state = ShareState.ACTIVE
            self._running = True

        # Phase 2 — Start I/O components with the lock released.
        _server: ShareLinkServer | None = None
        _tunnel: TunnelManager | None = None
        try:
            _server = ShareLinkServer(
                share_manager=self,
                download_handler=self._download_handler,
                host=self._config.host,
                port=0,  # OS assigns an ephemeral port.
            )
            _server.start()

            _tunnel = TunnelManager(
                host=self._config.host,
                port=_server.port,
                config=self._config,
            )
            _tunnel.add_url_listener(self._on_tunnel_url_changed)
            _tunnel.start()

        except Exception:
            # Roll back the state changes from Phase 1 before propagating.
            with self._lock:
                self._running = False
                self._info.state = ShareState.PENDING
            if _tunnel is not None:
                _tunnel.stop()
            if _server is not None:
                _server.stop()
            raise

        # Phase 3 — Atomically store references; honour a concurrent stop().
        stopped_during_startup: bool = False
        with self._lock:
            if self._running:
                self._server = _server
                self._tunnel = _tunnel
            else:
                # stop() arrived between Phase 1 and Phase 3; discard components.
                stopped_during_startup = True

        if stopped_during_startup:
            _tunnel.stop()
            _server.stop()
            return

        # Schedule automatic expiration with the lock fully released.
        self._schedule_expiry_timer()

        _logger.info(
            "ShareSession started",
            extra={
                "share_id": self._info.share_id,
                "source": self._info.source_path,
                "source_type": self._info.source_type.name,
                "server_port": _server.port,
                "expires_at": self._info.expires_at.isoformat(),
                "max_downloads": self._info.max_downloads,
            },
        )

    def stop(self) -> None:
        """Shut down the server and tunnel without changing the share state.

        Idempotent: safe to call when the session is not running or when called
        multiple times concurrently.  The share's :class:`~models.ShareState`
        is **not** changed by this method; use :meth:`expire` or :meth:`delete`
        when a terminal state transition is also required.

        Blocks until both the server background thread and the tunnel monitor
        thread have exited (subject to their respective join timeouts).
        """
        with self._lock:
            if not self._running:
                return
            self._running = False
            # Cancel the expiry timer while the lock is held so no race exists
            # between checking _expiry_timer and the timer firing.
            if self._expiry_timer is not None:
                self._expiry_timer.cancel()
                self._expiry_timer = None
            server = self._server
            tunnel = self._tunnel
            self._server = None
            self._tunnel = None

        # Shut down outside the lock; both calls may block briefly on thread joins.
        if tunnel is not None:
            tunnel.stop()
        if server is not None:
            server.stop()

        _logger.info(
            "ShareSession stopped",
            extra={
                "share_id": self._info.share_id,
                "state": self._info.state.name,
            },
        )

    def expire(self) -> None:
        """Transition the share to ``EXPIRED`` state and stop the session.

        Called automatically when the expiry timer fires.  Safe to call
        directly for forced early expiration.

        Idempotent: when the share is already in a terminal state this method
        is a no-op for the state machine but still ensures the session is
        stopped.  ``EXHAUSTED`` takes precedence; an exhausted share's state
        is not overwritten with ``EXPIRED``.
        """
        changed: bool = False
        with self._lock:
            if self._info.state not in (
                ShareState.EXPIRED,
                ShareState.EXHAUSTED,
                ShareState.REVOKED,
            ):
                self._info.state = ShareState.EXPIRED
                changed = True

        if changed:
            _logger.info(
                "Share expired",
                extra={"share_id": self._info.share_id},
            )
            self._notify_state_change(ShareState.EXPIRED)

        # Always ensure the session is stopped, even if the state did not change.
        self.stop()

    def delete(self) -> None:
        """Revoke the share immediately and stop the session.

        Transitions the share to ``REVOKED`` state regardless of its current
        state, then stops the server and tunnel.  Idempotent when already
        revoked.
        """
        changed: bool = False
        with self._lock:
            if self._info.state != ShareState.REVOKED:
                self._info.state = ShareState.REVOKED
                changed = True

        if changed:
            _logger.info(
                "Share deleted",
                extra={"share_id": self._info.share_id},
            )
            self._notify_state_change(ShareState.REVOKED)

        self.stop()

    # ------------------------------------------------------------------
    # Blocking helper
    # ------------------------------------------------------------------

    def wait_for_url(self, timeout: float | None = None) -> str | None:
        """Block until the Cloudflare tunnel establishes a public URL.

        Delegates directly to :meth:`~tunnel.TunnelManager.wait_for_url` on
        the owned tunnel instance.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` waits indefinitely.

        Returns
        -------
        str | None
            The public ``https://`` URL once available, or ``None`` when
            *timeout* elapsed before a URL was received.
        """
        with self._lock:
            tunnel = self._tunnel

        if tunnel is None:
            return None

        return tunnel.wait_for_url(timeout=timeout)

    # ------------------------------------------------------------------
    # ShareManagerProtocol implementation
    # (consumed by the owned ShareLinkServer via structural duck typing)
    # ------------------------------------------------------------------

    def get_share(self, share_id: str) -> ShareInfoProtocol | None:
        """Return this session's :class:`_ShareAdapter` when *share_id* matches.

        Parameters
        ----------
        share_id:
            The identifier extracted from the request URL.

        Returns
        -------
        ShareInfoProtocol | None
            The adapter object satisfying ``ShareInfoProtocol``, or ``None``
            when *share_id* does not match this session's share.
        """
        if share_id == self._info.share_id:
            return self._adapter
        return None

    def list_shares(self) -> list[ShareInfoProtocol]:
        """Return a single-element list containing this session's adapter.

        Returns
        -------
        list[ShareInfoProtocol]
            A list with exactly one element: the adapter for this share.
        """
        return [self._adapter]

    def delete_share(self, share_id: str) -> bool:
        """Revoke this share via the REST API if *share_id* matches.

        Parameters
        ----------
        share_id:
            The identifier from the ``DELETE /api/shares/<share_id>`` request.

        Returns
        -------
        bool
            ``True`` when the share was found and revoked; ``False`` when
            *share_id* does not match this session.
        """
        if share_id != self._info.share_id:
            return False
        self.delete()
        return True

    def record_download_start(self, share_id: str) -> None:
        """Record the beginning of a download and enforce the download limit.

        Increments :attr:`~models.ShareStatistics.total_downloads` and
        :attr:`~models.ShareStatistics.active_downloads`.  When
        ``total_downloads`` reaches ``max_downloads`` the share transitions
        to ``EXHAUSTED``, causing the server to reject subsequent requests
        with ``410 Gone`` while allowing the current download to proceed to
        completion.

        Parameters
        ----------
        share_id:
            Must match this session's share identifier; silently ignored
            when it does not.
        """
        if share_id != self._info.share_id:
            return

        now = utc_now()
        became_exhausted: bool = False

        with self._lock:
            stats = self._info.statistics
            stats.total_downloads += 1
            stats.active_downloads += 1
            stats.last_accessed = now
            if stats.first_accessed is None:
                stats.first_accessed = now

            if (
                self._info.state == ShareState.ACTIVE
                and stats.total_downloads >= self._info.max_downloads
            ):
                self._info.state = ShareState.EXHAUSTED
                became_exhausted = True

        if became_exhausted:
            _logger.info(
                "Share exhausted: download limit reached",
                extra={
                    "share_id": share_id,
                    "total_downloads": self._info.statistics.total_downloads,
                    "max_downloads": self._info.max_downloads,
                },
            )
            self._notify_state_change(ShareState.EXHAUSTED)

    def record_download_complete(self, share_id: str) -> None:
        """Record the completion of a download and release resources when done.

        Decrements :attr:`~models.ShareStatistics.active_downloads` and
        increments :attr:`~models.ShareStatistics.completed_downloads`.  When
        the share is ``EXHAUSTED`` and no active downloads remain, the session
        is stopped so the server socket and tunnel process are released promptly
        rather than waiting for the manager's next sweep.

        Parameters
        ----------
        share_id:
            Must match this session's share identifier; silently ignored
            when it does not.
        """
        if share_id != self._info.share_id:
            return

        stop_now: bool = False
        with self._lock:
            stats = self._info.statistics
            stats.active_downloads = max(0, stats.active_downloads - 1)
            stats.completed_downloads += 1

            if (
                self._info.state == ShareState.EXHAUSTED
                and stats.active_downloads == 0
            ):
                stop_now = True

        if stop_now:
            _logger.info(
                "Stopping exhausted session after last active download completed",
                extra={"share_id": share_id},
            )
            self.stop()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def share_id(self) -> str:
        """Unique identifier of this share."""
        return self._info.share_id

    @property
    def share_info(self) -> ShareInfo:
        """Read-only reference to the underlying :class:`~models.ShareInfo`.

        The returned object is the live record used by the session; do not
        mutate it directly.  All state changes must go through the session's
        public methods to maintain thread safety and trigger callbacks.
        """
        return self._info

    @property
    def public_url(self) -> str:
        """The current public HTTPS download URL for this share.

        Returns an empty string until the tunnel announces its first URL.
        The value may change if the tunnel reconnects with a new hostname.
        """
        with self._lock:
            return self._info.public_url

    @property
    def state(self) -> ShareState:
        """Current :class:`~models.ShareState` of this share."""
        with self._lock:
            return self._info.state

    @property
    def is_active(self) -> bool:
        """``True`` when the share is in ``ACTIVE`` state and serving requests."""
        with self._lock:
            return self._info.state == ShareState.ACTIVE

    @property
    def is_terminal(self) -> bool:
        """``True`` when the share is in a terminal state.

        Terminal states are ``EXPIRED``, ``EXHAUSTED``, and ``REVOKED``.  A
        terminal share will never transition back to ``ACTIVE`` and may be
        safely removed from the manager registry.
        """
        with self._lock:
            return self._info.is_terminal

    @property
    def is_expired(self) -> bool:
        """``True`` when the share has passed its expiry time or is in ``EXPIRED`` state.

        This property reflects the real-time clock so it becomes ``True`` at
        ``expires_at`` regardless of whether the expiry timer has fired yet.
        """
        with self._lock:
            return (
                self._info.state == ShareState.EXPIRED
                or self._info.is_past_expiry
            )

    @property
    def is_exhausted(self) -> bool:
        """``True`` when the share has reached its maximum allowed download count.

        Checks both the explicit ``EXHAUSTED`` state and the computed
        ``downloads_remaining == 0`` condition to handle the brief window
        between the last download starting and the state transition completing.
        """
        with self._lock:
            return (
                self._info.state == ShareState.EXHAUSTED
                or self._info.downloads_remaining == 0
            )

    @property
    def is_running(self) -> bool:
        """``True`` while the HTTP server and tunnel are active for this share."""
        return self._running

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of this session's current state.

        All mutable fields are read under the internal lock to guarantee a
        consistent snapshot.  :class:`datetime` values are encoded as ISO 8601
        strings; the ``unique_ips`` set is converted to a sorted list.

        Returns
        -------
        dict[str, Any]
            A dictionary suitable for direct use in JSON API responses.
        """
        with self._lock:
            info = self._info
            stats = info.statistics
            return {
                "share_id": info.share_id,
                "source_path": info.source_path,
                "source_type": info.source_type.name,
                "state": info.state.name,
                "public_url": info.public_url,
                "filename": DownloadHandler.resolve_filename(info),
                "content_type": DownloadHandler.resolve_content_type(info),
                "file_size": info.file_size,
                "display_name": info.display_name,
                "created_at": info.created_at.isoformat(),
                "expires_at": info.expires_at.isoformat(),
                "max_downloads": info.max_downloads,
                "downloads_remaining": info.downloads_remaining,
                "is_expired": (
                    info.state == ShareState.EXPIRED or info.is_past_expiry
                ),
                "is_exhausted": (
                    info.state == ShareState.EXHAUSTED
                    or info.downloads_remaining == 0
                ),
                "is_running": self._running,
                "ftp_host": info.ftp_host,
                "ftp_port": info.ftp_port,
                "statistics": {
                    "total_downloads": stats.total_downloads,
                    "completed_downloads": stats.completed_downloads,
                    "active_downloads": stats.active_downloads,
                    "total_bytes_transferred": stats.total_bytes_transferred,
                    "unique_ips": sorted(stats.unique_ips),
                    "first_accessed": (
                        stats.first_accessed.isoformat()
                        if stats.first_accessed is not None
                        else None
                    ),
                    "last_accessed": (
                        stats.last_accessed.isoformat()
                        if stats.last_accessed is not None
                        else None
                    ),
                },
            }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _on_tunnel_url_changed(self, tunnel_url: str) -> None:
        """Update the share's public URL when the tunnel reports a new URL.

        Invoked on the :class:`~tunnel.TunnelManager` monitor daemon thread.
        Constructs the full per-share download URL by appending the share's
        path segment to the tunnel's base URL.

        Parameters
        ----------
        tunnel_url:
            The HTTPS base URL announced by cloudflared, e.g.
            ``'https://example.trycloudflare.com'``.
        """
        new_url = f"{tunnel_url}/d/{self._info.share_id}"
        with self._lock:
            self._info.public_url = new_url

        _logger.info(
            "Share public URL updated",
            extra={
                "share_id": self._info.share_id,
                "public_url": new_url,
            },
        )

    def _schedule_expiry_timer(self) -> None:
        """Schedule a daemon timer to call :meth:`expire` at the share's expiry time.

        Uses :func:`~utils.remaining_seconds` to compute the delay.  When the
        share is already past its expiry time the method calls :meth:`expire`
        on a short-lived daemon thread to keep :meth:`start` non-blocking.

        This method must be called **outside** ``self._lock`` to avoid
        re-entrancy if the delay is zero or negative.
        """
        delay = remaining_seconds(self._info.expires_at)

        if delay <= 0.0:
            # Already expired; fire on a daemon thread so start() returns fast.
            t = threading.Thread(
                target=self.expire,
                daemon=True,
                name=f"sharelink-expiry-{self._info.share_id}",
            )
            t.start()
            return

        with self._lock:
            if not self._running:
                # Session was stopped between start() phases; nothing to schedule.
                return
            timer = threading.Timer(interval=delay, function=self.expire)
            timer.daemon = True
            timer.name = f"sharelink-expiry-{self._info.share_id}"
            timer.start()
            self._expiry_timer = timer

    def _notify_state_change(self, new_state: ShareState) -> None:
        """Invoke the ``on_state_change`` callback outside the internal lock.

        Exceptions raised by the callback are caught and logged so that a
        misbehaving callback cannot interrupt session lifecycle management.

        Parameters
        ----------
        new_state:
            The :class:`~models.ShareState` the share just transitioned into.
        """
        callback = self._on_state_change
        if callback is None:
            return
        try:
            callback(self._info.share_id, new_state)
        except Exception:
            _logger.exception(
                "on_state_change callback raised an exception",
                extra={
                    "share_id": self._info.share_id,
                    "new_state": new_state.name,
                },
            )

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        with self._lock:
            state_name = self._info.state.name
        return (
            f"<ShareSession"
            f" id={self._info.share_id!r}"
            f" state={state_name}"
            f" running={self._running}"
            f">"
        )

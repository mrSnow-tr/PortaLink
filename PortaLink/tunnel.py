"""
sharelink.tunnel
~~~~~~~~~~~~~~~~

Cloudflare Tunnel integration for the sharelink package.

Responsibilities
----------------
* Locate the ``cloudflared`` binary via :func:`~utils.find_cloudflared`.
* Download ``cloudflared`` automatically from GitHub Releases when absent.
* Start a Cloudflare quick-tunnel subprocess pointing at the local HTTP server.
* Monitor the subprocess output stream for public URL announcements.
* Reconnect automatically whenever the subprocess exits unexpectedly.
* Notify registered listeners whenever the public URL changes.
* Shut down cleanly, releasing the subprocess and all threads.

Architecture
------------
``TunnelManager``
    The single public class.  Owns the entire tunnel lifecycle: binary
    resolution, process management, output parsing, reconnect scheduling,
    and listener dispatch.  All public methods are thread-safe.

Module-level private helpers
    ``_is_executable``       – Checks owner-execute permission on a path.
    ``_download_cloudflared`` – Fetches the cloudflared binary from GitHub.
    ``_ensure_cloudflared``  – Locates or downloads cloudflared.
    ``_terminate_popen``     – SIGTERM → SIGKILL helper for subprocesses.

Threading model
---------------
One daemon thread (``sharelink-tunnel-monitor``) drives the complete lifecycle:

1. Launch the ``cloudflared`` subprocess with merged stdout+stderr.
2. Read its output line by line, scanning every line for a public URL with
   a compiled regular expression.
3. When a URL is found, update internal state, signal any callers blocked in
   :meth:`TunnelManager.wait_for_url`, and invoke all registered listeners.
4. When the process exits for any reason other than a :meth:`~TunnelManager.stop`
   call, wait :attr:`~config.ShareConfig.reconnect_delay` seconds and restart.
5. After :attr:`~config.ShareConfig.reconnect_retries` consecutive runs that
   end without ever producing a URL, set state to ``FAILED`` and exit the loop.

The main thread never blocks; :meth:`TunnelManager.start` returns as soon as
the daemon thread is running.  Callers that need to await the first URL use
:meth:`TunnelManager.wait_for_url`.

Reconnect semantics
-------------------
Two distinct reconnect scenarios are handled transparently:

* **Internal cloudflared reconnect** – cloudflared stays alive but announces
  a new URL.  The output reader picks up the new URL and notifies listeners
  without restarting the process.

* **Process death** – cloudflared exits.  The monitor loop waits
  ``reconnect_delay`` seconds, increments the failure counter only when the
  run never produced a URL, then restarts.  A run that did connect before
  dying resets the failure counter so the full retry budget is available for
  the next outage.
"""

from __future__ import annotations

import re
import stat
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from .config import ShareConfig, resolve_cloudflared_download_url
from .logger import get_system_logger
from .models import TunnelState
from .utils import find_cloudflared

_logger = get_system_logger()

__all__: list[str] = [
    "TunnelManager",
    "URLChangeCallback",
]


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

URLChangeCallback = Callable[[str], None]
"""Callable type for public-URL change notifications.

Each registered callback receives the new HTTPS URL as its only argument and
is invoked on the ``sharelink-tunnel-monitor`` daemon thread.  Implementations
must not block for extended periods; long-running work should be dispatched to
a separate thread.
"""


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
    re.IGNORECASE,
)
"""Compiled regex that matches Cloudflare quick-tunnel public URLs.

The pattern intentionally searches the full line so it works for both
plain-text output and JSON-formatted structured log output produced by
newer cloudflared builds.
"""

_PROCESS_TERMINATE_TIMEOUT: Final[float] = 5.0
"""Seconds to wait for cloudflared to exit after ``SIGTERM`` before escalating
to ``SIGKILL``."""

_MONITOR_JOIN_TIMEOUT: Final[float] = 10.0
"""Seconds to wait for the monitor daemon thread to exit during
:meth:`TunnelManager.stop`."""

_DOWNLOAD_CHUNK_SIZE: Final[int] = 65_536
"""Read buffer size in bytes used when streaming the cloudflared binary."""

_DOWNLOAD_USER_AGENT: Final[str] = "sharelink/1.0"
"""``User-Agent`` header sent with the cloudflared binary download request."""

_CLOUDFLARED_SUBCOMMAND: Final[str] = "tunnel"
"""cloudflared subcommand that activates quick-tunnel mode."""

_CLOUDFLARED_FLAG_NO_UPDATE: Final[str] = "--no-autoupdate"
"""Prevents cloudflared from attempting self-updates that would disrupt the
tunnel or require network access beyond the initial startup."""

_CLOUDFLARED_FLAG_URL: Final[str] = "--url"
"""cloudflared flag that specifies the local service URL to expose publicly."""


# ---------------------------------------------------------------------------
# Private module-level helpers
# ---------------------------------------------------------------------------


def _is_executable(path: Path) -> bool:
    """Return ``True`` when *path* is a regular file with the owner-execute bit set.

    Parameters
    ----------
    path:
        Filesystem path to inspect.

    Returns
    -------
    bool
        ``True`` if the path exists, is a regular file, and ``S_IXUSR`` is set.
        ``False`` for any other condition including ``OSError``.
    """
    try:
        mode: int = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and bool(mode & stat.S_IXUSR)


def _download_cloudflared(dest: Path) -> None:
    """Download the cloudflared binary for the current CPU architecture.

    The binary is streamed into a temporary file, made executable, then
    atomically renamed to *dest* so the destination is never left in a
    partially-written state.  The temporary file is removed on any failure.

    Parameters
    ----------
    dest:
        Absolute path where the executable should be installed.  Parent
        directories are created automatically.

    Raises
    ------
    RuntimeError
        If :func:`~config.resolve_cloudflared_download_url` raises because the
        current CPU architecture is not supported.
    urllib.error.URLError
        If the HTTP request fails.
    OSError
        If the binary cannot be written to or renamed on the local filesystem.
    """
    url: str = resolve_cloudflared_download_url()
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp: Path = dest.with_suffix(".download")

    _logger.info(
        "Downloading cloudflared binary",
        extra={"url": url, "destination": str(dest)},
    )

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _DOWNLOAD_USER_AGENT},
        )
        with urllib.request.urlopen(request) as response:
            with tmp.open("wb") as fh:
                while True:
                    chunk: bytes = response.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)

        # Grant owner, group, and world execute permission.
        permissions: int = (
            tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        tmp.chmod(permissions)

        # Atomic replace: source and destination share the same parent directory.
        tmp.replace(dest)

    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    _logger.info(
        "cloudflared binary installed",
        extra={"destination": str(dest)},
    )


def _ensure_cloudflared(config: ShareConfig) -> Path:
    """Locate cloudflared on the system, downloading it to the cache if absent.

    Search order:

    1. :func:`~utils.find_cloudflared` – checks the ``$CLOUDFLARED``
       environment variable, every directory in ``$PATH``, and a list of
       well-known installation directories.
    2. :attr:`~config.ShareConfig.cloudflared_binary_path` – the package's own
       download cache.
    3. Automatic download from Cloudflare's GitHub Releases page to the
       configured cache path.

    Parameters
    ----------
    config:
        Package configuration providing the cache path and reconnect settings.

    Returns
    -------
    Path
        Resolved absolute path to the cloudflared executable.

    Raises
    ------
    RuntimeError
        If the current CPU architecture is unsupported for automatic download.
    urllib.error.URLError
        If the download request fails.
    OSError
        If the downloaded binary cannot be written to the cache directory.
    """
    # 1 – Standard system locations (env var, PATH, common directories).
    try:
        found: Path = find_cloudflared()
        _logger.debug(
            "Using system cloudflared binary",
            extra={"path": str(found)},
        )
        return found
    except FileNotFoundError:
        pass

    # 2 – Package-managed download cache.
    cached: Path = config.cloudflared_binary_path
    if cached.is_file() and _is_executable(cached):
        _logger.debug(
            "Using cached cloudflared binary",
            extra={"path": str(cached)},
        )
        return cached.resolve()

    # 3 – Automatic download.
    _download_cloudflared(cached)
    return cached.resolve()


def _terminate_popen(
    process: subprocess.Popen[bytes],
    timeout: float = _PROCESS_TERMINATE_TIMEOUT,
) -> None:
    """Terminate *process* gracefully, escalating to ``SIGKILL`` if needed.

    Sends ``SIGTERM`` and waits up to *timeout* seconds.  If the process has
    not exited by then, sends ``SIGKILL`` and waits without a timeout.  Safe
    to call on an already-exited process.

    Parameters
    ----------
    process:
        The subprocess to terminate.
    timeout:
        Seconds to wait after ``SIGTERM`` before sending ``SIGKILL``.
    """
    if process.poll() is not None:
        return  # Already exited.

    try:
        process.terminate()
    except OSError:
        return  # Process exited between the poll() and terminate() calls.

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _logger.warning(
            "cloudflared did not respond to SIGTERM; sending SIGKILL",
            extra={"pid": process.pid},
        )
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class TunnelManager:
    """Manages a Cloudflare quick-tunnel subprocess for the sharelink package.

    A single ``TunnelManager`` instance handles the complete tunnel lifecycle:
    locating or downloading the cloudflared binary, launching the subprocess,
    parsing its output for the public URL, reconnecting on failure, and
    notifying all registered listeners whenever the URL changes.

    Listener contract
    -----------------
    Zero or more :data:`URLChangeCallback` callables may be registered with
    :meth:`add_url_listener`.  Every listener receives the new URL string as
    its sole argument and is called on the ``sharelink-tunnel-monitor`` daemon
    thread.  Listeners must not block for extended periods.  Exceptions raised
    by individual listeners are caught and logged; they do not interrupt
    delivery to subsequent listeners.

    Parameters
    ----------
    host:
        Hostname or IP address of the local HTTP server to expose.  Passed to
        cloudflared's ``--url`` flag (e.g. ``"127.0.0.1"``).
    port:
        TCP port of the local HTTP server to expose.
    config:
        Package configuration.  Uses :class:`~config.ShareConfig` defaults
        when *None*.

    Examples
    --------
    Explicit lifecycle::

        tunnel = TunnelManager(host="127.0.0.1", port=8080)
        tunnel.add_url_listener(lambda url: print("URL:", url))
        tunnel.start()
        public_url = tunnel.wait_for_url(timeout=30.0)
        # … application runs …
        tunnel.stop()

    Context manager::

        with TunnelManager(host="127.0.0.1", port=8080) as tunnel:
            url = tunnel.wait_for_url(timeout=30.0)
            # … application runs …
    """

    def __init__(
        self,
        host: str,
        port: int,
        config: ShareConfig | None = None,
    ) -> None:
        self._host: str = host
        self._port: int = port
        self._config: ShareConfig = config or ShareConfig()

        # --- State (protected by _state_lock) --------------------------------
        self._state: TunnelState = TunnelState.DISCONNECTED
        self._public_url: str | None = None
        self._state_lock: threading.Lock = threading.Lock()

        # --- Listeners (protected by _listeners_lock) -------------------------
        self._listeners: list[URLChangeCallback] = []
        self._listeners_lock: threading.Lock = threading.Lock()

        # --- Subprocess (protected by _process_lock) -------------------------
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock: threading.Lock = threading.Lock()

        # --- Cross-thread signalling ------------------------------------------
        self._stop_event: threading.Event = threading.Event()
        self._url_ready: threading.Event = threading.Event()

        # --- Resolved binary path (written once by start(), read by monitor) --
        self._cloudflared_path: Path | None = None

        # --- Per-run connection flag (monitor thread only) --------------------
        # True when the current _run_tunnel_process invocation received a URL.
        self._run_got_url: bool = False

        # --- Monitor thread --------------------------------------------------
        self._monitor_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Locate cloudflared, start the tunnel, and begin monitoring.

        Returns immediately after spawning the monitor daemon thread.  The
        tunnel URL is not yet available; use :meth:`wait_for_url` to block
        until the first URL is established.

        Raises
        ------
        RuntimeError
            If the tunnel is already active.
        RuntimeError
            If the current CPU architecture is unsupported for auto-download.
        urllib.error.URLError
            If cloudflared must be downloaded but the request fails.
        FileNotFoundError
            If cloudflared cannot be located and cannot be downloaded.
        OSError
            If the cloudflared binary cannot be written to the cache directory.
        """
        with self._state_lock:
            active: frozenset[TunnelState] = frozenset({
                TunnelState.CONNECTING,
                TunnelState.CONNECTED,
                TunnelState.RECONNECTING,
            })
            if self._state in active:
                raise RuntimeError(
                    f"TunnelManager is already active (state={self._state.name}). "
                    "Call stop() before calling start() again."
                )
            self._state = TunnelState.CONNECTING

        self._stop_event.clear()
        self._url_ready.clear()

        # Resolve the binary before spawning the monitor thread so that any
        # download error surfaces to the caller immediately with a clear
        # traceback, rather than appearing silently in a daemon thread.
        self._cloudflared_path = _ensure_cloudflared(self._config)

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="sharelink-tunnel-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

        _logger.info(
            "TunnelManager started",
            extra={
                "host": self._host,
                "port": self._port,
                "cloudflared": str(self._cloudflared_path),
            },
        )

    def stop(self) -> None:
        """Signal the tunnel to stop and wait for a clean shutdown.

        Sets the stop event (which interrupts any reconnect sleep), sends
        ``SIGTERM`` to the cloudflared process (escalating to ``SIGKILL`` if
        needed), then joins the monitor thread.

        Safe to call when the tunnel is not running; behaves as a no-op in
        that case.
        """
        _logger.info("TunnelManager stopping")

        self._stop_event.set()
        self._terminate_process()

        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=_MONITOR_JOIN_TIMEOUT)
            if self._monitor_thread.is_alive():
                _logger.warning(
                    "Tunnel monitor thread did not exit within timeout",
                    extra={"timeout_seconds": _MONITOR_JOIN_TIMEOUT},
                )

        with self._state_lock:
            self._state = TunnelState.DISCONNECTED
            self._public_url = None

        _logger.info("TunnelManager stopped")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "TunnelManager":
        """Start the tunnel and return *self*."""
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop the tunnel on context exit regardless of exceptions."""
        self.stop()

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------

    def add_url_listener(self, callback: URLChangeCallback) -> None:
        """Register *callback* to be invoked when the public URL changes.

        Each unique callable is registered at most once; duplicate
        registrations are silently ignored.

        Parameters
        ----------
        callback:
            A :data:`URLChangeCallback` callable.  Invoked on the monitor
            thread with the new URL string as its only argument.
        """
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_url_listener(self, callback: URLChangeCallback) -> None:
        """Deregister a previously registered URL change callback.

        Does nothing if *callback* is not currently registered.

        Parameters
        ----------
        callback:
            The callable to remove.
        """
        with self._listeners_lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Blocking helpers
    # ------------------------------------------------------------------

    def wait_for_url(self, timeout: float | None = None) -> str | None:
        """Block until a public URL is established or *timeout* expires.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` waits indefinitely.

        Returns
        -------
        str | None
            The public URL string once available, or ``None`` when *timeout*
            elapsed before a URL was received.
        """
        if not self._url_ready.wait(timeout=timeout):
            return None
        with self._state_lock:
            return self._public_url

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def public_url(self) -> str | None:
        """The current public HTTPS tunnel URL, or ``None`` if not yet connected."""
        with self._state_lock:
            return self._public_url

    @property
    def state(self) -> TunnelState:
        """Current operational state of the tunnel."""
        with self._state_lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """``True`` when the tunnel is in ``CONNECTED`` state."""
        with self._state_lock:
            return self._state == TunnelState.CONNECTED

    @property
    def host(self) -> str:
        """Hostname of the local service being tunnelled."""
        return self._host

    @property
    def port(self) -> int:
        """TCP port of the local service being tunnelled."""
        return self._port

    # ------------------------------------------------------------------
    # Private – monitor loop
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Drive the tunnel lifecycle: connect, parse output, and reconnect.

        Runs exclusively on the ``sharelink-tunnel-monitor`` daemon thread.
        The loop exits when :attr:`_stop_event` is set or the consecutive
        failure retry budget is exhausted.
        """
        consecutive_failures: int = 0
        max_retries: int = self._config.reconnect_retries

        while not self._stop_event.is_set():
            got_url: bool = False
            try:
                got_url = self._run_tunnel_process()
            except Exception:
                _logger.exception("Unexpected error in tunnel process management")

            if self._stop_event.is_set():
                break

            # A run that produced a URL resets the consecutive failure counter
            # so that the full retry budget is available after a brief outage.
            if got_url:
                consecutive_failures = 0
            else:
                if max_retries != -1 and consecutive_failures >= max_retries:
                    with self._state_lock:
                        self._state = TunnelState.FAILED
                    _logger.error(
                        "Tunnel permanently failed: maximum retry attempts exhausted",
                        extra={
                            "max_retries": max_retries,
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    return

                consecutive_failures += 1

            with self._state_lock:
                if self._state not in (TunnelState.FAILED,):
                    self._state = TunnelState.RECONNECTING

            _logger.info(
                "Tunnel will reconnect after delay",
                extra={
                    "delay_seconds": self._config.reconnect_delay,
                    "consecutive_failures": consecutive_failures,
                },
            )

            # Interruptible sleep: returns immediately when stop() is called.
            self._stop_event.wait(timeout=self._config.reconnect_delay)

        with self._state_lock:
            if self._state not in (TunnelState.FAILED,):
                self._state = TunnelState.DISCONNECTED

    # ------------------------------------------------------------------
    # Private – process management
    # ------------------------------------------------------------------

    def _run_tunnel_process(self) -> bool:
        """Start cloudflared, consume its output, and wait for it to exit.

        Returns
        -------
        bool
            ``True`` if at least one public URL was parsed during this run,
            meaning the tunnel successfully connected before exiting.
            ``False`` if the process exited or failed without ever producing
            a URL.
        """
        self._run_got_url = False

        cmd: list[str] = self._build_command()
        _logger.debug("Launching cloudflared", extra={"cmd": " ".join(cmd)})

        try:
            process: subprocess.Popen[bytes] = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout.
                close_fds=True,
            )
        except OSError:
            _logger.exception(
                "Failed to launch cloudflared",
                extra={"executable": cmd[0]},
            )
            return False

        with self._process_lock:
            self._process = process

        try:
            self._read_process_output(process)
        finally:
            # Ensure the process is fully dead before returning so the monitor
            # loop can accurately decide whether to reconnect.
            _terminate_popen(process, timeout=_PROCESS_TERMINATE_TIMEOUT)

            # Collect the exit code to prevent zombie processes.
            try:
                process.wait()
            except Exception:
                pass

            with self._process_lock:
                self._process = None

            with self._state_lock:
                if self._state == TunnelState.CONNECTED:
                    self._state = TunnelState.DISCONNECTED

        return self._run_got_url

    def _read_process_output(self, process: subprocess.Popen[bytes]) -> None:
        """Read merged stdout/stderr line by line until EOF or stop signal.

        Each decoded line is forwarded to :meth:`_handle_output_line` for URL
        extraction.  The loop exits when the stream reaches EOF (process exited
        naturally) or when :attr:`_stop_event` is set (graceful shutdown).

        Parameters
        ----------
        process:
            The running cloudflared subprocess whose stdout to consume.
        """
        assert process.stdout is not None, (
            "cloudflared process was not started with stdout=PIPE"
        )

        for raw_bytes in process.stdout:
            if self._stop_event.is_set():
                break
            line: str = raw_bytes.decode("utf-8", errors="replace").rstrip()
            self._handle_output_line(line)

    def _handle_output_line(self, line: str) -> None:
        """Parse one line of cloudflared output and act on any URL found.

        Uses :data:`_URL_PATTERN` which works for both plain-text log lines and
        JSON-formatted structured log output because the URL always appears as a
        literal string value in either format.

        Parameters
        ----------
        line:
            A decoded, trailing-whitespace-stripped line from cloudflared.
        """
        if not line:
            return

        _logger.debug("cloudflared: %s", line)

        match: re.Match[str] | None = _URL_PATTERN.search(line)
        if match is not None:
            self._on_url_discovered(match.group(0))

    def _on_url_discovered(self, url: str) -> None:
        """Handle a newly discovered public URL emitted by cloudflared.

        Updates the internal URL and state, signals :attr:`_url_ready` so that
        any caller blocked in :meth:`wait_for_url` is unblocked, and notifies
        all registered listeners when the URL has changed.

        Parameters
        ----------
        url:
            The HTTPS public URL extracted from cloudflared's output.
        """
        with self._state_lock:
            previous_url: str | None = self._public_url
            self._public_url = url
            self._state = TunnelState.CONNECTED

        # Mark this run as successfully connected so the monitor loop resets
        # the consecutive-failure counter.
        self._run_got_url = True

        # Unblock all callers waiting in wait_for_url().
        self._url_ready.set()

        if url != previous_url:
            _logger.info(
                "Tunnel URL updated",
                extra={"url": url, "previous_url": previous_url},
            )
            self._notify_listeners(url)

    def _notify_listeners(self, url: str) -> None:
        """Invoke all registered URL change callbacks with the new URL.

        Iterates over a snapshot of the listener list so that callbacks may
        safely call :meth:`add_url_listener` or :meth:`remove_url_listener`
        without causing a deadlock.  Exceptions from individual listeners are
        caught and logged; they do not interrupt delivery to remaining listeners.

        Parameters
        ----------
        url:
            The new public tunnel URL to broadcast to all listeners.
        """
        with self._listeners_lock:
            callbacks: list[URLChangeCallback] = list(self._listeners)

        for callback in callbacks:
            try:
                callback(url)
            except Exception:
                _logger.exception(
                    "URL change listener raised an exception",
                    extra={"callback": repr(callback), "url": url},
                )

    def _terminate_process(self) -> None:
        """Terminate the live cloudflared process if one is running.

        Retrieves the current process reference under the process lock, then
        delegates to :func:`_terminate_popen` outside the lock so that the
        monitor thread is never blocked waiting for a lock held by this method.
        Safe to call when no process is running.
        """
        with self._process_lock:
            process: subprocess.Popen[bytes] | None = self._process

        if process is None:
            return

        _logger.debug(
            "Terminating cloudflared process",
            extra={"pid": process.pid},
        )
        _terminate_popen(process, timeout=_PROCESS_TERMINATE_TIMEOUT)

    def _build_command(self) -> list[str]:
        """Construct the cloudflared argument list for this tunnel.

        Returns
        -------
        list[str]
            A list suitable for :class:`subprocess.Popen`.

        Raises
        ------
        AssertionError
            If called before :meth:`start` has resolved the binary path.
        """
        assert self._cloudflared_path is not None, (
            "_build_command() called before _cloudflared_path was set; "
            "ensure start() is called before the monitor thread is spawned."
        )
        local_url: str = f"http://{self._host}:{self._port}"
        return [
            str(self._cloudflared_path),
            _CLOUDFLARED_SUBCOMMAND,
            _CLOUDFLARED_FLAG_NO_UPDATE,
            _CLOUDFLARED_FLAG_URL,
            local_url,
        ]

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        with self._state_lock:
            state_name: str = self._state.name
            url: str = self._public_url or "none"
        return (
            f"<TunnelManager"
            f" {self._host}:{self._port}"
            f" state={state_name}"
            f" url={url!r}"
            f">"
        )

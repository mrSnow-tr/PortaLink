"""
sharelink.download
~~~~~~~~~~~~~~~~~~

Download handler for the sharelink package.

Responsible exclusively for resolving, measuring, and streaming share content
to HTTP clients.  Supports three source types:

* ``LOCAL_FILE``         Streamed directly from the local filesystem with full
                         HTTP Range (resume) support.
* ``LOCAL_DIRECTORY``    Packaged as a ZIP archive on the fly and streamed
                         without buffering the entire archive in memory.  Byte-
                         range slicing is applied over the generated ZIP stream.
* ``FTP``                Proxied from a remote FTP server using the ``REST`` /
                         ``RETR`` mechanism for byte-level resume support.

All public entry points are thread-safe.  A single :class:`DownloadHandler`
instance can serve thousands of concurrent clients without contention.

Architecture
------------
``DownloadHandler``
    The single public class.  Satisfies the ``DownloadHandlerProtocol``
    structural contract defined in ``server.py``.  Delegates to private
    module-level streaming helpers and owns a :class:`_DownloadTracker` for
    real-time monitoring.

``_DownloadTracker``
    Thread-safe registry of active sessions, per-transfer speed measurements,
    and cumulative bandwidth counters.

``_DownloadSession``
    Mutable per-download state (session ID, byte counter, rolling speed window)
    maintained exclusively by :class:`_DownloadTracker`.

Streaming helpers (module-level, private)
    ``_stream_local_file``, ``_stream_directory_zip``, ``_stream_ftp``  
    generator functions that yield raw byte chunks for each source type.

    ``_get_local_file_size``, ``_get_ftp_file_size``  
    point-in-time helpers that query resource sizes without modifying state.

    ``_ftp_connect``   establishes an authenticated ``ftplib.FTP`` session
    from the parameters stored in a :class:`~models.ShareInfo`.

Directory ZIP streaming
-----------------------
A dedicated daemon thread writes the archive into the write end of an
``os.pipe``; the generator reads from the read end in chunks.  The kernel pipe
buffer (~64 KiB on Linux) provides natural back-pressure so memory usage stays
bounded regardless of directory size.  When the generator is closed early
(client disconnect or end of requested range), ``stop_event`` signals the
writer to abort before its next file, and closing the read end of the pipe
delivers ``BrokenPipeError`` to the writer, terminating it promptly.

FTP resume
----------
The FTP ``REST`` command sets the server-side restart position to ``start``
before issuing ``RETR``.  The generator truncates output once ``end`` bytes
have been delivered and tears down both the data socket and the control
connection in the ``finally`` block regardless of how the transfer ended.
"""

from __future__ import annotations

import ftplib
import os
import threading
import time
import uuid
import zipfile
import urllib.request

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import ShareConfig
from .logger import get_download_logger
from .models import ShareInfo, SourceType
from .utils import guess_mime

_logger = get_download_logger()

__all__: list[str] = [
    "DownloadHandler",
    "DownloadSessionInfo",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SPEED_WINDOW_SECONDS: Final[float] = 5.0
"""Rolling time window (seconds) used to compute per-transfer throughput."""

_FTP_CONNECT_TIMEOUT: Final[float] = 30.0
"""Maximum seconds to wait when establishing an FTP control connection."""

_FTP_DATA_TIMEOUT: Final[float] = 60.0
"""Maximum seconds to wait for data from an open FTP data socket."""

_ZIP_COMPRESSION: Final[int] = zipfile.ZIP_STORED
"""Compression method for directory ZIP archives.

``ZIP_STORED`` avoids CPU overhead and, critically, allows the archive to be
streamed without knowing the total compressed size in advance.
``ZIP_DEFLATED`` would require the full compressed payload before the ZIP
central directory can be written, making on-the-fly streaming impractical."""

_ZIP_WRITER_JOIN_TIMEOUT: Final[float] = 5.0
"""Maximum seconds to wait for the ZIP writer daemon thread to exit cleanly
after the streaming generator is closed or exhausted."""


# ---------------------------------------------------------------------------
# Public monitoring snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadSessionInfo:
    """Immutable snapshot of one active download session.

    Returned by :meth:`DownloadHandler.get_active_sessions` for consumption
    by monitoring endpoints in ``dashboard.py`` and ``api.py``.

    Attributes
    ----------
    session_id:
        UUID-4 identifier unique to this download session.
    share_id:
        Identifier of the share being served in this session.
    bytes_transferred:
        Total bytes delivered to the remote client so far.
    speed_bps:
        Current throughput estimate in bytes per second, computed over a
        rolling :data:`_SPEED_WINDOW_SECONDS`-second window.
    elapsed_seconds:
        Seconds elapsed since this session began.
    """

    session_id: str
    share_id: str
    bytes_transferred: int
    speed_bps: float
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Internal per-download state
# ---------------------------------------------------------------------------


class _DownloadSession:
    """Mutable per-download state owned by :class:`_DownloadTracker`.

    All methods that mutate ``bytes_transferred`` or ``_speed_window`` must
    be called with the tracker's lock held to guarantee thread-safe updates.
    """

    __slots__ = (
        "session_id",
        "share_id",
        "started_at",
        "bytes_transferred",
        "_speed_window",
    )

    def __init__(self, session_id: str, share_id: str) -> None:
        self.session_id: str = session_id
        self.share_id: str = share_id
        self.started_at: float = time.monotonic()
        self.bytes_transferred: int = 0
        #: ``(monotonic_timestamp, byte_count)`` pairs for the speed window.
        self._speed_window: deque[tuple[float, int]] = deque()

    def add_bytes(self, count: int) -> None:
        """Record *count* bytes transferred and refresh the rolling speed window.

        **Must be called with the owning tracker's lock held.**
        """
        now = time.monotonic()
        self.bytes_transferred += count
        self._speed_window.append((now, count))
        cutoff = now - _SPEED_WINDOW_SECONDS
        while self._speed_window and self._speed_window[0][0] < cutoff:
            self._speed_window.popleft()

    def current_speed_bps(self) -> float:
        """Return bytes-per-second averaged over the rolling window.

        **Must be called with the owning tracker's lock held.**
        """
        if not self._speed_window:
            return 0.0
        now = time.monotonic()
        cutoff = now - _SPEED_WINDOW_SECONDS
        window_bytes = sum(b for t, b in self._speed_window if t >= cutoff)
        elapsed = min(now - self.started_at, _SPEED_WINDOW_SECONDS)
        return (window_bytes / elapsed) if elapsed > 0.0 else 0.0

    def to_info(self) -> DownloadSessionInfo:
        """Build an immutable monitoring snapshot of this session.

        **Must be called with the owning tracker's lock held.**
        """
        return DownloadSessionInfo(
            session_id=self.session_id,
            share_id=self.share_id,
            bytes_transferred=self.bytes_transferred,
            speed_bps=self.current_speed_bps(),
            elapsed_seconds=time.monotonic() - self.started_at,
        )


# ---------------------------------------------------------------------------
# Thread-safe active-download registry
# ---------------------------------------------------------------------------


class _DownloadTracker:
    """Thread-safe registry of active sessions and cumulative bandwidth metrics.

    A single instance is owned by :class:`DownloadHandler` and shared across
    all request-handler threads without additional locking by the caller.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _DownloadSession] = {}
        self._lock: threading.Lock = threading.Lock()
        self._total_bytes: int = 0

    def start(self, share_id: str) -> _DownloadSession:
        """Create, register, and return a new download session.

        Parameters
        ----------
        share_id:
            Identifier of the share about to be streamed.
        """
        session = _DownloadSession(
            session_id=str(uuid.uuid4()),
            share_id=share_id,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        _logger.debug(
            "Download session started",
            extra={"session_id": session.session_id, "share_id": share_id},
        )
        return session

    def record_bytes(self, session: _DownloadSession, count: int) -> None:
        """Record *count* bytes sent in *session* and update global totals."""
        with self._lock:
            session.add_bytes(count)
            self._total_bytes += count

    def finish(self, session: _DownloadSession) -> None:
        """Remove *session* from the active registry.

        Safe to call more than once; duplicate calls are silently discarded.
        """
        with self._lock:
            self._sessions.pop(session.session_id, None)
        _logger.debug(
            "Download session ended",
            extra={
                "session_id": session.session_id,
                "share_id": session.share_id,
                "bytes_transferred": session.bytes_transferred,
                "elapsed_seconds": round(time.monotonic() - session.started_at, 3),
            },
        )

    @property
    def active_count(self) -> int:
        """Number of download sessions currently in progress."""
        with self._lock:
            return len(self._sessions)

    @property
    def total_bytes_transferred(self) -> int:
        """Cumulative bytes delivered to all clients since this tracker was created."""
        with self._lock:
            return self._total_bytes

    def get_session_infos(self) -> list[DownloadSessionInfo]:
        """Return a thread-safe snapshot of all active sessions."""
        with self._lock:
            return [s.to_info() for s in self._sessions.values()]


# ---------------------------------------------------------------------------
# Local file helpers
# ---------------------------------------------------------------------------


def _get_local_file_size(path: Path) -> int | None:
    """Return the byte size of *path*, or *None* on any OS error.

    Parameters
    ----------
    path:
        Absolute path to the file to inspect.
    """
    try:
        return path.stat().st_size
    except OSError as exc:
        _logger.warning(
            "Cannot stat local file for size",
            extra={"path": str(path), "error": str(exc)},
        )
        return None


def _stream_local_file(
    path: Path,
    start: int,
    end: int | None,
    chunk_size: int,
) -> Iterator[bytes]:
    """Yield raw byte chunks from *path* within the inclusive range ``[start, end]``.

    Parameters
    ----------
    path:
        Absolute path to the file.
    start:
        Zero-based inclusive start byte offset.
    end:
        Zero-based inclusive end byte offset, or *None* to read until EOF.
    chunk_size:
        Maximum bytes per read call.

    Raises
    ------
    OSError
        If the file cannot be opened or a read fails.  Propagates to
        ``_stream_body`` in ``server.py``, which logs and handles it.
    """
    remaining: int | None = (end - start + 1) if end is not None else None

    with path.open("rb") as fh:
        if start:
            fh.seek(start)

        while True:
            to_read = chunk_size
            if remaining is not None:
                if remaining <= 0:
                    return
                to_read = min(chunk_size, remaining)

            chunk = fh.read(to_read)
            if not chunk:
                return

            yield chunk

            if remaining is not None:
                remaining -= len(chunk)


# ---------------------------------------------------------------------------
# Directory ZIP streaming helper
# ---------------------------------------------------------------------------


def _stream_directory_zip(
    path: Path,
    start: int,
    end: int | None,
    chunk_size: int,
) -> Iterator[bytes]:
    """Yield bytes of an on-the-fly ZIP archive assembled from *path*.

    A daemon thread builds the archive and pipes it to this generator via
    ``os.pipe``.  The byte-range ``[start, end]`` is applied by discarding
    leading bytes (``start``) and capping trailing output (``end``).

    Back-pressure
    -------------
    The kernel pipe buffer (~64 KiB on Linux) blocks the writer once full,
    keeping memory usage bounded to approximately the buffer size plus two
    ``chunk_size`` allocations regardless of directory size.

    Cancellation and clean-up
    -------------------------
    When the generator is closed early (client disconnects, triggering
    :class:`GeneratorExit`, or the byte budget is exhausted), ``stop_event``
    signals the writer thread to skip remaining files.  Closing the read end of
    the pipe delivers :class:`BrokenPipeError` to the writer on its next write,
    terminating it promptly.  :data:`_ZIP_WRITER_JOIN_TIMEOUT` bounds how long
    the clean-up phase waits for the writer thread to exit.

    Parameters
    ----------
    path:
        Absolute path to the directory to archive.
    start:
        Zero-based inclusive start byte offset applied to the ZIP byte stream.
    end:
        Zero-based inclusive end byte offset, or *None* for end-of-archive.
    chunk_size:
        Pipe read-buffer size in bytes.
    """
    stop_event = threading.Event()
    read_fd, write_fd = os.pipe()

    def _write_zip() -> None:
        try:
            with os.fdopen(write_fd, "wb") as wf:
                with zipfile.ZipFile(wf, "w", _ZIP_COMPRESSION) as zf:
                    for file_path in sorted(path.rglob("*")):
                        if stop_event.is_set():
                            break
                        if not file_path.is_file():
                            continue
                        try:
                            arc = file_path.relative_to(path)
                            zf.write(str(file_path), arcname=str(arc))
                        except OSError as exc:
                            _logger.warning(
                                "Skipping unreadable file in directory share",
                                extra={"file": str(file_path), "error": str(exc)},
                            )
        except BrokenPipeError:
            # Read end was closed; expected on client disconnection or range end.
            pass
        except Exception:
            _logger.exception(
                "Unhandled error in ZIP writer thread",
                extra={"directory": str(path)},
            )

    writer = threading.Thread(
        target=_write_zip,
        daemon=True,
        name="sharelink-zip-writer",
    )
    writer.start()

    pending_skip: int = start
    bytes_remaining: int | None = (end - start + 1) if end is not None else None

    with os.fdopen(read_fd, "rb") as rf:
        try:
            while True:
                chunk = rf.read(chunk_size)
                if not chunk:
                    break

                # Honour ``start`` by discarding leading bytes.
                if pending_skip:
                    skip = min(pending_skip, len(chunk))
                    chunk = chunk[skip:]
                    pending_skip -= skip
                    if not chunk:
                        continue

                # Honour ``end`` by capping to the remaining byte budget.
                if bytes_remaining is not None:
                    if bytes_remaining <= 0:
                        break
                    if len(chunk) > bytes_remaining:
                        chunk = chunk[:bytes_remaining]
                    bytes_remaining -= len(chunk)

                yield chunk

                if bytes_remaining is not None and bytes_remaining <= 0:
                    break

        except GeneratorExit:
            # Generator closed before natural exhaustion (e.g. client disconnected).
            pass
        finally:
            # Signal the writer to skip remaining files before the next iteration.
            stop_event.set()
            # The ``with`` block exit closes ``rf`` (and thus ``read_fd``), which
            # delivers BrokenPipeError to the writer on its next write() call.

    writer.join(timeout=_ZIP_WRITER_JOIN_TIMEOUT)
    if writer.is_alive():
        _logger.warning(
            "ZIP writer thread did not exit within timeout",
            extra={"directory": str(path), "timeout_s": _ZIP_WRITER_JOIN_TIMEOUT},
        )


# ---------------------------------------------------------------------------
# FTP helpers
# ---------------------------------------------------------------------------


def _ftp_connect(share: ShareInfo) -> ftplib.FTP:
    """Open and authenticate an ``ftplib.FTP`` session from *share*'s credentials.

    Parameters
    ----------
    share:
        Share whose ``ftp_host``, ``ftp_port``, ``ftp_username``,
        ``ftp_password``, and ``ftp_passive`` fields provide connection
        parameters.

    Returns
    -------
    ftplib.FTP
        A logged-in, mode-configured FTP session ready for commands.

    Raises
    ------
    ValueError
        If *share* has no FTP host configured.
    ftplib.all_errors
        On connection or authentication failure.
    """
    if not share.ftp_host:
        raise ValueError(
            f"ShareInfo {share.share_id!r} has source_type FTP "
            "but no ftp_host is configured."
        )
    ftp = ftplib.FTP()
    ftp.connect(
        host=share.ftp_host,
        port=share.ftp_port or 21,
        timeout=_FTP_CONNECT_TIMEOUT,
    )
    ftp.login(
        user=share.ftp_username or "anonymous",
        passwd=share.ftp_password or "anonymous@",
    )
    ftp.set_pasv(share.ftp_passive)
    return ftp


def _get_ftp_file_size(share: ShareInfo) -> int | None:
    """Return the byte size of the FTP resource described by *share*.

    Opens a brief FTP control connection, issues the ``SIZE`` command, then
    closes the connection.  Returns *None* on any error (server unreachable,
    file absent, server does not implement ``SIZE``, etc.).

    Parameters
    ----------
    share:
        Share describing the FTP resource to measure.
    """
    try:
        ftp = _ftp_connect(share)
        try:
            return ftp.size(share.source_path)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
    except Exception as exc:
        _logger.warning(
            "Cannot determine FTP file size",
            extra={
                "host": share.ftp_host,
                "path": share.source_path,
                "error": str(exc),
            },
        )
        return None


def _stream_ftp(
    share: ShareInfo,
    start: int,
    end: int | None,
    chunk_size: int,
) -> Iterator[bytes]:
    """Yield raw byte chunks from the FTP resource described by *share*.

    Resume support uses the FTP ``REST`` command to position the server's
    transfer pointer at *start* before issuing ``RETR``.  Output is truncated
    client-side once *end* bytes have been delivered, closing the data
    connection early if needed.  Both the data socket and the control
    connection are torn down in the ``finally`` block regardless of how the
    transfer ended.

    Parameters
    ----------
    share:
        Fully populated :class:`~models.ShareInfo` with FTP connection fields.
    start:
        Zero-based inclusive start byte offset.
    end:
        Zero-based inclusive end byte offset, or *None* to read until EOF.
    chunk_size:
        Maximum bytes per ``recv()`` call on the data socket.

    Raises
    ------
    ValueError
        If *share* has no FTP host configured.
    ftplib.all_errors
        On connection, authentication, or FTP protocol errors.
    OSError
        On network-level I/O errors during the data transfer.
    """
    ftp = _ftp_connect(share)
    data_conn = None

    try:
        if start:
            ftp.sendcmd(f"REST {start}")

        data_conn = ftp.transfercmd(f"RETR {share.source_path}")
        data_conn.settimeout(_FTP_DATA_TIMEOUT)

        remaining: int | None = (end - start + 1) if end is not None else None

        while True:
            to_recv = chunk_size
            if remaining is not None:
                if remaining <= 0:
                    break
                to_recv = min(chunk_size, remaining)

            chunk = data_conn.recv(to_recv)
            if not chunk:
                break

            yield chunk

            if remaining is not None:
                remaining -= len(chunk)

    finally:
        if data_conn is not None:
            try:
                data_conn.close()
            except Exception:
                pass
        # Read the server's transfer-complete or transfer-aborted response so
        # the control connection remains in a clean state before QUIT.
        try:
            ftp.voidresp()
        except Exception:
            pass
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def _http_request(
    url: str,
    start: int = 0,
    end: int | None = None,
    method: str = "GET",
):
    """
    Open an HTTP/HTTPS upstream resource.

    A Range header is sent when start/end are supplied.
    Redirects are handled automatically by urllib.
    """

    headers = {
        "User-Agent": "Sharelink/1.0",
        "Accept": "*/*",
    }

    if start or end is not None:
        if end is None:
            headers["Range"] = f"bytes={start}-"
        else:
            headers["Range"] = f"bytes={start}-{end}"

    request = urllib.request.Request(
        url,
        headers=headers,
        method=method,
    )

    return urllib.request.urlopen(
        request,
        timeout=60,
    )


def _http_content_length(url: str) -> int | None:
    """
    Determine the upstream HTTP/HTTPS resource size.

    First tries HEAD.

    If HEAD does not provide Content-Length, performs a
    one-byte Range request and extracts the total size from
    Content-Range.
    """

    # ---------------------------------------------------------
    # Try HEAD first
    # ---------------------------------------------------------

    try:
        response = _http_request(
            url,
            method="HEAD",
        )

        try:
            value = response.headers.get("Content-Length")

            if value:
                return int(value)

        finally:
            response.close()

    except Exception:
        pass

    # ---------------------------------------------------------
    # Fallback: GET bytes=0-0
    # ---------------------------------------------------------

    try:
        response = _http_request(
            url,
            start=0,
            end=0,
            method="GET",
        )

        try:
            content_range = response.headers.get(
                "Content-Range"
            )

            if content_range:
                # Example:
                # bytes 0-0/123456

                total = content_range.rsplit("/", 1)[-1]

                if total != "*":
                    return int(total)

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:
                # If server ignored Range and returned 200,
                # this is the full resource size.
                return int(content_length)

        finally:
            response.close()

    except Exception as exc:
        _logger.warning(
            "Unable to determine HTTP resource size",
            extra={
                "url": url,
                "error": str(exc),
            },
        )

    return None


def _stream_http(
    url: str,
    start: int,
    end: int | None,
    chunk_size: int,
) -> Iterator[bytes]:
    """
    Stream an HTTP/HTTPS resource.

    Supports:

        bytes=0-
        bytes=1000-
        bytes=1000-5000

    If the upstream server supports Range, the range is passed
    directly to it.

    If the upstream server ignores Range and returns 200,
    Sharelink discards the unwanted leading bytes locally.
    """

    response = _http_request(
        url,
        start=start,
        end=end,
        method="GET",
    )

    try:
        status = getattr(
            response,
            "status",
            None,
        )

        # -----------------------------------------------------
        # Determine whether upstream honored Range
        # -----------------------------------------------------

        range_honored = status == 206

        remaining = (
            end - start + 1
            if end is not None
            else None
        )

        # -----------------------------------------------------
        # Upstream ignored Range.
        #
        # We need to discard `start` bytes ourselves.
        # -----------------------------------------------------

        if start and not range_honored:

            to_discard = start

            while to_discard > 0:

                chunk = response.read(
                    min(
                        chunk_size,
                        to_discard,
                    )
                )

                if not chunk:
                    return

                to_discard -= len(chunk)

        # -----------------------------------------------------
        # Stream requested bytes
        # -----------------------------------------------------

        while True:

            read_size = chunk_size

            if remaining is not None:

                if remaining <= 0:
                    return

                read_size = min(
                    read_size,
                    remaining,
                )

            chunk = response.read(
                read_size
            )

            if not chunk:
                return

            yield chunk

            if remaining is not None:
                remaining -= len(chunk)

    finally:
        response.close()
#---------------------------------------------------------------------------
# Public download handler
# ---------------------------------------------------------------------------


class DownloadHandler:
    """Resolves, measures, and streams share content to HTTP clients.

    Satisfies the ``DownloadHandlerProtocol`` structural contract defined in
    ``server.py``.  Thread-safe; a single instance handles all concurrent
    requests without external locking.

    Scope
    -----
    This class is responsible exclusively for:

    * Determining resource sizes for ``Content-Length`` response headers.
    * Streaming byte ranges from local files, directories, and FTP servers.
    * Tracking active sessions, per-transfer throughput, and bandwidth totals.
    * Resolving the effective content type and download filename from share
      metadata (used by the session layer at share-creation time).

    Share state mutations, expiry enforcement, and download counting are the
    responsibility of ``session.py`` and ``manager.py``, not this class.

    Parameters
    ----------
    config:
        Package configuration snapshot.  Defaults to
        :class:`~config.ShareConfig` with all default values when *None*.

    Examples
    --------
    ::

        handler = DownloadHandler()

        # Called by server.py before sending response headers:
        length = handler.get_content_length(share)   # int | None

        # Called by server.py to produce the response body:
        for chunk in handler.stream_range(share, start=0, end=None):
            wfile.write(chunk)

        # Real-time monitoring:
        print(handler.active_download_count)
        for info in handler.get_active_sessions():
            print(info.share_id, info.speed_bps)
    """

    def __init__(self, config: ShareConfig | None = None) -> None:
        self._config: ShareConfig = config or ShareConfig()
        self._tracker: _DownloadTracker = _DownloadTracker()

    # ------------------------------------------------------------------
    # DownloadHandlerProtocol interface (consumed by server.py)
    # ------------------------------------------------------------------

    def get_content_length(self, share: ShareInfo) -> int | None:
        """Return the total byte length of *share*'s resource, or *None*.

        The server uses this value to populate the ``Content-Length`` header
        and decide whether chunked transfer encoding is required.

        Resolution order
        ----------------
        1. ``share.file_size``   used directly when already set by the manager
           layer at share-creation time, avoiding redundant I/O per request.
        2. ``os.stat()``  1 for :attr:`~models.SourceType.LOCAL_FILE` shares.
        3. FTP ``SIZE`` command   for :attr:`~models.SourceType.FTP` shares
           (requires a brief control connection).
        4. *None*  for :attr:`~models.SourceType.LOCAL_DIRECTORY`; the ZIP
           archive size is unknown until the archive is fully generated.

        Parameters
        ----------
        share:
            Share whose resource size is required.
        """
        if share.file_size is not None:
        	return share.file_size
        source_type = share.source_type
        if source_type == SourceType.LOCAL_FILE:
        	return _get_local_file_size(Path(share.source_path))
        if source_type == SourceType.LOCAL_DIRECTORY:
        	return None
        if source_type == SourceType.FTP:
        	return _get_ftp_file_size(share)
        if source_type == SourceType.HTTP:
        	return _http_content_length(share.source_path)
        _logger.warning(
        "Unrecognised source type in get_content_length; "
        "returning None",
        extra={
        "share_id": share.share_id,
        "source_type": str(share.source_type),},)
        return None

    def stream_range(
        self,
        share: ShareInfo,
        start: int,
        end: int | None,
    ) -> Iterator[bytes]:
        """Yield raw byte chunks for the inclusive byte range ``[start, end]``.

        Registers the download with :class:`_DownloadTracker` before streaming
        begins and always de-registers it via a ``finally`` block, ensuring
        accurate active-session counts even when a client disconnects mid-
        transfer.  Per-chunk byte counts are recorded for speed tracking.

        Parameters
        ----------
        share:
            Fully populated :class:`~models.ShareInfo` describing the resource.
        start:
            Zero-based inclusive start byte offset.
        end:
            Zero-based inclusive end byte offset, or *None* for end-of-resource.

        Raises
        ------
        ValueError
            If *share* has an unsupported or misconfigured source type.
        OSError
            If a local file cannot be opened or read.  Propagates to
            ``server.py``'s ``_stream_body``, which logs and handles it.
        ftplib.all_errors
            On FTP connection or transfer failure.

        Yields
        ------
        bytes
            Non-empty byte chunks from the resource in sequential order.
        """
        session = self._tracker.start(share.share_id)
        try:
            for chunk in self._resolve_iterator(share, start, end):
                self._tracker.record_bytes(session, len(chunk))
                yield chunk
        finally:
            self._tracker.finish(session)

    # ------------------------------------------------------------------
    # Content-type and filename resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_content_type(share: ShareInfo) -> str:
        """Return the effective MIME content type for *share*'s resource.

        Resolution priority:

        1. ``share.content_type``   explicit override set at creation time.
        2. ``"application/zip"``   for :attr:`~models.SourceType.LOCAL_DIRECTORY`
           shares (served as ZIP archives).
        3. MIME guess derived from the file extension of ``share.source_path``.
        4. ``"application/octet-stream"``   universal fallback from
           :func:`~utils.guess_mime`.

        Parameters
        ----------
        share:
            Share whose content type is needed.

        Returns
        -------
        str
            A non-empty MIME type string.
        """
        if share.content_type:
            return share.content_type
        if share.source_type == SourceType.LOCAL_DIRECTORY:
            return "application/zip"
        return guess_mime(share.source_path)

    @staticmethod
    def resolve_filename(share: ShareInfo) -> str:
        """Return the suggested download filename for *share*.

        Resolution priority:

        1. ``share.display_name``   explicit override set at creation time.
        2. ``<directory_name>.zip``   for :attr:`~models.SourceType.LOCAL_DIRECTORY`
           shares.
        3. Basename of ``share.source_path``.
        4. ``"download"``   fallback when the path yields no recognisable basename.

        Parameters
        ----------
        share:
            Share whose download filename is needed.

        Returns
        -------
        str
            A non-empty filename string suitable for a ``Content-Disposition``
            header.
        """
        if share.display_name:
            return share.display_name
        source = Path(share.source_path)
        if share.source_type == SourceType.LOCAL_DIRECTORY:
            return (source.name or "archive") + ".zip"
        return source.name or "download"

    # ------------------------------------------------------------------
    # Monitoring interface
    # ------------------------------------------------------------------

    @property
    def active_download_count(self) -> int:
        """Number of download sessions currently in progress."""
        return self._tracker.active_count

    @property
    def total_bytes_transferred(self) -> int:
        """Cumulative bytes delivered to all clients since this handler was created."""
        return self._tracker.total_bytes_transferred

    def get_active_sessions(self) -> list[DownloadSessionInfo]:
        """Return an immutable snapshot of all in-progress download sessions.

        The returned list is a copy; modifying it has no effect on internal
        tracker state.

        Returns
        -------
        list[DownloadSessionInfo]
            Zero or more monitoring snapshots, one per active download.
        """
        return self._tracker.get_session_infos()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_iterator(
        self,
        share: ShareInfo,
        start: int,
        end: int | None,
    ) -> Iterator[bytes]:
        """Dispatch to the appropriate streaming helper for *share*'s source type.

        Parameters
        ----------
        share:
            Describes the resource to stream.
        start:
            Zero-based inclusive start byte offset.
        end:
            Zero-based inclusive end byte offset, or *None* for end-of-resource.

        Raises
        ------
        ValueError
            If *share.source_type* is not a recognised :class:`~models.SourceType`.
        """
        chunk_size = self._config.chunk_size
        source_type = share.source_type

        if source_type == SourceType.LOCAL_FILE:
            return _stream_local_file(Path(share.source_path), start, end, chunk_size)

        if source_type == SourceType.LOCAL_DIRECTORY:
            return _stream_directory_zip(Path(share.source_path), start, end, chunk_size)

        if source_type == SourceType.HTTP:
        	return _stream_http(share.source_path, start, end, chunk_size)

    
        if source_type == SourceType.FTP:
            return _stream_ftp(share, start, end, chunk_size)

        raise ValueError(
            f"Unsupported source_type {source_type!r} for share {share.share_id!r}."
        )

    def __repr__(self) -> str:
        return (
            f"<DownloadHandler"
            f" active={self.active_download_count}"
            f" total_bytes={self.total_bytes_transferred}"
            f">"
        )

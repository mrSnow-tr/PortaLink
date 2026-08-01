"""
sharelink.server
~~~~~~~~~~~~~~~~

HTTP server providing routing, download endpoints with HTTP Range support,
and REST API endpoints for the sharelink package.

This module is intentionally decoupled from:

- Cloudflare Tunnel integration  (→ tunnel.py)
- Dashboard HTML rendering       (→ dashboard.py)
- Share lifecycle management     (→ manager.py)
- Session tracking               (→ session.py)

Architecture
------------
``ShareLinkServer``
    Public-facing class that owns the server lifecycle (start / stop).

``_ThreadedHTTPServer``
    Extends ``socketserver.ThreadingMixIn`` and ``http.server.HTTPServer``
    so that every HTTP connection is served in its own daemon thread,
    supporting thousands of concurrent downloads without blocking.

``_ShareLinkHandler``
    Per-request HTTP handler.  All mutable dependencies (share_manager,
    download_handler, dashboard_handler) are read from ``self.server``,
    the ``_ThreadedHTTPServer`` instance, so no global state is required.

``_Router``
    Immutable, regex-based URL router compiled once at class-definition
    time and shared safely across all request-handler threads.

Protocols
---------
``ShareInfoProtocol``, ``ShareManagerProtocol``, ``DownloadHandlerProtocol``,
and ``DashboardHandlerProtocol`` define the structural contracts that
collaborating modules must satisfy.  No concrete implementations are
imported here; future modules fulfil the protocols via duck typing.

HTTP Endpoints
--------------
GET    /d/<share_id>           Download a file (Range request supported).
HEAD   /d/<share_id>           Return metadata headers, no body.
GET    /api/shares             List all active shares (JSON).
GET    /api/shares/<share_id>  Retrieve share details (JSON).
DELETE /api/shares/<share_id>  Revoke a share (JSON 204).
GET    /api/status             Server health and status (JSON).
GET    /                       Dashboard (delegates to DashboardHandlerProtocol).
GET    /dashboard              Dashboard (delegates to DashboardHandlerProtocol).
"""

from __future__ import annotations

import http.server
import json
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol, runtime_checkable

from .logger import get_logger

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Byte size of each chunk yielded to the client during streaming.
_CHUNK_SIZE: int = 65_536  # 64 KiB

#: Value for the ``Server`` HTTP response header.
_SERVER_NAME: str = "sharelink/1.0"

#: Per-connection socket read/write timeout in seconds.
#: Prevents slow-client and slow-loris style attacks from tying up threads.
_SOCKET_TIMEOUT: float = 30.0

#: Maximum seconds to wait for the server background thread to exit cleanly.
_SHUTDOWN_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Protocols – structural contracts expected from collaborating modules
# ---------------------------------------------------------------------------


@runtime_checkable
class ShareInfoProtocol(Protocol):
    """Read-only interface the HTTP layer requires from a share object.

    Concrete implementations live in models.py / manager.py.  The server
    never creates or mutates share objects; it only reads them.
    """

    @property
    def share_id(self) -> str:
        """Unique identifier embedded in the public download URL."""
        ...

    @property
    def is_expired(self) -> bool:
        """``True`` when the share's time-to-live has elapsed."""
        ...

    @property
    def is_exhausted(self) -> bool:
        """``True`` when the maximum permitted download count is reached."""
        ...

    @property
    def file_size(self) -> int | None:
        """Total resource size in bytes, or *None* if not known in advance."""
        ...

    @property
    def filename(self) -> str:
        """Suggested download filename sent via ``Content-Disposition``."""
        ...

    @property
    def content_type(self) -> str:
        """MIME type string for the ``Content-Type`` response header."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the share."""
        ...


class ShareManagerProtocol(Protocol):
    """Interface the HTTP server uses to query and mutate shares.

    Implemented by manager.py.  All methods must be thread-safe because
    they are called concurrently from multiple request-handler threads.
    """

    def get_share(self, share_id: str) -> ShareInfoProtocol | None:
        """Return the share for *share_id*, or *None* if not found."""
        ...

    def list_shares(self) -> list[ShareInfoProtocol]:
        """Return all currently active (non-expired) shares."""
        ...

    def delete_share(self, share_id: str) -> bool:
        """Revoke *share_id*.  Returns ``True`` if the share existed."""
        ...

    def record_download_start(self, share_id: str) -> None:
        """Notify the manager that a download of *share_id* has begun."""
        ...

    def record_download_complete(self, share_id: str) -> None:
        """Notify the manager that a download of *share_id* has finished."""
        ...


class DownloadHandlerProtocol(Protocol):
    """Interface the HTTP server uses to resolve and stream share content.

    Implemented by download.py.  Supports both local files and FTP resources.
    """

    def get_content_length(self, share: ShareInfoProtocol) -> int | None:
        """Return the total byte length of the resource, or *None* if unknown.

        Returning *None* triggers chunked transfer encoding on the response.
        """
        ...

    def stream_range(
        self,
        share: ShareInfoProtocol,
        start: int,
        end: int | None,
    ) -> Iterator[bytes]:
        """Yield raw byte chunks for the specified byte range.

        Parameters
        ----------
        share:
            Describes the resource to stream.
        start:
            First byte offset (inclusive, 0-based).
        end:
            Last byte offset (inclusive), or *None* meaning end-of-resource.
        """
        ...


class DashboardHandlerProtocol(Protocol):
    """Interface for dashboard HTML generation.

    Implemented by dashboard.py.  The server calls ``render()`` on each
    dashboard request; caching is the dashboard handler's responsibility.
    """

    def render(self) -> tuple[bytes, str]:
        """Return *(html_bytes, content_type)* for the dashboard page."""
        ...


# ---------------------------------------------------------------------------
# Internal data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedRange:
    """A validated HTTP byte-range extracted from a ``Range`` request header."""

    start: int
    """First byte offset, inclusive, 0-based."""

    end: int | None
    """Last byte offset, inclusive.  *None* means stream to end-of-resource."""


@dataclass(frozen=True, slots=True)
class _RouteMatch:
    """Result returned by ``_Router.match`` when a URL path matches a rule."""

    handler_method: str
    """Name of the method to invoke on the request handler instance."""

    params: dict[str, str]
    """Named path parameters extracted from the URL by the regex."""


class _RangeNotSatisfiable(Exception):
    """Raised when the requested Range start offset exceeds the resource size."""

    def __init__(self, content_length: int) -> None:
        self.content_length = content_length
        super().__init__(
            f"Range not satisfiable for a resource of {content_length} bytes."
        )


# ---------------------------------------------------------------------------
# URL router
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Route:
    """A single compiled URL routing rule."""

    method: str
    pattern: re.Pattern[str]
    handler_method: str

    def match(self, method: str, path: str) -> _RouteMatch | None:
        """Return a ``_RouteMatch`` when *method* and *path* both match."""
        if self.method != method:
            return None
        m = self.pattern.fullmatch(path)
        if m is None:
            return None
        return _RouteMatch(handler_method=self.handler_method, params=m.groupdict())


class _Router:
    """Immutable, thread-safe regex URL router.

    URL patterns use ``<name>`` placeholders, which capture any sequence of
    characters that does not contain a forward slash.  The router is compiled
    once at class-definition time and is safe to share across threads.

    Example
    -------
    >>> router = _Router()
    >>> match = router.match("GET", "/d/abc-123")
    >>> assert match is not None
    >>> assert match.handler_method == "_handle_download"
    >>> assert match.params == {"share_id": "abc-123"}
    """

    _PARAM_RE: re.Pattern[str] = re.compile(r"<([^>]+)>")

    # Ordered list of routing rules evaluated top-to-bottom.
    # More specific patterns should appear before general ones.
    _ROUTING_TABLE: tuple[tuple[str, str, str], ...] = (
        # (HTTP method,  URL pattern,                   handler method name)
        ("GET",    "/d/<share_id>",             "_handle_download"),
        ("HEAD",   "/d/<share_id>",             "_handle_download_head"),
        ("GET",    "/api/shares/<share_id>",    "_handle_api_share_get"),
        ("DELETE", "/api/shares/<share_id>",    "_handle_api_share_delete"),
        ("GET",    "/api/shares",               "_handle_api_shares_list"),
        ("GET",    "/api/status",               "_handle_api_status"),
        ("GET",    "/dashboard",                "_handle_dashboard"),
        ("GET",    "/",                         "_handle_dashboard"),
    )

    def __init__(self) -> None:
        self._routes: tuple[_Route, ...] = tuple(
            _Route(
                method=method,
                pattern=re.compile(self._build_pattern(url_pattern)),
                handler_method=handler_method,
            )
            for method, url_pattern, handler_method in self._ROUTING_TABLE
        )

    @classmethod
    def _build_pattern(cls, url_pattern: str) -> str:
        """Convert a ``/path/<name>`` pattern into a named-group regex string.

        Literal path segments are regex-escaped; ``<name>`` placeholders
        become ``(?P<name>[^/]+)`` named capture groups.
        """
        segments: list[str] = []
        parts = cls._PARAM_RE.split(url_pattern)
        for index, part in enumerate(parts):
            if index % 2 == 0:
                # Literal segment: escape all regex metacharacters.
                segments.append(re.escape(part))
            else:
                # Capture group name from the placeholder.
                segments.append(f"(?P<{part}>[^/]+)")
        return "".join(segments)

    def match(self, method: str, path: str) -> _RouteMatch | None:
        """Return the first matching ``_RouteMatch``, or *None*."""
        for route in self._routes:
            result = route.match(method, path)
            if result is not None:
                return result
        return None


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class _ShareLinkHandler(http.server.BaseHTTPRequestHandler):
    """Per-request HTTP handler for the sharelink service.

    One instance is created per HTTP connection by ``_ThreadedHTTPServer``.
    All injected dependencies are read from ``self.server`` (the server
    instance) so no class-level mutable state is ever required.

    HTTP/1.1 is declared so that chunked transfer encoding is available for
    resources whose size cannot be determined in advance (live FTP streams).
    """

    # Tell mypy that self.server is our custom subclass.
    server: "_ThreadedHTTPServer"  # type: ignore[assignment]

    protocol_version: str = "HTTP/1.1"

    # Shared, immutable router built once at class-definition time.
    _router: _Router = _Router()

    # ------------------------------------------------------------------
    # BaseHTTPRequestHandler dispatch entry points
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        """Dispatch an HTTP GET request."""
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        """Dispatch an HTTP HEAD request."""
        self._dispatch("HEAD")

    def do_DELETE(self) -> None:  # noqa: N802
        """Dispatch an HTTP DELETE request."""
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Handle CORS preflight OPTIONS requests."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Allow", "GET, HEAD, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self._add_cors_headers()
        self.end_headers()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        """Parse the URL path and invoke the matching handler method.

        Strips trailing slashes before matching (``/d/abc/`` → ``/d/abc``),
        preserving the bare slash for the root dashboard route.  Unmatched
        paths receive a ``404`` JSON response.  Any exception raised after
        the route is resolved is caught here; if headers have not yet been
        sent, a ``500`` JSON response is returned.  If headers were already
        sent, the connection is closed ungracefully.
        """
        parsed = urllib.parse.urlparse(self.path)
        path: str = parsed.path.rstrip("/") or "/"

        match = self._router.match(method, path)
        if match is None:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"No route for {method} {path}.",
            )
            return

        try:
            handler = getattr(self, match.handler_method)
            handler(**match.params)
        except Exception:
            _logger.exception("Unhandled error in %s %s", method, path)
            try:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "An internal server error occurred.",
                )
            except Exception:
                # Headers already sent; close the connection ungracefully.
                self.close_connection = True

    # ------------------------------------------------------------------
    # Download endpoints
    # ------------------------------------------------------------------

    def _handle_download(self, share_id: str) -> None:
        """Stream a share's content, honouring the ``Range`` request header.

        Response codes:

        - ``200 OK``                       – full resource (no Range header).
        - ``206 Partial Content``          – valid byte range requested.
        - ``410 Gone``                     – share expired or exhausted.
        - ``416 Range Not Satisfiable``    – Range start ≥ content length.
        - ``503 Service Unavailable``      – resource temporarily inaccessible.

        Uses chunked transfer encoding when content length is unknown.
        Supports ``bytes=start-end``, ``bytes=start-``, and ``bytes=-N``
        range formats; multi-range requests fall back to a full ``200``.
        """
        share = self._resolve_active_share(share_id)
        if share is None:
            return

        try:
            content_length = self.server.download_handler.get_content_length(share)
        except Exception:
            _logger.exception(
                "Failed to determine content length for share %s.", share_id
            )
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The resource is temporarily unavailable.",
            )
            return

        try:
            parsed_range = self._parse_range_header(content_length)
        except _RangeNotSatisfiable as exc:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Server", _SERVER_NAME)
            self.send_header("Content-Range", f"bytes */{exc.content_length}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        is_ranged = parsed_range is not None
        start: int = parsed_range.start if is_ranged else 0
        end: int | None = parsed_range.end if is_ranged else None

        # Determine response status, Content-Range, and Content-Length.
        status: HTTPStatus
        content_range_value: str | None = None
        response_length: int | None

        if is_ranged:
            status = HTTPStatus.PARTIAL_CONTENT
            if content_length is not None:
                # Full information available: canonical Content-Range.
                effective_end = end if end is not None else content_length - 1
                content_range_value = (
                    f"bytes {start}-{effective_end}/{content_length}"
                )
                response_length = effective_end - start + 1
            elif end is not None:
                # Total size unknown but explicit end requested.
                content_range_value = f"bytes {start}-{end}/*"
                response_length = end - start + 1
            else:
                # Cannot serve a range without a known end; fall back to 200.
                is_ranged = False
                status = HTTPStatus.OK
                response_length = None
        else:
            status = HTTPStatus.OK
            response_length = content_length

        use_chunked = response_length is None

        self.send_response(status)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Content-Type", share.content_type)
        self.send_header(
            "Content-Disposition",
            _build_content_disposition(share.filename),
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")

        if content_range_value is not None:
            self.send_header("Content-Range", content_range_value)

        if use_chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(response_length))

        self._add_cors_headers()
        self.end_headers()

        self._stream_body(share, start=start, end=end, chunked=use_chunked)

    def _handle_download_head(self, share_id: str) -> None:
        """Return the response headers for a share without writing a body.

        Mirrors the headers that a GET request would return, allowing
        clients to inspect content type, length, and range support before
        committing to a full download.
        """
        share = self._resolve_active_share(share_id)
        if share is None:
            return

        try:
            content_length = self.server.download_handler.get_content_length(share)
        except Exception:
            _logger.exception(
                "Failed to determine content length for share %s.", share_id
            )
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The resource is temporarily unavailable.",
            )
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Content-Type", share.content_type)
        self.send_header(
            "Content-Disposition",
            _build_content_disposition(share.filename),
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self._add_cors_headers()
        self.end_headers()
        # HEAD: no body is written.

    # ------------------------------------------------------------------
    # REST API endpoints
    # ------------------------------------------------------------------

    def _handle_api_shares_list(self) -> None:
        """Return a JSON object containing all active shares."""
        shares = self.server.share_manager.list_shares()
        self._send_json(
            {
                "shares": [s.to_dict() for s in shares],
                "count": len(shares),
            }
        )

    def _handle_api_share_get(self, share_id: str) -> None:
        """Return JSON details for a single share, or ``404`` if absent."""
        share = self.server.share_manager.get_share(share_id)
        if share is None:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"Share '{share_id}' not found.",
            )
            return
        self._send_json(share.to_dict())

    def _handle_api_share_delete(self, share_id: str) -> None:
        """Revoke a share and respond with ``204 No Content``."""
        deleted = self.server.share_manager.delete_share(share_id)
        if not deleted:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"Share '{share_id}' not found.",
            )
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self._add_cors_headers()
        self.end_headers()

    def _handle_api_status(self) -> None:
        """Return a JSON health and status document."""
        shares = self.server.share_manager.list_shares()
        self._send_json(
            {
                "status": "ok",
                "server": _SERVER_NAME,
                "timestamp": time.time(),
                "active_shares": len(shares),
            }
        )

    def _handle_dashboard(self) -> None:
        """Delegate dashboard rendering to the injected dashboard handler.

        Returns ``503`` when no dashboard handler has been configured.
        """
        handler = self.server.dashboard_handler
        if handler is None:
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Dashboard is not configured on this server.",
            )
            return

        body, content_type = handler.render()
        self.send_response(HTTPStatus.OK)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_active_share(self, share_id: str) -> ShareInfoProtocol | None:
        """Look up *share_id* and enforce expiry and download-limit guards.

        Sends a ``404`` or ``410`` error response and returns *None* when the
        share is unavailable, so the caller can return immediately without
        writing a second response.
        """
        share = self.server.share_manager.get_share(share_id)
        if share is None:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"Share '{share_id}' not found.",
            )
            return None

        if share.is_expired:
            self._send_error_json(
                HTTPStatus.GONE,
                "This download link has expired.",
            )
            return None

        if share.is_exhausted:
            self._send_error_json(
                HTTPStatus.GONE,
                "This download link has reached its maximum download count.",
            )
            return None

        return share

    def _parse_range_header(self, content_length: int | None) -> _ParsedRange | None:
        """Parse the ``Range`` request header into a ``_ParsedRange``.

        Returns *None* when the header is absent or cannot be understood,
        signalling the caller to serve a full ``200`` response instead.

        Supported single-range formats:

        ``bytes=start-end``
            Explicit inclusive start and end offsets.
        ``bytes=start-``
            From *start* to end-of-resource.
        ``bytes=-N``
            The last *N* bytes (requires *content_length* to be known).

        Multi-range requests are intentionally not supported and fall back
        to a full ``200`` response.

        Raises
        ------
        _RangeNotSatisfiable
            When the *start* offset meets or exceeds a known *content_length*.
        """
        raw_header = self.headers.get("Range", "").strip()
        if not raw_header:
            return None

        # Only single-range byte ranges are honoured.
        if not raw_header.startswith("bytes=") or "," in raw_header:
            return None

        range_spec = raw_header[len("bytes="):].strip()
        dash_index = range_spec.find("-")
        if dash_index == -1:
            return None

        raw_start = range_spec[:dash_index]
        raw_end = range_spec[dash_index + 1:]

        try:
            if raw_start == "":
                # bytes=-N → last N bytes.
                if content_length is None:
                    # Cannot resolve a suffix range without a known length.
                    return None
                suffix = int(raw_end)
                if suffix <= 0:
                    return None
                start = max(0, content_length - suffix)
                return _ParsedRange(start=start, end=content_length - 1)

            start = int(raw_start)
            if start < 0:
                return None

            end: int | None = int(raw_end) if raw_end.strip() else None

            if content_length is not None:
                if start >= content_length:
                    raise _RangeNotSatisfiable(content_length)
                if end is not None:
                    end = min(end, content_length - 1)

            if end is not None and end < start:
                return None

            return _ParsedRange(start=start, end=end)

        except ValueError:
            return None

    def _stream_body(
        self,
        share: ShareInfoProtocol,
        *,
        start: int,
        end: int | None,
        chunked: bool,
    ) -> None:
        """Write the response body by consuming the download handler's iterator.

        Notifies the share manager when the transfer begins and when it ends
        (via a ``finally`` block so the event is always recorded).  Broken
        client connections and I/O errors are handled gracefully without
        raising to the caller.

        Parameters
        ----------
        share:
            The share being served; used for manager notifications.
        start:
            First byte offset forwarded to the download handler.
        end:
            Last byte offset (inclusive), or *None* for end-of-resource.
        chunked:
            When ``True``, each chunk is wrapped in HTTP/1.1 chunked encoding
            framing before being written to the socket.
        """
        self.server.share_manager.record_download_start(share.share_id)
        try:
            iterator = self.server.download_handler.stream_range(share, start, end)
            for chunk in iterator:
                if not chunk:
                    continue
                if chunked:
                    size_header = f"{len(chunk):X}\r\n".encode("ascii")
                    self.wfile.write(size_header)
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)

            if chunked:
                # Chunked terminator.
                self.wfile.write(b"0\r\n\r\n")

            self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            _logger.debug(
                "Client disconnected during download of share %s.", share.share_id
            )
        except OSError:
            _logger.exception(
                "I/O error while streaming share %s.", share.share_id
            )
        finally:
            self.server.share_manager.record_download_complete(share.share_id)

    def _send_json(
        self,
        data: Any,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Serialise *data* to JSON and write a complete HTTP response.

        Uses ``default=str`` so that non-serialisable types (e.g. ``datetime``,
        ``UUID``) are coerced to their string representation instead of raising.
        """
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Server", _SERVER_NAME)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        """Send a JSON error envelope ``{"error": …, "status": …}``."""
        self._send_json({"error": message, "status": status.value}, status)

    def _add_cors_headers(self) -> None:
        """Append permissive CORS headers to the current response.

        Allows any origin to perform authenticated range downloads, which is
        intentional for a public file-sharing service fronted by Cloudflare.
        """
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, HEAD, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Range, Accept-Ranges, Content-Disposition",
        )

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        """Route access log messages through the package logger at DEBUG level."""
        _logger.debug("HTTP %s", fmt % args)

    def log_error(self, fmt: str, *args: object) -> None:  # noqa: N802
        """Route error log messages through the package logger at WARNING level."""
        _logger.warning("HTTP error: %s", fmt % args)


# ---------------------------------------------------------------------------
# Threaded HTTP server
# ---------------------------------------------------------------------------


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server that handles every connection in a dedicated daemon thread.

    Attributes are accessed by ``_ShareLinkHandler`` through ``self.server``,
    the standard CPython pattern for injecting per-server context into per-
    request handlers without using module-level globals.

    ``daemon_threads = True`` ensures that active request threads do not
    prevent the process from exiting during shutdown.  ``block_on_close =
    False`` makes ``server_close()`` return immediately rather than waiting
    for all in-flight threads.

    Attributes
    ----------
    share_manager:
        Object satisfying :class:`ShareManagerProtocol`.
    download_handler:
        Object satisfying :class:`DownloadHandlerProtocol`.
    dashboard_handler:
        Optional object satisfying :class:`DashboardHandlerProtocol`.
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = True
    block_on_close: bool = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[_ShareLinkHandler],
        *,
        share_manager: ShareManagerProtocol,
        download_handler: DownloadHandlerProtocol,
        dashboard_handler: DashboardHandlerProtocol | None,
    ) -> None:
        # Assign attributes before super().__init__() so they are available
        # if the handler is somehow called during server activation.
        self.share_manager: ShareManagerProtocol = share_manager
        self.download_handler: DownloadHandlerProtocol = download_handler
        self.dashboard_handler: DashboardHandlerProtocol | None = dashboard_handler
        super().__init__(server_address, handler_class)

    def get_request(self) -> tuple[socket.socket, Any]:
        """Accept a connection and apply the configured per-socket timeout."""
        conn, addr = super().get_request()
        conn.settimeout(_SOCKET_TIMEOUT)
        return conn, addr

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log connection errors without surfacing benign client-side drops.

        ``BrokenPipeError``, ``ConnectionResetError``, and ``TimeoutError``
        are expected when a client disconnects mid-transfer and are logged at
        DEBUG level to avoid log spam.  All other errors are logged at ERROR
        level with a full traceback.
        """
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionResetError, TimeoutError):
            _logger.debug("Connection dropped by client %s.", client_address)
        else:
            _logger.exception(
                "Unhandled error while serving client %s.", client_address
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_content_disposition(filename: str) -> str:
    """Construct a ``Content-Disposition: attachment`` header value.

    Produces both an ASCII-safe fallback ``filename`` and an RFC 5987
    percent-encoded ``filename*`` parameter so that clients supporting
    either convention receive a correct filename.

    Non-printable characters and characters that would break the quoted
    string (``"``, ``\\``, ``\\r``, ``\\n``) are stripped from the ASCII
    fallback.  The original filename is percent-encoded for the ``filename*``
    parameter using UTF-8, so non-ASCII characters are preserved for
    compliant clients.

    Parameters
    ----------
    filename:
        The original filename string, which may contain non-ASCII characters.

    Returns
    -------
    str
        A complete ``Content-Disposition`` header value, e.g.
        ``attachment; filename="report.pdf"; filename*=UTF-8''report.pdf``
    """
    # Build a printable ASCII-only fallback name.
    ascii_safe = "".join(
        c
        for c in filename
        if 0x20 <= ord(c) < 0x7F and c not in ('"', "\\", "\r", "\n")
    )
    if not ascii_safe:
        ascii_safe = "download"

    # Percent-encode the full UTF-8 filename for RFC 5987 / RFC 6266.
    encoded = urllib.parse.quote(filename, safe="")

    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{encoded}'


# ---------------------------------------------------------------------------
# Public server class
# ---------------------------------------------------------------------------


class ShareLinkServer:
    """Thread-safe HTTP server that exposes the sharelink download service.

    Binds to *host*:*port* and dispatches each HTTP request to a daemon
    thread, supporting thousands of concurrent downloads.  All I/O is
    delegated: file/FTP streaming goes to *download_handler*, share state
    management goes to *share_manager*, and optional dashboard rendering
    goes to *dashboard_handler*.

    Passing ``port=0`` causes the operating system to assign a free
    ephemeral port; the actual port is accessible via :attr:`port` once
    the server is started.

    The server defaults to binding on ``127.0.0.1`` so that only Cloudflare
    Tunnel (or an explicit reverse proxy) can expose it externally.

    Parameters
    ----------
    share_manager:
        Object satisfying :class:`ShareManagerProtocol`.
    download_handler:
        Object satisfying :class:`DownloadHandlerProtocol`.
    host:
        Network interface to bind.  Defaults to loopback (``"127.0.0.1"``).
    port:
        TCP port to listen on.  ``0`` delegates port selection to the OS.
    dashboard_handler:
        Optional object satisfying :class:`DashboardHandlerProtocol`.  When
        not provided, ``GET /`` and ``GET /dashboard`` return ``503``.

    Examples
    --------
    Explicit lifecycle management::

        server = ShareLinkServer(
            share_manager=my_manager,
            download_handler=my_downloader,
        )
        server.start()
        print(server.local_url)   # http://127.0.0.1:<port>
        server.stop()

    Context-manager usage::

        with ShareLinkServer(share_manager=mgr, download_handler=dl) as srv:
            print(srv.local_url)
    """

    def __init__(
        self,
        share_manager: ShareManagerProtocol,
        download_handler: DownloadHandlerProtocol,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        dashboard_handler: DashboardHandlerProtocol | None = None,
    ) -> None:
        self._share_manager = share_manager
        self._download_handler = download_handler
        self._dashboard_handler = dashboard_handler
        self._host = host
        self._port = port

        self._httpd: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind to the configured address and begin serving in a daemon thread.

        The method returns as soon as the background thread is running.  Use
        :attr:`local_url` or :attr:`port` to discover the bound address.

        Raises
        ------
        RuntimeError
            If the server is already running.
        OSError
            If the port is in use or the address cannot be bound.
        """
        with self._lock:
            if self._running:
                raise RuntimeError(
                    "ShareLinkServer is already running.  Call stop() first."
                )

            httpd = _ThreadedHTTPServer(
                (self._host, self._port),
                _ShareLinkHandler,
                share_manager=self._share_manager,
                download_handler=self._download_handler,
                dashboard_handler=self._dashboard_handler,
            )
            thread = threading.Thread(
                target=httpd.serve_forever,
                name="sharelink-http-server",
                daemon=True,
            )
            thread.start()

            self._httpd = httpd
            self._thread = thread
            self._running = True

        bound_host, bound_port = httpd.server_address
        _logger.info(
            "ShareLinkServer started on http://%s:%d.", bound_host, bound_port
        )

    def stop(self) -> None:
        """Signal the server to stop and wait for its background thread to exit.

        Calls ``HTTPServer.shutdown()`` to signal ``serve_forever()`` to
        return, then ``server_close()`` to release the listening socket.
        Safe to call when the server is not running (no-op in that case).
        """
        with self._lock:
            if not self._running or self._httpd is None:
                return
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
            self._running = False

        # Performed outside the lock so other threads are not blocked.
        httpd.shutdown()      # signals serve_forever() to return
        httpd.server_close()  # releases the listening socket

        if thread is not None:
            thread.join(timeout=_SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                _logger.warning(
                    "HTTP server thread did not exit within %.1f s after shutdown.",
                    _SHUTDOWN_TIMEOUT,
                )

        _logger.info("ShareLinkServer stopped.")

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ShareLinkServer":
        """Start the server and return *self*."""
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop the server on context exit, regardless of exceptions."""
        self.stop()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` while the server is accepting connections."""
        return self._running

    @property
    def host(self) -> str:
        """The network interface the server is bound to."""
        return self._host

    @property
    def port(self) -> int:
        """The TCP port the server is listening on.

        When ``port=0`` was passed at construction, this reflects the
        ephemeral port selected by the OS after :meth:`start` is called.
        Returns the configured value (possibly ``0``) when the server is
        stopped.
        """
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    @property
    def local_url(self) -> str:
        """The ``http://host:port`` base URL of the running server.

        Raises
        ------
        RuntimeError
            If called before :meth:`start`.
        """
        if self._httpd is None:
            raise RuntimeError(
                "Server is not running.  Call start() before accessing local_url."
            )
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"<ShareLinkServer {self._host}:{self.port} [{state}]>"

"""
sharelink.utils
===============

General-purpose utilities for the sharelink package.

This module is self-contained and imports only from the Python standard
library.  It exposes pure helper functions, a small set of frozen
dataclasses, and lightweight platform / binary-detection utilities that
are shared across all other sharelink modules.

Thread safety
-------------
All public functions are stateless (pure) or rely on
``functools.lru_cache``, which CPython documents as thread-safe.
Callers need not apply additional locking when invoking any symbol
exported from this module.
"""

from __future__ import annotations

__all__ = [
    # Dataclasses
    "FTPComponents",
    "PlatformInfo",
    "RangeRequest",
    # Platform
    "detect_platform",
    # Networking
    "find_free_port",
    "validate_url",
    # Filesystem / MIME
    "guess_mime",
    "safe_filename",
    "secure_path",
    "file_etag",
    # FTP
    "parse_ftp_url",
    "is_ftp_url",
    "is_local_path",
    # Cloudflared
    "find_cloudflared",
    # HTTP Range
    "parse_range_header",
    # Formatting
    "human_size",
    "human_speed",
    "format_duration",
    # Share ID
    "generate_share_id",
    # Time helpers
    "utc_now",
    "utc_from_timestamp",
    "expiry_datetime",
    "is_expired",
    "remaining_seconds",
]

import functools
import hashlib
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import string
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Alphabet used when generating share identifiers.
_SHARE_ID_ALPHABET: Final[str] = string.ascii_letters + string.digits

#: Default length of a generated share identifier.
_SHARE_ID_LENGTH: Final[int] = 16

#: Ordered size units for human-readable formatting.
_SIZE_UNITS: Final[tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB", "PB")

#: Bytes per step when scaling size units.
_UNIT_STEP: Final[int] = 1024

#: Name of the Cloudflare tunnel binary.
_CLOUDFLARED_BINARY_NAME: Final[str] = "cloudflared"

#: URL schemes accepted by :func:`validate_url` by default.
_VALID_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Compiled regex for a single-range HTTP Range header value.
#: Matches: bytes=<start>-<end>  where start and/or end may be absent.
_RANGE_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$",
    re.IGNORECASE,
)

#: Compiled regex that matches characters NOT allowed in a safe filename.
_UNSAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"[^\w.\-]")

#: Well-known installation directories searched when ``cloudflared`` is
#: not on ``$PATH``.  Searched in order after :func:`shutil.which`.
_CLOUDFLARED_FALLBACK_DIRS: Final[tuple[str, ...]] = (
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",                      # macOS ARM Homebrew
    "/home/linuxbrew/.linuxbrew/bin",          # Linuxbrew on Linux
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / "bin"),
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FTPComponents:
    """
    Immutable, validated components parsed from an FTP URL.

    Attributes
    ----------
    host:
        The FTP server hostname or IP address.
    port:
        TCP port number (default ``21``).
    username:
        Authentication username (default ``'anonymous'``).
    password:
        Authentication password (default ``'anonymous@'``).
    path:
        Absolute path on the FTP server; always begins with ``'/'``.
    """

    host: str
    port: int
    username: str
    password: str
    path: str

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("FTP host must not be empty")
        if not (1 <= self.port <= 65535):
            raise ValueError(
                f"FTP port must be in [1, 65535]; got {self.port!r}"
            )
        if not self.path.startswith("/"):
            raise ValueError(
                f"FTP path must be absolute (start with '/'); got {self.path!r}"
            )


@dataclass(frozen=True, slots=True)
class RangeRequest:
    """
    A parsed HTTP ``Range`` header representing a single byte range.

    Attributes
    ----------
    start:
        Inclusive start byte offset.  ``None`` only for suffix ranges
        (``bytes=-N``).
    end:
        Inclusive end byte offset.  ``None`` means "through the last
        byte of the resource".
    is_suffix:
        ``True`` when the client requested the last *N* bytes via a
        suffix range.  In that case ``start`` is ``None`` and ``end``
        holds *N*.
    """

    start: int | None
    end: int | None
    is_suffix: bool = field(default=False)

    def resolve(self, total_size: int) -> tuple[int, int]:
        """
        Resolve abstract offsets to concrete, inclusive byte positions.

        Parameters
        ----------
        total_size:
            Total size of the resource in bytes; must be positive.

        Returns
        -------
        tuple[int, int]
            ``(first_byte, last_byte)`` — both inclusive — clamped to
            the interval ``[0, total_size - 1]``.

        Raises
        ------
        ValueError
            When *total_size* is not positive, or the resolved range
            would have ``first > last``.
        """
        if total_size <= 0:
            raise ValueError(
                f"total_size must be a positive integer; got {total_size!r}"
            )

        last = total_size - 1

        if self.is_suffix:
            n = self.end  # bytes=-N stores N in ``end``
            if n is None or n <= 0:
                raise ValueError(
                    "Suffix range length must be a positive integer"
                )
            first = max(0, total_size - n)
            return first, last

        first = self.start if self.start is not None else 0
        final = self.end if self.end is not None else last

        first = max(0, first)
        final = min(final, last)

        if first > final:
            raise ValueError(
                f"Range [{first}, {final}] is invalid for a resource of "
                f"{total_size} bytes"
            )
        return first, final


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """
    Immutable snapshot of the host operating-system platform.

    Attributes
    ----------
    system:
        OS name as returned by :func:`platform.system`
        (e.g. ``'Linux'``, ``'Darwin'``, ``'Windows'``).
    is_linux:
        ``True`` when running on Linux.
    is_macos:
        ``True`` when running on macOS / Darwin.
    is_windows:
        ``True`` when running on Windows.
    machine:
        Processor architecture string (e.g. ``'x86_64'``, ``'arm64'``).
    architecture:
        Bitness of the Python interpreter (``'64bit'`` or ``'32bit'``).
    """

    system: str
    is_linux: bool
    is_macos: bool
    is_windows: bool
    machine: str
    architecture: str


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def detect_platform() -> PlatformInfo:
    """
    Return an immutable description of the current operating-system platform.

    The result is computed once and cached; all subsequent calls are O(1)
    and thread-safe.

    Returns
    -------
    PlatformInfo
        A frozen snapshot of the host platform characteristics.
    """
    system = platform.system()
    bits, _ = platform.architecture()
    return PlatformInfo(
        system=system,
        is_linux=(system == "Linux"),
        is_macos=(system == "Darwin"),
        is_windows=(system == "Windows"),
        machine=platform.machine(),
        architecture=bits,
    )


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------


def find_free_port(host: str = "127.0.0.1") -> int:
    """
    Find a free TCP port by binding to port 0 and reading the OS assignment.

    The socket is released immediately after the port number is obtained,
    leaving a small TOCTOU window.  For loopback server / tunnel usage
    this window is inconsequential; callers should bind their server
    socket within a few milliseconds.

    Parameters
    ----------
    host:
        Interface address for the probe bind.  Defaults to the loopback
        interface so the probe never touches the network.

    Returns
    -------
    int
        An available ephemeral port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def validate_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] | None = None,
) -> bool:
    """
    Return ``True`` when *url* is syntactically valid and uses an allowed scheme.

    Parameters
    ----------
    url:
        The URL string to inspect.
    allowed_schemes:
        Set of accepted schemes (compared case-insensitively).  Defaults
        to ``{'http', 'https'}``.

    Returns
    -------
    bool
        ``True`` when the URL has a recognised scheme and a non-empty
        network location.
    """
    schemes = allowed_schemes if allowed_schemes is not None else _VALID_URL_SCHEMES
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return (
        bool(parsed.scheme)
        and parsed.scheme.lower() in schemes
        and bool(parsed.netloc)
    )


# ---------------------------------------------------------------------------
# Filesystem / MIME helpers
# ---------------------------------------------------------------------------


def guess_mime(path: str | Path) -> str:
    """
    Guess the MIME type of a file from its name or extension.

    Parameters
    ----------
    path:
        File path.  Only the name component is used; the file need not
        exist on disk.

    Returns
    -------
    str
        A MIME type string such as ``'text/html'`` or
        ``'image/png'``.  Falls back to ``'application/octet-stream'``
        when the type cannot be determined.
    """
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def safe_filename(
    name: str,
    *,
    replacement: str = "_",
    max_length: int = 255,
) -> str:
    """
    Sanitise a raw string so it is safe to use as a filesystem filename.

    Processing steps applied in order:

    1. Unicode NFKD normalisation and ASCII transliteration.
    2. Explicit replacement of OS path separators and null bytes.
    3. Substitution of every character not matching ``[\\w.\\-]`` with
       *replacement*.
    4. Stripping of leading and trailing dots and whitespace.
    5. Truncation to *max_length* bytes while preserving the extension.
    6. Fall-back to ``'file'`` when the result would otherwise be empty.

    Parameters
    ----------
    name:
        The raw filename to sanitise.
    replacement:
        Substitute character for disallowed characters.
    max_length:
        Maximum byte length of the returned string.

    Returns
    -------
    str
        A filesystem-safe filename string.
    """
    # Step 1 — Unicode normalisation and ASCII transliteration
    normalised = unicodedata.normalize("NFKD", name)
    ascii_name = normalised.encode("ascii", errors="ignore").decode("ascii")

    # Step 2 — Replace path separators and null bytes
    for char in (os.sep, os.altsep or "", "\x00"):
        ascii_name = ascii_name.replace(char, replacement)

    # Step 3 — Substitute disallowed characters
    sanitised = _UNSAFE_FILENAME_RE.sub(replacement, ascii_name)

    # Step 4 — Strip leading / trailing dots and whitespace
    sanitised = sanitised.strip(". ")

    # Step 5 — Truncate to max_length bytes while preserving the extension
    if len(sanitised.encode()) > max_length:
        stem = Path(sanitised).stem
        suffix = Path(sanitised).suffix
        max_stem_bytes = max_length - len(suffix.encode())
        stem_truncated = stem.encode()[:max_stem_bytes].decode(errors="ignore")
        sanitised = stem_truncated + suffix

    # Step 6 — Fallback
    return sanitised or "file"


def secure_path(
    requested: str | Path,
    base_directory: str | Path,
) -> Path:
    """
    Resolve *requested* and assert it resides inside *base_directory*.

    Leading path separators are stripped from *requested* before joining,
    preventing absolute-path injection.  Symlinks are fully resolved via
    :meth:`Path.resolve`, so a symlink pointing outside *base_directory*
    is rejected.

    Parameters
    ----------
    requested:
        The path supplied by an external caller or extracted from an HTTP
        request.
    base_directory:
        The root directory that all served content must reside under.

    Returns
    -------
    Path
        The resolved, validated absolute path.

    Raises
    ------
    FileNotFoundError
        When the resolved path does not exist on disk.
    PermissionError
        When the resolved path escapes *base_directory*, indicating a
        path-traversal attempt.
    """
    base = Path(base_directory).resolve()

    # Strip leading separators to prevent absolute-path injection
    relative = Path(str(requested).lstrip("/\\"))
    target = (base / relative).resolve()

    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")

    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PermissionError(
            f"Path traversal detected: {target!r} is outside {base!r}"
        ) from exc

    return target


def file_etag(path: Path) -> str:
    """
    Generate a lightweight ETag for *path* from its size and mtime.

    This is **not** a cryptographic hash of the file contents.  It is
    intended solely for HTTP cache validation (``ETag`` /
    ``If-None-Match`` headers) and avoids the cost of reading the file.

    Parameters
    ----------
    path:
        Path to an existing file.

    Returns
    -------
    str
        A double-quoted ETag string ready for use in an HTTP response
        header, e.g. ``'"a3f2c1b0d4e5f6a7"'``.
    """
    st = path.stat()
    raw = f"{st.st_size}-{st.st_mtime_ns}"
    digest = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]
    return f'"{digest}"'


# ---------------------------------------------------------------------------
# FTP helpers
# ---------------------------------------------------------------------------


def parse_ftp_url(url: str) -> FTPComponents:
    """
    Parse an FTP URL into its constituent components.

    Accepted formats::

        ftp://host/path
        ftp://host:port/path
        ftp://user@host/path
        ftp://user:password@host:port/path

    Percent-encoded credentials are decoded automatically via
    :func:`urllib.parse.unquote`.

    Parameters
    ----------
    url:
        A well-formed FTP URL string.

    Returns
    -------
    FTPComponents
        Parsed and validated FTP connection parameters.

    Raises
    ------
    ValueError
        When *url* does not use the ``ftp`` scheme, is missing a
        hostname, or contains an out-of-range port number.
    """
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme.lower() != "ftp":
        raise ValueError(
            f"Expected scheme 'ftp', got {parsed.scheme!r}: {url!r}"
        )

    host = parsed.hostname
    if not host:
        raise ValueError(f"FTP URL is missing a hostname: {url!r}")

    return FTPComponents(
        host=host,
        port=parsed.port or 21,
        username=urllib.parse.unquote(parsed.username or "anonymous"),
        password=urllib.parse.unquote(parsed.password or "anonymous@"),
        path=parsed.path or "/",
    )


def is_ftp_url(url: str) -> bool:
    """
    Return ``True`` when *url* uses the ``ftp`` scheme.

    Parameters
    ----------
    url:
        The URL string to inspect.

    Returns
    -------
    bool
    """
    try:
        return urllib.parse.urlparse(url).scheme.lower() == "ftp"
    except ValueError:
        return False


def is_local_path(source: str) -> bool:
    """
    Return ``True`` when *source* refers to a local filesystem path.

    A source is considered local when its URL scheme is absent or not one
    of ``http``, ``https``, or ``ftp``.

    Parameters
    ----------
    source:
        Path or URL string to classify.

    Returns
    -------
    bool
        ``True`` for local paths, ``False`` for remote URLs.
    """
    try:
        scheme = urllib.parse.urlparse(source).scheme.lower()
    except ValueError:
        return True  # Treat malformed strings as local paths
    return scheme not in {"http", "https", "ftp"}


# ---------------------------------------------------------------------------
# Cloudflared binary lookup
# ---------------------------------------------------------------------------


def find_cloudflared() -> Path:
    """
    Locate the ``cloudflared`` binary on the current host.

    Search order:

    1. ``$CLOUDFLARED`` environment variable (explicit path override).
    2. :func:`shutil.which` — searches every directory in ``$PATH``.
    3. A hard-coded list of well-known installation directories.

    Returns
    -------
    Path
        The resolved absolute path to the ``cloudflared`` binary.

    Raises
    ------
    FileNotFoundError
        When ``cloudflared`` cannot be located by any of the above
        methods.
    """
    # 1. Explicit environment variable override
    env_override = os.environ.get("CLOUDFLARED")
    if env_override:
        candidate = Path(env_override)
        if candidate.is_file() and _is_executable(candidate):
            return candidate.resolve()

    # 2. PATH-based lookup via shutil.which
    which_result = shutil.which(_CLOUDFLARED_BINARY_NAME)
    if which_result:
        return Path(which_result).resolve()

    # 3. Well-known fallback directories
    for directory in _CLOUDFLARED_FALLBACK_DIRS:
        candidate = Path(directory) / _CLOUDFLARED_BINARY_NAME
        if candidate.is_file() and _is_executable(candidate):
            return candidate.resolve()

    raise FileNotFoundError(
        "cloudflared binary not found. "
        "Install it from https://developers.cloudflare.com/cloudflare-one/"
        "connections/connect-apps/install-and-setup/installation/ "
        "or set the CLOUDFLARED environment variable to its absolute path."
    )


def _is_executable(path: Path) -> bool:
    """
    Return ``True`` when *path* is a regular file with owner-execute permission.

    Parameters
    ----------
    path:
        Filesystem path to inspect.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and bool(mode & stat.S_IXUSR)


# ---------------------------------------------------------------------------
# HTTP Range header parsing
# ---------------------------------------------------------------------------


def parse_range_header(
    header_value: str,
    total_size: int,
) -> RangeRequest | None:
    """
    Parse a single-range HTTP ``Range`` header value.

    Multi-range requests (comma-separated ranges) and range units other
    than ``bytes`` are not supported; such inputs return ``None``.

    Parameters
    ----------
    header_value:
        The raw header value (field name excluded), e.g.
        ``'bytes=0-1023'``, ``'bytes=-500'``, or ``'bytes=9500-'``.
    total_size:
        Total resource size in bytes; used for boundary validation.

    Returns
    -------
    RangeRequest | None
        A parsed :class:`RangeRequest`, or ``None`` when the header is
        malformed, uses an unsupported unit, or requests an unsatisfiable
        range.
    """
    if not header_value or total_size <= 0:
        return None

    match = _RANGE_HEADER_RE.match(header_value)
    if not match:
        return None

    raw_start, raw_end = match.group(1), match.group(2)

    # Degenerate case: "bytes=-"  (no digits on either side)
    if not raw_start and not raw_end:
        return None

    # Suffix range: bytes=-N
    if not raw_start and raw_end:
        try:
            n = int(raw_end)
        except ValueError:
            return None
        if n <= 0:
            return None
        return RangeRequest(start=None, end=n, is_suffix=True)

    # Standard or open-ended range: bytes=N-M or bytes=N-
    try:
        start = int(raw_start) if raw_start else 0
        end = int(raw_end) if raw_end else None
    except ValueError:
        return None

    if start >= total_size:
        return None

    effective_end = end if end is not None else total_size - 1
    if start > effective_end:
        return None

    return RangeRequest(start=start, end=end, is_suffix=False)


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------


def human_size(num_bytes: int | float, *, precision: int = 2) -> str:
    """
    Format a byte count as a human-readable size string.

    Parameters
    ----------
    num_bytes:
        Byte count to format.
    precision:
        Decimal places for values larger than one byte.

    Returns
    -------
    str
        Formatted string, e.g. ``'0 B'``, ``'512 B'``,
        ``'1.50 KB'``, ``'3.72 GB'``.

    Examples
    --------
    >>> human_size(0)
    '0 B'
    >>> human_size(1536)
    '1.50 KB'
    >>> human_size(1_073_741_824)
    '1.00 GB'
    """
    value = float(num_bytes)
    for unit in _SIZE_UNITS[:-1]:
        if abs(value) < _UNIT_STEP:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= _UNIT_STEP
    return f"{value:.{precision}f} {_SIZE_UNITS[-1]}"


def human_speed(
    bytes_per_second: int | float,
    *,
    precision: int = 2,
) -> str:
    """
    Format a transfer rate as a human-readable speed string.

    Delegates to :func:`human_size` and appends ``'/s'``.

    Parameters
    ----------
    bytes_per_second:
        Transfer speed in bytes per second.
    precision:
        Decimal places forwarded to :func:`human_size`.

    Returns
    -------
    str
        Formatted string, e.g. ``'512 B/s'``, ``'1.00 MB/s'``.

    Examples
    --------
    >>> human_speed(1_048_576)
    '1.00 MB/s'
    """
    return f"{human_size(bytes_per_second, precision=precision)}/s"


def format_duration(seconds: float) -> str:
    """
    Format a duration as a compact human-readable string.

    Negative values are treated as zero.

    Parameters
    ----------
    seconds:
        Duration in seconds (may be fractional).

    Returns
    -------
    str
        A string such as ``'0s'``, ``'45s'``, ``'3m 12s'``,
        ``'1h 5m'``, or ``'2h 0m 0s'`` (zero sub-units are omitted
        unless the total is zero).

    Examples
    --------
    >>> format_duration(0)
    '0s'
    >>> format_duration(3661)
    '1h 1m 1s'
    >>> format_duration(3600)
    '1h'
    """
    total = max(0, int(seconds))
    if total == 0:
        return "0s"

    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Share ID generation
# ---------------------------------------------------------------------------


def generate_share_id(length: int = _SHARE_ID_LENGTH) -> str:
    """
    Generate a cryptographically secure random share identifier.

    Only URL-safe alphanumeric characters (A–Z, a–z, 0–9) are used,
    making the result safe to embed directly in a URL path segment
    without percent-encoding.

    Parameters
    ----------
    length:
        Number of characters in the returned identifier.  Defaults to
        ``16``, yielding ~95 bits of entropy.

    Returns
    -------
    str
        A random alphanumeric string of the requested *length*.
    """
    return "".join(secrets.choice(_SHARE_ID_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """
    Return the current UTC moment as a timezone-aware :class:`datetime`.

    Returns
    -------
    datetime
        Current time in UTC with ``tzinfo=timezone.utc``.
    """
    return datetime.now(tz=timezone.utc)


def utc_from_timestamp(ts: float) -> datetime:
    """
    Convert a POSIX timestamp to a timezone-aware UTC :class:`datetime`.

    Parameters
    ----------
    ts:
        Seconds since the Unix epoch (may be fractional).

    Returns
    -------
    datetime
        The corresponding UTC :class:`datetime`.
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def expiry_datetime(
    *,
    hours: float = 24.0,
    from_time: datetime | None = None,
) -> datetime:
    """
    Compute an expiry :class:`datetime` relative to *from_time*.

    Parameters
    ----------
    hours:
        Number of hours until expiry.  Defaults to ``24``.
    from_time:
        Reference start time.  Defaults to :func:`utc_now`.

    Returns
    -------
    datetime
        An aware UTC :class:`datetime` representing the expiry moment.
    """
    base = from_time if from_time is not None else utc_now()
    return base + timedelta(hours=hours)


def is_expired(expires_at: datetime) -> bool:
    """
    Return ``True`` when *expires_at* is in the past relative to UTC now.

    Parameters
    ----------
    expires_at:
        An aware :class:`datetime` representing the expiry moment.

    Returns
    -------
    bool
        ``True`` if the current UTC time is at or past *expires_at*.
    """
    return utc_now() >= expires_at


def remaining_seconds(expires_at: datetime) -> float:
    """
    Return the number of seconds until *expires_at*, clamped to zero.

    Parameters
    ----------
    expires_at:
        An aware :class:`datetime` representing the expiry moment.

    Returns
    -------
    float
        Seconds remaining until expiry; ``0.0`` when already expired.
    """
    delta = (expires_at - utc_now()).total_seconds()
    return max(0.0, delta)

"""
models.py - Core data models for the sharelink package.

Defines all data structures, enumerations, and type aliases used throughout
the sharelink library. All models are implemented as Python dataclasses with
full type annotations and comprehensive docstrings.

All datetime values are expected to be timezone-aware UTC timestamps.
Thread safety for mutable fields (e.g. ShareStatistics) is the responsibility
of the consuming layer (session.py, manager.py) via explicit locking.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Generic, TypeVar

__all__ = [
    "ShareState",
    "SourceType",
    "TunnelState",
    "ClientInfo",
    "DownloadStatistics",
    "ShareStatistics",
    "ShareInfo",
    "APIResponse",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ShareState(Enum):
    """Represents the full lifecycle state of a file share.

    Transitions follow a strict directed graph:
        PENDING -> ACTIVE -> EXPIRED
        PENDING -> ACTIVE -> EXHAUSTED
        PENDING -> ACTIVE -> REVOKED
        PENDING -> REVOKED

    A share in EXPIRED, EXHAUSTED, or REVOKED state is terminal and
    will never transition back to ACTIVE.
    """

    PENDING = auto()
    """Share has been created but the tunnel URL has not yet been assigned."""

    ACTIVE = auto()
    """Share is live and accepting authenticated download requests."""

    EXPIRED = auto()
    """Share surpassed its expiration timestamp and is no longer accessible."""

    EXHAUSTED = auto()
    """Share reached its maximum allowed download count and is now closed."""

    REVOKED = auto()
    """Share was explicitly revoked by the owner before natural expiration."""


class SourceType(Enum):
    """Classifies the type of resource being shared.

    Determines which download handler is selected by the server layer
    and which validation rules apply during share creation.
    """

    LOCAL_FILE = auto()
    """A single regular file on the local filesystem."""

    LOCAL_DIRECTORY = auto()
    """A directory on the local filesystem served as a browsable file index."""

    FTP = auto()
    """A file or directory on a remote FTP server, proxied through the local
    HTTP server. Requires ftp_host and credentials to be set on ShareInfo."""
    
    HTTP = auto()
    """A file or directory on a http server, proxied through the local
    HTTP server. Requires ftp_host and credentials to be set on ShareInfo."""


class TunnelState(Enum):
    """Represents the operational state of the Cloudflare Tunnel process.

    The tunnel module transitions between these states and broadcasts
    URL updates to all active shares when the tunnel reconnects.
    """

    CONNECTING = auto()
    """Initial connection attempt is in progress."""

    CONNECTED = auto()
    """Tunnel is established; the public URL is valid and reachable."""

    DISCONNECTED = auto()
    """The tunnel connection was lost; a reconnection attempt is pending."""

    RECONNECTING = auto()
    """An automatic reconnection attempt is currently in progress."""

    FAILED = auto()
    """The tunnel has permanently failed after exhausting all retry attempts."""


    
# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class ClientInfo:
    """Captures metadata about the HTTP client initiating a download.

    Recorded once per download session at the moment the request is accepted
    and stored as part of DownloadStatistics for audit and analytics purposes.

    Attributes:
        ip_address: The remote IP address extracted from the HTTP request,
            accounting for X-Forwarded-For headers when behind a proxy.
        user_agent: The raw User-Agent header string from the client request.
            Empty string when the header is absent.
        request_time: Timezone-aware UTC timestamp of when the request arrived.
        bytes_downloaded: Running total of bytes successfully delivered to this
            client. Updated incrementally during streaming transfers.
        is_range_request: True when the client supplied an HTTP Range header,
            indicating a partial-content or resume request.
        completed: True when the transfer ended cleanly with all requested
            bytes delivered; False when interrupted or still in progress.
    """

    ip_address: str
    user_agent: str
    request_time: datetime
    bytes_downloaded: int = 0
    is_range_request: bool = False
    completed: bool = False


@dataclass
class DownloadStatistics:
    """Records the complete statistics for a single download session.

    Created at the start of each download and updated as bytes are streamed
    to the client. Immutable fields are set at construction; mutable fields
    (bytes_transferred, completed_at) are updated by the download handler.

    Range request fields are populated only when is_range_request is True.
    Both range_start and range_end use zero-based byte offsets and are
    inclusive, matching the semantics of the HTTP Content-Range header.

    Attributes:
        download_id: Unique UUID-4 identifier for this download session.
        share_id: The share_id of the parent ShareInfo this session belongs to.
        client_info: Snapshot of client metadata captured at request time.
        started_at: Timezone-aware UTC timestamp when the session began.
        completed_at: Timezone-aware UTC timestamp when the session ended,
            or None when the download is still active or was interrupted.
        bytes_transferred: Total bytes delivered to the client across this
            session, including retransmissions on resumed range requests.
        is_range_request: True when the client requested a byte range.
        range_start: Zero-based inclusive start byte of the requested range,
            or None for non-range requests.
        range_end: Zero-based inclusive end byte of the requested range,
            or None for non-range requests or open-ended ranges.
    """

    download_id: str
    share_id: str
    client_info: ClientInfo
    started_at: datetime
    completed_at: datetime | None = None
    bytes_transferred: int = 0
    is_range_request: bool = False
    range_start: int | None = None
    range_end: int | None = None

    @classmethod
    def create(cls, share_id: str, client_info: ClientInfo) -> DownloadStatistics:
        """Factory that constructs a new session record ready for tracking.

        Generates a unique download_id and stamps started_at with the current
        UTC time. All counters start at zero; the caller is responsible for
        updating bytes_transferred and completed_at as the transfer progresses.

        Args:
            share_id: Identifier of the share being accessed in this session.
            client_info: Pre-populated metadata about the requesting client.

        Returns:
            A DownloadStatistics instance with a unique ID and current timestamp.
        """
        return cls(
            download_id=str(uuid.uuid4()),
            share_id=share_id,
            client_info=client_info,
            started_at=datetime.now(timezone.utc),
        )


@dataclass
class ShareStatistics:
    """Aggregated download metrics and full session history for a share.

    Maintained by the session layer throughout the share's lifetime.
    All integer counters are updated atomically under an external lock
    held by the owning ShareSession. The download_history list provides
    a complete audit trail of every access.

    Attributes:
        total_downloads: Cumulative count of all download sessions initiated,
            including incomplete and interrupted transfers.
        completed_downloads: Count of sessions that delivered all requested
            bytes without interruption.
        active_downloads: Count of sessions currently streaming data.
            Decremented when a session ends, regardless of completion status.
        total_bytes_transferred: Sum of bytes_transferred across every session,
            including partial and failed downloads.
        unique_ips: Set of distinct client IP addresses that have accessed
            this share. Used to approximate unique visitor counts.
        download_history: Chronologically ordered list of all DownloadStatistics
            records associated with this share.
        first_accessed: Timezone-aware UTC timestamp of the first download
            session, or None if the share has never been accessed.
        last_accessed: Timezone-aware UTC timestamp of the most recent download
            session, or None if the share has never been accessed.
    """

    total_downloads: int = 0
    completed_downloads: int = 0
    active_downloads: int = 0
    total_bytes_transferred: int = 0
    unique_ips: set[str] = field(default_factory=set)
    download_history: list[DownloadStatistics] = field(default_factory=list)
    first_accessed: datetime | None = None
    last_accessed: datetime | None = None


@dataclass
class ShareInfo:
    """Primary domain object representing a single file share.

    Encapsulates the source resource, access control parameters, current
    lifecycle state, and accumulated statistics. Instantiated by the manager
    layer and stored in the session registry for the duration of its lifetime.

    For LOCAL_FILE and LOCAL_DIRECTORY shares, source_path must be an absolute
    path accessible by the running process. For FTP shares, source_path is the
    remote path on the FTP server, and the ftp_* connection fields are required.

    The public_url field is populated once the tunnel reports a connected state
    and is updated in-place whenever the tunnel reconnects with a new hostname.
    All other fields are immutable after creation.

    Attributes:
        share_id: Unique UUID-4 identifier for this share.
        source_path: Absolute local filesystem path (LOCAL_FILE, LOCAL_DIRECTORY)
            or remote path on the FTP server (FTP).
        source_type: Classification of the resource being shared.
        state: Current lifecycle state; mutated by the session layer only.
        token: Cryptographically random URL-safe token embedded in the public
            URL for access authorization. Must be treated as a secret.
        created_at: Timezone-aware UTC timestamp when the share was created.
        expires_at: Timezone-aware UTC timestamp after which the share becomes
            inaccessible regardless of remaining download count.
        max_downloads: Upper bound on the number of download sessions permitted
            before the share transitions to EXHAUSTED state.
        statistics: Mutable aggregated and historical download data. Updated
            exclusively by the session layer under an external lock.
        public_url: Full HTTPS URL through which clients access this share.
            Empty string until the tunnel provides a hostname; updated
            automatically on tunnel reconnect.
        display_name: Optional override for the filename presented to the client
            in Content-Disposition headers. When None, the basename of
            source_path is used.
        content_type: MIME type string for the response Content-Type header.
            When None, the server layer determines it via file inspection.
        file_size: Size of the file in bytes for Content-Length headers and
            range validation. None for directories or when not yet determined.
        ftp_host: Hostname or IP address of the FTP server. Required when
            source_type is FTP; None otherwise.
        ftp_port: TCP port of the FTP server. Defaults to 21 when None and
            source_type is FTP.
        ftp_username: Username for FTP authentication. None implies anonymous.
        ftp_password: Plaintext password for FTP authentication. The caller
            is responsible for secure handling and must not log this value.
        ftp_passive: True to use PASV mode; False to use active (PORT) mode.
            Passive mode is strongly preferred for NAT and firewall compatibility.
    """

    share_id: str
    source_path: str
    source_type: SourceType
    state: ShareState
    token: str
    created_at: datetime
    expires_at: datetime
    max_downloads: int
    statistics: ShareStatistics = field(default_factory=ShareStatistics)
    public_url: str = ""
    display_name: str | None = None
    content_type: str | None = None
    file_size: int | None = None
    ftp_host: str | None = None
    ftp_port: int | None = None
    ftp_username: str | None = None
    ftp_password: str | None = None
    ftp_passive: bool = True
    http_headers: dict[str, str] = field(default_factory=dict)
    http_url: str | None = None

    @property
    def is_active(self) -> bool:
        """True when the share is in ACTIVE state and ready to serve requests."""
        return self.state == ShareState.ACTIVE

    @property
    def is_terminal(self) -> bool:
        """True when the share is in a terminal state and cannot be reactivated.

        Terminal states are EXPIRED, EXHAUSTED, and REVOKED. A terminal share
        will never serve further downloads and may be safely cleaned up.
        """
        return self.state in (ShareState.EXPIRED, ShareState.EXHAUSTED, ShareState.REVOKED)

    @property
    def is_past_expiry(self) -> bool:
        """True when the current UTC time is strictly after expires_at.

        Does not mutate state; the session layer is responsible for
        transitioning the share to EXPIRED when this returns True.
        """
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def downloads_remaining(self) -> int:
        """Number of additional downloads permitted before exhaustion.

        Returns zero when total_downloads has already met or exceeded
        max_downloads, never a negative value.
        """
        return max(0, self.max_downloads - self.statistics.total_downloads)

    @property
    def is_ftp_source(self) -> bool:
        """True when this share proxies a resource from an FTP server."""
        return self.source_type == SourceType.FTP

    @property
    def is_directory_source(self) -> bool:
        """True when this share serves a local directory index."""
        return self.source_type == SourceType.LOCAL_DIRECTORY


@dataclass
class APIResponse(Generic[T]):
    """Generic response envelope for all operations exposed by the API layer.

    Provides a consistent contract between api.py and its callers: every
    operation returns an APIResponse regardless of success or failure.
    Successful responses carry a typed payload in data; failed responses
    carry a human-readable message in error. Exactly one of data or error
    will be non-None for any given instance.

    Use the ok() and fail() class methods to construct instances rather
    than calling the constructor directly.

    Type Parameters:
        T: The type of the data payload carried in a successful response.

    Attributes:
        success: True when the requested operation completed without error.
        data: The typed operation result. Populated on success; None on failure.
        error: Human-readable description of the failure. None on success.
        timestamp: Timezone-aware UTC timestamp when this response was created.
        request_id: Optional opaque identifier for correlating this response
            with a specific inbound request in logs and distributed traces.
    """

    success: bool
    data: T | None = None
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str | None = None

    @classmethod
    def ok(cls, data: T, request_id: str | None = None) -> "APIResponse[T]":
        """Constructs a successful response wrapping the given payload.

        Args:
            data: The operation result to embed in the response.
            request_id: Optional correlation identifier for request tracing.

        Returns:
            An APIResponse with success=True, the provided data payload,
            and error set to None.
        """
        return cls(success=True, data=data, request_id=request_id)

    @classmethod
    def fail(cls, error: str, request_id: str | None = None) -> "APIResponse[None]":
        """Constructs a failure response carrying a descriptive error message.

        Args:
            error: A clear, human-readable explanation of what went wrong.
                Should not include internal stack traces or sensitive values.
            request_id: Optional correlation identifier for request tracing.

        Returns:
            An APIResponse with success=False, the provided error message,
            and data set to None.
        """
        return cls(success=False, error=error, request_id=request_id)

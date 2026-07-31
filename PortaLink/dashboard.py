"""
sharelink.dashboard
~~~~~~~~~~~~~~~~~~~

HTML dashboard renderer for the sharelink package.

Provides a dark-themed, self-contained web dashboard that displays all active
shares, download statistics, transfer metrics, expiration status, and recent
structured log entries.  Share data and server status are fetched by the
browser via AJAX polls against the REST API endpoints served by the same HTTP
server.  Log entries are read directly from the configured log directory on
every :meth:`DashboardHandler.render` call and embedded in the HTML so they
appear immediately without an extra network round-trip.

Archireading
------------
``DashboardHandler``
    The single public class.  Satisfies the ``DashboardHandlerProtocol``
    structural contract defined in ``server.py``::

        class DashboardHandlerProtocol(Protocol):
            def render(self) -> tuple[bytes, str]: ...

    One instance is created and injected into
    :class:`~server.ShareLinkServer` as its ``dashboard_handler`` parameter.
    The class owns no mutable state beyond its construction-time configuration;
    :meth:`render` is therefore safe to call concurrently from multiple HTTP
    request-handler threads without additional locking.

Log reading
-----------
Log files are JSON-lines files produced by :class:`~logger.JsonFormatter`.
On each :meth:`render` call the three canonical log files
(``request.log``, ``download.log``, ``system.log``) are read from the
configured directory.  Each file is read tail-first to bound memory usage on
large log files.  Entries from all three files are merged, sorted by
``timestamp`` descending, and the most recent :attr:`DashboardHandler.max_log_lines`
entries are embedded in the returned HTML.  Missing or unreadable files are
silently skipped so the dashboard remains functional during early start-up.

REST integration
----------------
The JavaScript embedded in the rendered HTML polls two endpoints every
:data:`_POLL_INTERVAL_MS` milliseconds:

* ``GET /api/status``  – Server health and aggregate active-share count.
* ``GET /api/shares``  – Complete share list with per-share statistics.

Share revocation is performed via:

* ``DELETE /api/shares/<share_id>``  – Immediately revokes the named share.

Transfer speed is estimated client-side by computing the change in
cumulative ``total_bytes_transferred`` between consecutive polling cycles,
divided by the elapsed wall-clock time.

No tunnel, session, or server code is imported or referenced here.  All live
data reaches the browser exclusively through the REST layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .config import DEFAULT_LOG_DIRECTORY
from .logger import (
    DOWNLOAD_LOGGER_NAME,
    REQUEST_LOGGER_NAME,
    SYSTEM_LOGGER_NAME,
)

__all__: list[str] = ["DashboardHandler"]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_CONTENT_TYPE: Final[str] = "text/html; charset=utf-8"
"""HTTP ``Content-Type`` value returned alongside the rendered HTML bytes."""

_POLL_INTERVAL_MS: Final[int] = 3_000
"""Milliseconds between automatic AJAX refreshes in the browser."""

_DEFAULT_MAX_LOG_LINES: Final[int] = 200
"""Maximum number of log entries embedded per :meth:`~DashboardHandler.render` call."""

_LOG_FILES: Final[tuple[str, ...]] = (
    REQUEST_LOGGER_NAME.rpartition(".")[-1],   # "request"
    DOWNLOAD_LOGGER_NAME.rpartition(".")[-1],  # "download"
    SYSTEM_LOGGER_NAME.rpartition(".")[-1],    # "system"
)
"""Stem names of the JSON-lines log files scanned on every render."""

# Standard fields emitted by JsonFormatter; excluded from the "extras" line.
_STANDARD_LOG_FIELDS: Final[frozenset[str]] = frozenset({
    "timestamp", "level", "logger", "message",
    "module", "function", "line", "thread_id", "thread_name",
    "_source", "exc_info", "stack_info",
})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """Escape *text* for safe embedding in HTML element content or attributes.

    Parameters
    ----------
    text:
        Raw string to escape.

    Returns
    -------
    str
        HTML-safe string with ``&``, ``<``, ``>``, ``"``, and ``'``
        replaced by their character-entity equivalents.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class DashboardHandler:
    """Renders the sharelink administration dashboard as a self-contained HTML page.

    Satisfies the ``DashboardHandlerProtocol`` defined in ``server.py``; inject
    one instance into :class:`~server.ShareLinkServer` to activate the dashboard
    at ``GET /`` and ``GET /dashboard``.

    The rendered page:

    * Uses a dark theme with zero external dependencies (no CDN, no framework).
    * Polls ``/api/status`` and ``/api/shares`` every
      :data:`_POLL_INTERVAL_MS` milliseconds and updates the share table and
      stat cards in-place without a full page reload.
    * Estimates aggregate transfer speed client-side from consecutive poll
      deltas.
    * Embeds recent structured log entries (read server-side on each render)
      in a filterable log viewer.
    * Provides a per-share "Revoke" button that issues a ``DELETE`` REST call
      and immediately refreshes the table.

    Thread safety
    -------------
    :meth:`render` is effectively stateless — it reads from the filesystem and
    constructs strings — making it safe to call from multiple HTTP
    request-handler threads simultaneously without additional locking.

    Parameters
    ----------
    log_directory:
        Directory containing the JSON-lines log files written by
        :class:`~logger.LoggingManager`.  Defaults to
        :data:`~config.DEFAULT_LOG_DIRECTORY` when *None*.
    max_log_lines:
        Maximum number of log entries to embed per render.  The most recent
        entries across all log files are selected.  Clamped to at least 1.
    """

    def __init__(
        self,
        log_directory: Path | None = None,
        max_log_lines: int = _DEFAULT_MAX_LOG_LINES,
    ) -> None:
        self._log_directory: Path = (
            log_directory if log_directory is not None else DEFAULT_LOG_DIRECTORY
        )
        self._max_log_lines: int = max(1, max_log_lines)

    # ------------------------------------------------------------------
    # DashboardHandlerProtocol
    # ------------------------------------------------------------------

    def render(self) -> tuple[bytes, str]:
        """Build and return the complete dashboard HTML page.

        Reads recent log entries from disk, generates the HTML document with
        embedded CSS and JavaScript, and returns the UTF-8 encoded bytes
        together with the appropriate ``Content-Type`` header value.

        Returns
        -------
        tuple[bytes, str]
            ``(html_bytes, content_type)`` where *content_type* is
            ``"text/html; charset=utf-8"``.
        """
        log_entries = self._read_recent_logs()
        html = self._build_html(log_entries)
        return html.encode("utf-8"), _CONTENT_TYPE

    # ------------------------------------------------------------------
    # Private – log ingestion
    # ------------------------------------------------------------------

    def _read_recent_logs(self) -> list[dict[str, Any]]:
        """Read, merge, and sort recent entries from all log files.

        Each log file is opened and only the last :attr:`_max_log_lines` raw
        lines are parsed to bound memory usage on large files.  Entries from
        all three files are merged into one list, sorted by the ``timestamp``
        field in descending order (most-recent-first), and truncated to
        :attr:`_max_log_lines`.

        Individual JSON decode errors and ``OSError`` exceptions are caught
        per-file so that a single corrupted or missing file does not prevent
        log entries from other files from appearing on the dashboard.

        Returns
        -------
        list[dict[str, Any]]
            Parsed log-entry dictionaries, most-recent-first.
        """
        entries: list[dict[str, Any]] = []

        for stem in _LOG_FILES:
            log_path = self._log_directory / f"{stem}.log"
            if not log_path.is_file():
                continue
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    # Read from the tail only to avoid scanning enormous files.
                    lines = fh.readlines()[-self._max_log_lines:]
                for raw in lines:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        entry: dict[str, Any] = json.loads(stripped)
                        # Tag the entry so JS and server-side rendering can
                        # route it to the correct source filter.
                        entry.setdefault("_source", stem)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        # Partially written or corrupted line; skip silently.
                        continue
            except OSError:
                continue

        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return entries[: self._max_log_lines]

    # ------------------------------------------------------------------
    # Private – HTML construction
    # ------------------------------------------------------------------

    def _build_html(self, log_entries: list[dict[str, Any]]) -> str:
        """Assemble the complete HTML5 document.

        Parameters
        ----------
        log_entries:
            Pre-sorted log entries to embed in the log viewer section.

        Returns
        -------
        str
            A complete, self-contained HTML5 document string.
        """
        log_rows = self._render_log_rows(log_entries)
        render_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        poll_ms = _POLL_INTERVAL_MS
        poll_s = poll_ms // 1000

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sharelink Dashboard</title>
<style>
{_CSS}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
      </svg>
      sharelink
    </div>
    <div class="header-meta">
      <span id="status-badge" class="badge badge-pending">
        <span class="badge-dot"></span>connecting&hellip;
      </span>
      <span class="render-time">{_esc(render_time)}</span>
    </div>
  </div>
</header>

<main>

  <!-- ── Stat cards ─────────────────────────────────────────────────── -->
  <section class="panel">
    <h2 class="panel-title">Server Status</h2>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Active Shares</div>
        <div class="stat-value" id="sc-shares">&mdash;</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Downloads</div>
        <div class="stat-value" id="sc-total-dl">&mdash;</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active Downloads</div>
        <div class="stat-value" id="sc-active-dl">&mdash;</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Transfer Speed</div>
        <div class="stat-value" id="sc-speed">&mdash;</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Bytes Transferred</div>
        <div class="stat-value" id="sc-bytes">&mdash;</div>
      </div>
    </div>
  </section>

  <!-- ── Shares table ───────────────────────────────────────────────── -->
  <section class="panel">
    <h2 class="panel-title">
      Shares
      <span class="pill">auto&#8209;refresh every {poll_s}s</span>
    </h2>
    <div class="table-wrap">
      <table id="shares-table">
        <thead>
          <tr>
            <th>Filename</th>
            <th>Type</th>
            <th>State</th>
            <th>Downloads</th>
            <th>Active</th>
            <th>Transferred</th>
            <th>Expires</th>
            <th>Public URL</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="shares-tbody">
          <tr><td colspan="9" class="table-empty">Loading&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <!-- ── Log viewer ─────────────────────────────────────────────────── -->
  <section class="panel">
    <h2 class="panel-title">
      Recent Logs
      <button class="btn btn-sm" onclick="location.reload()" title="Reload page to refresh log entries">
        &#8635;&nbsp;Refresh
      </button>
    </h2>
    <div class="log-toolbar">
      <label class="filter-lbl">
        <input type="checkbox" id="f-request" checked>
        <span class="log-tag tag-request">request</span>
      </label>
      <label class="filter-lbl">
        <input type="checkbox" id="f-download" checked>
        <span class="log-tag tag-download">download</span>
      </label>
      <label class="filter-lbl">
        <input type="checkbox" id="f-system" checked>
        <span class="log-tag tag-system">system</span>
      </label>
      <span class="filter-sep"></span>
      <label class="filter-lbl">
        <input type="checkbox" id="f-warn">
        <span class="log-tag tag-warn">warnings &amp; errors only</span>
      </label>
    </div>
    <div class="log-viewer" id="log-viewer">
{log_rows}
    </div>
  </section>

</main>

<script>const POLL_MS = {poll_ms};</script>
<script>
{_JAVASCRIPT}
</script>

</body>
</html>"""

    def _render_log_rows(self, entries: list[dict[str, Any]]) -> str:
        """Render log entries as an HTML fragment for the log viewer.

        Each entry becomes a ``<div class="log-row">`` element.  The primary
        log line contains the timestamp, source tag, severity badge, and
        message text.  Any caller-supplied extra fields (anything beyond the
        standard :class:`~logger.JsonFormatter` schema) are rendered on a
        secondary ``key=value`` line beneath the main message.  Exception
        tracebacks (``exc_info``) appear in a ``<pre>`` block.

        All string values are passed through :func:`_esc` before insertion so
        that log messages containing HTML metacharacters cannot break the page
        or inject markup.

        Parameters
        ----------
        entries:
            Log-entry dictionaries sorted most-recent-first.

        Returns
        -------
        str
            HTML fragment ready to embed inside ``<div class="log-viewer">``.
            Contains no wrapping element of its own.
        """
        if not entries:
            return '      <div class="log-empty">No log entries available.</div>'

        _src_tag_cls: dict[str, str] = {
            "request":  "tag-request",
            "download": "tag-download",
            "system":   "tag-system",
        }

        rows: list[str] = []
        for entry in entries:
            level   = str(entry.get("level", "INFO")).upper()
            source  = str(entry.get("_source", "system"))
            ts_raw  = str(entry.get("timestamp", ""))
            # Trim to second precision and convert ISO separator for display.
            ts_disp = ts_raw[:19].replace("T", " ") if len(ts_raw) >= 19 else ts_raw
            message = _esc(str(entry.get("message", "")))

            lv      = level.lower()
            is_warn = level in ("WARNING", "ERROR", "CRITICAL")
            warn_attr = ' data-warn="1"' if is_warn else ""

            src_cls = _src_tag_cls.get(source, "tag-system")

            # --- Extra fields (non-standard keys) ---
            extras = {k: v for k, v in entry.items() if k not in _STANDARD_LOG_FIELDS}
            extras_html = ""
            if extras:
                kv_parts = "".join(
                    f'<span class="kv">'
                    f'<span class="kv-k">{_esc(k)}</span>'
                    f'<span class="kv-eq">=</span>'
                    f'<span class="kv-v">{_esc(str(v))}</span>'
                    f'</span>'
                    for k, v in extras.items()
                )
                extras_html = f'\n        <div class="log-extras">{kv_parts}</div>'

            # --- Exception traceback ---
            exc_html = ""
            if exc_info := entry.get("exc_info"):
                exc_html = f'\n        <pre class="log-exc">{_esc(str(exc_info))}</pre>'

            rows.append(
                f'      <div class="log-row row-{lv}" data-src="{_esc(source)}"{warn_attr}>\n'
                f'        <div class="log-line">'
                f'<span class="log-ts">{_esc(ts_disp)}</span>'
                f'<span class="log-tag {src_cls}">{_esc(source)}</span>'
                f'<span class="log-lv lvb-{lv}">{level}</span>'
                f'<span class="log-msg">{message}</span>'
                f'</div>'
                f'{extras_html}'
                f'{exc_html}'
                f'\n      </div>'
            )

        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Embedded CSS  (dark theme, no external dependencies)
# ---------------------------------------------------------------------------

_CSS: Final[str] = """\
/* ── Reset & base ─────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:          #0d1117;
  --surface:     #161b22;
  --surface-2:   #1c2128;
  --border:      #30363d;
  --border-2:    #21262d;
  --text:        #e6edf3;
  --text-muted:  #8b949e;
  --text-dim:    #484f58;
  --accent:      #58a6ff;
  --accent-bg:   #1c2b3d;
  --green:       #3fb950;
  --green-bg:    #122a1a;
  --yellow:      #e3b341;
  --yellow-bg:   #2d2010;
  --red:         #f85149;
  --red-bg:      #3d1212;
  --purple:      #bc8cff;
  --purple-bg:   #1e1030;
  --r:           6px;
  --r-sm:        4px;
  --r-lg:        8px;
  --font:        -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  --mono:        'Cascadia Code', 'Fira Code', 'Consolas', 'Liberation Mono', monospace;
  --t:           0.14s ease;
}

html { font-size: 14px; scroll-behavior: smooth; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  line-height: 1.6;
  min-height: 100vh;
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Header ────────────────────────────────────────────────────────────── */
header {
  position: sticky; top: 0; z-index: 200;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  height: 54px;
  padding: 0 1.5rem;
}
.header-inner {
  max-width: 1440px; margin: 0 auto; height: 100%;
  display: flex; align-items: center; justify-content: space-between;
}
.logo {
  display: flex; align-items: center; gap: 0.45rem;
  font-size: 1rem; font-weight: 700;
  color: var(--accent); letter-spacing: 0.02em;
}
.header-meta { display: flex; align-items: center; gap: 1.25rem; }
.render-time { font-size: 0.7rem; color: var(--text-dim); font-family: var(--mono); }

/* ── Status badge ──────────────────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 3px 11px; border-radius: 999px;
  font-size: 0.7rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.06em;
  border: 1px solid;
}
.badge-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; flex-shrink: 0;
}
.badge-ok      { color: var(--green);  border-color: var(--green);  background: var(--green-bg); }
.badge-error   { color: var(--red);    border-color: var(--red);    background: var(--red-bg); }
.badge-pending { color: var(--yellow); border-color: var(--yellow); background: var(--yellow-bg); }

/* ── Layout ────────────────────────────────────────────────────────────── */
main {
  max-width: 1440px; margin: 1.5rem auto;
  padding: 0 1.5rem 3rem;
  display: flex; flex-direction: column; gap: 1.25rem;
}

/* ── Panel ─────────────────────────────────────────────────────────────── */
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 1.25rem 1.5rem;
}
.panel-title {
  font-size: 0.72rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--text-muted); margin-bottom: 1rem;
  display: flex; align-items: center; gap: 0.75rem;
}

/* ── Pill ──────────────────────────────────────────────────────────────── */
.pill {
  font-size: 0.64rem; font-weight: 400;
  text-transform: none; letter-spacing: 0;
  color: var(--text-dim);
  background: var(--surface-2); border: 1px solid var(--border-2);
  border-radius: 999px; padding: 1px 9px;
  font-family: var(--mono);
}

/* ── Stat grid ─────────────────────────────────────────────────────────── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
  gap: 1rem;
}
.stat-card {
  background: var(--bg);
  border: 1px solid var(--border-2);
  border-radius: var(--r);
  padding: 1rem 1.2rem;
}
.stat-label {
  font-size: 0.66rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-muted);
  margin-bottom: 0.45rem;
}
.stat-value {
  font-size: 1.75rem; font-weight: 700;
  color: var(--text); font-family: var(--mono); line-height: 1;
}

/* ── Table ─────────────────────────────────────────────────────────────── */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-2);
  border-radius: var(--r);
}
table { width: 100%; border-collapse: collapse; font-size: 0.81rem; }
thead th {
  background: var(--bg); color: var(--text-muted);
  font-size: 0.66rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.08em;
  padding: 0.6rem 0.9rem; text-align: left;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
tbody tr { border-bottom: 1px solid var(--border-2); transition: background var(--t); }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: rgba(255,255,255,.025); }
td { padding: 0.65rem 0.9rem; color: var(--text); vertical-align: middle; }
.td-mono { font-family: var(--mono); font-size: 0.77rem; }
.td-file { max-width: 180px; }
.td-file > span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.table-empty { text-align: center; color: var(--text-muted); padding: 2.5rem; font-style: italic; }

/* ── Type badges ───────────────────────────────────────────────────────── */
.type-badge {
  display: inline-block; padding: 2px 8px; border-radius: var(--r-sm);
  font-size: 0.67rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap;
}
.type-FILE      { background: var(--accent-bg); color: var(--accent); }
.type-DIRECTORY { background: var(--purple-bg); color: var(--purple); }
.type-FTP       { background: var(--yellow-bg); color: var(--yellow); }

/* ── State badges ──────────────────────────────────────────────────────── */
.state-badge {
  display: inline-block; padding: 2px 8px; border-radius: var(--r-sm);
  font-size: 0.67rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap;
}
.state-ACTIVE    { background: var(--green-bg);  color: var(--green); }
.state-PENDING   { background: var(--accent-bg); color: var(--accent); }
.state-EXPIRED   { background: var(--yellow-bg); color: var(--yellow); }
.state-EXHAUSTED { background: var(--yellow-bg); color: var(--yellow); }
.state-REVOKED   { background: var(--red-bg);    color: var(--red); }

/* ── Expiry ────────────────────────────────────────────────────────────── */
.exp-ok   { color: var(--text-muted); }
.exp-soon { color: var(--yellow); font-weight: 600; }
.exp-gone { color: var(--red);    font-weight: 600; }

/* ── URL cell ──────────────────────────────────────────────────────────── */
.url-link {
  color: var(--accent); font-family: var(--mono); font-size: 0.72rem;
  max-width: 220px; display: block;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.url-pending { color: var(--text-dim); font-style: italic; font-size: 0.75rem; }

/* ── Buttons ───────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 5px 12px; border-radius: var(--r);
  border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text);
  font-size: 0.78rem; font-family: var(--font);
  cursor: pointer; transition: background var(--t), border-color var(--t);
  white-space: nowrap; line-height: 1;
}
.btn:hover:not(:disabled) { background: var(--bg); border-color: var(--text-muted); }
.btn:disabled { opacity: 0.35; cursor: not-allowed; }
.btn-danger { border-color: #5a1e1e; color: var(--red); }
.btn-danger:hover:not(:disabled) { background: var(--red-bg); border-color: var(--red); }
.btn-sm { padding: 3px 9px; font-size: 0.71rem; }

/* ── Log toolbar ───────────────────────────────────────────────────────── */
.log-toolbar {
  display: flex; align-items: center; flex-wrap: wrap;
  gap: 0.55rem; margin-bottom: 0.75rem;
}
.filter-lbl {
  display: flex; align-items: center; gap: 0.3rem;
  cursor: pointer; user-select: none;
}
.filter-lbl input[type="checkbox"] {
  cursor: pointer; accent-color: var(--accent);
  width: 13px; height: 13px;
}
.filter-sep { width: 1px; height: 16px; background: var(--border); margin: 0 0.2rem; }

/* ── Log viewer ────────────────────────────────────────────────────────── */
.log-viewer {
  background: var(--bg);
  border: 1px solid var(--border-2);
  border-radius: var(--r);
  max-height: 460px; overflow-y: auto;
  padding: 0.4rem;
  font-family: var(--mono); font-size: 0.72rem;
  display: flex; flex-direction: column; gap: 1px;
}
.log-empty { color: var(--text-muted); text-align: center; padding: 2rem; font-style: italic; }

/* ── Log rows ──────────────────────────────────────────────────────────── */
.log-row {
  padding: 4px 7px; border-radius: var(--r-sm);
  border-left: 3px solid transparent;
  transition: background var(--t);
}
.log-row:hover { background: rgba(255,255,255,.04); }
.log-row.hidden { display: none; }

.log-row.row-debug    { border-left-color: var(--border); opacity: 0.55; }
.log-row.row-info     { border-left-color: var(--accent); }
.log-row.row-warning  { border-left-color: var(--yellow); background: rgba(227,179,65,.05); }
.log-row.row-error    { border-left-color: var(--red);    background: rgba(248,81,73,.08); }
.log-row.row-critical { border-left-color: #ff2222;       background: rgba(255,0,0,.12); }

.log-line {
  display: flex; align-items: baseline; flex-wrap: wrap;
  gap: 0.4rem; line-height: 1.55;
}
.log-ts  { color: var(--text-dim); flex-shrink: 0; }
.log-msg { color: var(--text); flex: 1; word-break: break-word; }

/* ── Source tags ───────────────────────────────────────────────────────── */
.log-tag {
  display: inline-block; padding: 0 5px; border-radius: 3px;
  font-size: 0.61rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.04em; flex-shrink: 0;
}
.tag-request  { background: #1c2a3a; color: #79b8ff; }
.tag-download { background: var(--green-bg);  color: var(--green); }
.tag-system   { background: var(--purple-bg); color: var(--purple); }
.tag-warn     { background: var(--yellow-bg); color: var(--yellow); }

/* ── Level badges (prefix lvb- avoids selector collision with row classes) */
.log-lv { flex-shrink: 0; font-weight: 700; font-size: 0.64rem; }
.lvb-debug    { color: var(--text-dim); }
.lvb-info     { color: var(--accent); }
.lvb-warning  { color: var(--yellow); }
.lvb-error    { color: var(--red); }
.lvb-critical { color: #ff4444; }

/* ── Log extras ────────────────────────────────────────────────────────── */
.log-extras {
  display: flex; flex-wrap: wrap; gap: 0.35rem;
  margin-top: 2px; padding-left: 2px;
}
.kv       { font-size: 0.67rem; color: var(--text-muted); }
.kv-k     { color: var(--purple); }
.kv-eq    { color: var(--text-dim); }
.kv-v     { color: var(--accent); }

/* ── Log exception ─────────────────────────────────────────────────────── */
.log-exc {
  margin-top: 5px; padding: 6px 9px;
  background: #110808; border: 1px solid #5a1e1e; border-radius: var(--r-sm);
  color: #ff9090; font-size: 0.64rem;
  white-space: pre-wrap; word-break: break-all;
  max-height: 140px; overflow-y: auto;
}

/* ── Scrollbars ────────────────────────────────────────────────────────── */
::-webkit-scrollbar            { width: 6px; height: 6px; }
::-webkit-scrollbar-track      { background: var(--bg); }
::-webkit-scrollbar-thumb      { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ── Responsive ────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  main { padding: 0 0.75rem 2rem; margin-top: 1rem; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  thead th, td { padding: 0.5rem 0.6rem; }
  .header-meta .render-time { display: none; }
}
"""


# ---------------------------------------------------------------------------
# Embedded JavaScript  (AJAX polling, table rendering, log filtering)
# ---------------------------------------------------------------------------

_JAVASCRIPT: Final[str] = r"""
'use strict';

/* ── Utilities ─────────────────────────────────────────────────────────── */

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return '\u2014';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let v = Number(n);
  for (let i = 0; i < units.length - 1; i++) {
    if (Math.abs(v) < 1024) return i === 0 ? v + '\u00a0B' : v.toFixed(2) + '\u00a0' + units[i];
    v /= 1024;
  }
  return v.toFixed(2) + '\u00a0PB';
}

function fmtSpeed(bps) {
  return bps > 0 ? fmtBytes(bps) + '/s' : '\u2014';
}

function relTime(iso) {
  if (!iso) return '\u2014';
  const diff = new Date(iso).getTime() - Date.now();
  const abs  = Math.abs(diff);
  const past = diff < 0;
  const sign = past ? '' : 'in\u00a0';
  const suf  = past ? '\u00a0ago' : '';
  if (abs < 60000)    return past ? 'just now' : '<\u00a01m';
  if (abs < 3600000)  return sign + Math.round(abs / 60000)    + 'm'  + suf;
  if (abs < 86400000) return sign + Math.round(abs / 3600000)  + 'h'  + suf;
  return                      sign + Math.round(abs / 86400000) + 'd'  + suf;
}

function expCls(iso, isExpired, isExhausted) {
  if (isExpired || isExhausted) return 'exp-gone';
  if (!iso) return 'exp-ok';
  return (new Date(iso).getTime() - Date.now()) < 3_600_000 ? 'exp-soon' : 'exp-ok';
}

function typeCls(srcType) {
  if (!srcType)                 return 'type-FILE';
  if (srcType.includes('DIR')) return 'type-DIRECTORY';
  if (srcType.includes('FTP')) return 'type-FTP';
  return 'type-FILE';
}

function typeLabel(srcType) {
  if (!srcType)                 return 'FILE';
  if (srcType.includes('DIR')) return 'DIR';
  if (srcType.includes('FTP')) return 'FTP';
  return 'FILE';
}

/* ── API helpers ───────────────────────────────────────────────────────── */

async function apiGet(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP\u00a0' + r.status);
  return r.json();
}

async function apiDelete(path) {
  const r = await fetch(path, { method: 'DELETE', cache: 'no-store' });
  if (!r.ok && r.status !== 204) throw new Error('HTTP\u00a0' + r.status);
}

/* ── Status badge ──────────────────────────────────────────────────────── */

function setStatusBadge(ok) {
  const el = document.getElementById('status-badge');
  if (!el) return;
  el.className = 'badge badge-' + (ok ? 'ok' : 'error');
  el.innerHTML = '<span class="badge-dot"></span>' + (ok ? 'online' : 'offline');
}

/* ── Speed tracking (client-side, from cumulative byte deltas) ─────────── */

let _prevBytes = 0;
let _prevTime  = Date.now();

function computeSpeed(totalBytes) {
  const now = Date.now();
  const dtS = (now - _prevTime) / 1000;
  const bps = dtS > 0.1 ? Math.max(0, (totalBytes - _prevBytes) / dtS) : 0;
  _prevBytes = totalBytes;
  _prevTime  = now;
  return bps;
}

/* ── Stat cards ────────────────────────────────────────────────────────── */

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = (val != null && val !== '') ? val : '\u2014';
}

function updateStatCards(status, shares) {
  setText('sc-shares', status.active_shares);

  let totalDl = 0, activeDl = 0, totalBytes = 0;
  (shares || []).forEach(s => {
    const st = s.statistics || {};
    totalDl    += st.total_downloads          || 0;
    activeDl   += st.active_downloads         || 0;
    totalBytes += st.total_bytes_transferred  || 0;
  });

  const speed = computeSpeed(totalBytes);
  setText('sc-total-dl',  totalDl);
  setText('sc-active-dl', activeDl);
  setText('sc-speed',     fmtSpeed(speed));
  setText('sc-bytes',     fmtBytes(totalBytes));
}

/* ── Share table ───────────────────────────────────────────────────────── */

function buildShareRow(s) {
  const st    = s.statistics || {};
  const state = s.state || 'UNKNOWN';

  // --- Type badge ---
  const tc = typeCls(s.source_type);
  const tl = typeLabel(s.source_type);
  const typeBadge = `<span class="type-badge ${tc}">${esc(tl)}</span>`;

  // --- State badge ---
  const stateBadge = `<span class="state-badge state-${esc(state)}">${esc(state)}</span>`;

  // --- Downloads ---
  const remain = (s.downloads_remaining != null)
    ? `<span style="color:var(--text-dim);font-size:.7rem">\u00a0(${s.downloads_remaining} left)</span>`
    : '';
  const dlCell = `${st.total_downloads || 0}\u00a0/\u00a0${s.max_downloads || '?'}${remain}`;

  // --- Expiry ---
  let expHtml;
  if (s.is_expired) {
    expHtml = '<span class="exp-gone">expired</span>';
  } else if (s.is_exhausted) {
    expHtml = '<span class="exp-soon">exhausted</span>';
  } else {
    const cls = expCls(s.expires_at, false, false);
    expHtml = `<span class="${cls}" title="${esc(s.expires_at || '')}">${esc(relTime(s.expires_at))}</span>`;
  }

  // --- URL ---
  const urlHtml = s.public_url
    ? `<a class="url-link" href="${esc(s.public_url)}" target="_blank" rel="noopener noreferrer">${esc(s.public_url)}</a>`
    : '<span class="url-pending">pending\u2026</span>';

  // --- Revoke button ---
  const canRevoke = state === 'ACTIVE' || state === 'PENDING';
  const btnId     = 'btn-' + s.share_id;
  const btn       = canRevoke
    ? `<button id="${esc(btnId)}" class="btn btn-danger btn-sm" onclick="doRevoke('${esc(s.share_id)}')">Revoke</button>`
    : `<button class="btn btn-sm" disabled>Done</button>`;

  return `<tr data-id="${esc(s.share_id)}">
  <td class="td-file"><span title="${esc(s.source_path || '')}">${esc(s.filename || s.share_id)}</span></td>
  <td>${typeBadge}</td>
  <td>${stateBadge}</td>
  <td class="td-mono">${dlCell}</td>
  <td class="td-mono">${st.active_downloads || 0}</td>
  <td class="td-mono">${fmtBytes(st.total_bytes_transferred || 0)}</td>
  <td>${expHtml}</td>
  <td>${urlHtml}</td>
  <td>${btn}</td>
</tr>`;
}

function renderSharesTable(shares) {
  const tb = document.getElementById('shares-tbody');
  if (!tb) return;
  if (!shares || shares.length === 0) {
    tb.innerHTML = '<tr><td colspan="9" class="table-empty">No active shares.</td></tr>';
    return;
  }
  tb.innerHTML = shares.map(buildShareRow).join('\n');
}

/* ── Revoke action ─────────────────────────────────────────────────────── */

async function doRevoke(shareId) {
  const btn = document.getElementById('btn-' + shareId);
  if (btn) { btn.disabled = true; btn.textContent = 'Revoking\u2026'; }
  try {
    await apiDelete('/api/shares/' + encodeURIComponent(shareId));
    await refresh();
  } catch (err) {
    console.error('Revoke failed:', err);
    if (btn) { btn.disabled = false; btn.textContent = 'Revoke'; }
    // eslint-disable-next-line no-alert
    alert('Failed to revoke share: ' + err.message);
  }
}

/* ── Log filters ───────────────────────────────────────────────────────── */

function applyFilters() {
  const fReq  = document.getElementById('f-request')?.checked  ?? true;
  const fDl   = document.getElementById('f-download')?.checked ?? true;
  const fSys  = document.getElementById('f-system')?.checked   ?? true;
  const fWarn = document.getElementById('f-warn')?.checked     ?? false;

  document.querySelectorAll('#log-viewer .log-row').forEach(row => {
    const src  = row.dataset.src  || 'system';
    const warn = row.dataset.warn === '1';

    const srcOk = (src === 'request'  && fReq)
               || (src === 'download' && fDl)
               || (src === 'system'   && fSys)
               || !['request', 'download', 'system'].includes(src);
    const warnOk = !fWarn || warn;

    row.classList.toggle('hidden', !(srcOk && warnOk));
  });
}

['f-request', 'f-download', 'f-system', 'f-warn'].forEach(id => {
  document.getElementById(id)?.addEventListener('change', applyFilters);
});

/* ── Main refresh ──────────────────────────────────────────────────────── */

async function refresh() {
  try {
    const [status, payload] = await Promise.all([
      apiGet('/api/status'),
      apiGet('/api/shares'),
    ]);
    const shares = payload.shares || [];
    setStatusBadge(true);
    updateStatCards(status, shares);
    renderSharesTable(shares);
  } catch (err) {
    console.warn('Dashboard refresh failed:', err);
    setStatusBadge(false);
  }
}

/* ── Initialise ────────────────────────────────────────────────────────── */
refresh();
setInterval(refresh, POLL_MS);
applyFilters();
"""

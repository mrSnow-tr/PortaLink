"""
test_config.py
~~~~~~~~~~~~~~

Standalone test script for sharelink.ShareConfig.

Tests:
  - Default config values match module-level constants
  - Custom values are stored correctly
  - Frozen dataclass rejects attribute reassignment
  - mime_types is always wrapped in MappingProxyType
  - Invalid values (non-positive ints, bad strings, etc.) raise ValueError

Usage:
    python test_config.py

Exit codes:
    0  All tests passed
    1  One or more tests failed (or an unexpected exception occurred)
"""

import sys
import types

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_failures = 0


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")


def check(condition: bool, pass_msg: str, fail_msg: str) -> None:
    """Evaluate *condition* and print the appropriate PASS/FAIL line."""
    if condition:
        _pass(pass_msg)
    else:
        _fail(fail_msg)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

try:
    from sharelink.config import (
        ShareConfig,
        DEFAULT_EXPIRE_SECONDS,
        DEFAULT_MAX_DOWNLOADS,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_HOST,
        DEFAULT_PORT,
        DEFAULT_HTTP_TIMEOUT,
        DEFAULT_SESSION_SWEEP_INTERVAL,
        DEFAULT_RECONNECT_DELAY,
        DEFAULT_RECONNECT_RETRIES,
        DEFAULT_LOG_FILENAME,
        DEFAULT_LOG_ROTATION_WHEN,
        DEFAULT_LOG_BACKUP_COUNT,
        DEFAULT_CLOUDFLARED_BINARY_PATH,
        DEFAULT_MIME_FALLBACK,
        DEFAULT_MIME_TYPES,
        DEFAULT_LOG_DIRECTORY,
    )
    _pass("sharelink.config imported successfully.")
except Exception as exc:
    print(exc)
    _fail("Failed to import sharelink.config.")
    print("=================================")
    print("TEST RESULT : FAIL")
    print("=================================")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 1. Default config – spot-check every documented field
# ---------------------------------------------------------------------------

try:
    cfg = ShareConfig()

    check(cfg.host == DEFAULT_HOST,
          f"Default host is {DEFAULT_HOST!r}.",
          f"Default host wrong: {cfg.host!r}.")

    check(cfg.port == DEFAULT_PORT,
          f"Default port is {DEFAULT_PORT}.",
          f"Default port wrong: {cfg.port}.")

    check(cfg.expire_seconds == DEFAULT_EXPIRE_SECONDS,
          f"Default expire_seconds is {DEFAULT_EXPIRE_SECONDS}.",
          f"Default expire_seconds wrong: {cfg.expire_seconds}.")

    check(cfg.max_downloads == DEFAULT_MAX_DOWNLOADS,
          f"Default max_downloads is {DEFAULT_MAX_DOWNLOADS}.",
          f"Default max_downloads wrong: {cfg.max_downloads}.")

    check(cfg.chunk_size == DEFAULT_CHUNK_SIZE,
          f"Default chunk_size is {DEFAULT_CHUNK_SIZE}.",
          f"Default chunk_size wrong: {cfg.chunk_size}.")

    check(cfg.http_timeout == DEFAULT_HTTP_TIMEOUT,
          f"Default http_timeout is {DEFAULT_HTTP_TIMEOUT}.",
          f"Default http_timeout wrong: {cfg.http_timeout}.")

    check(cfg.session_sweep_interval == DEFAULT_SESSION_SWEEP_INTERVAL,
          f"Default session_sweep_interval is {DEFAULT_SESSION_SWEEP_INTERVAL}.",
          f"Default session_sweep_interval wrong: {cfg.session_sweep_interval}.")

    check(cfg.reconnect_delay == DEFAULT_RECONNECT_DELAY,
          f"Default reconnect_delay is {DEFAULT_RECONNECT_DELAY}.",
          f"Default reconnect_delay wrong: {cfg.reconnect_delay}.")

    check(cfg.reconnect_retries == DEFAULT_RECONNECT_RETRIES,
          f"Default reconnect_retries is {DEFAULT_RECONNECT_RETRIES}.",
          f"Default reconnect_retries wrong: {cfg.reconnect_retries}.")

    check(cfg.log_filename == DEFAULT_LOG_FILENAME,
          f"Default log_filename is {DEFAULT_LOG_FILENAME!r}.",
          f"Default log_filename wrong: {cfg.log_filename!r}.")

    check(cfg.log_rotation_when == DEFAULT_LOG_ROTATION_WHEN,
          f"Default log_rotation_when is {DEFAULT_LOG_ROTATION_WHEN!r}.",
          f"Default log_rotation_when wrong: {cfg.log_rotation_when!r}.")

    check(cfg.log_backup_count == DEFAULT_LOG_BACKUP_COUNT,
          f"Default log_backup_count is {DEFAULT_LOG_BACKUP_COUNT}.",
          f"Default log_backup_count wrong: {cfg.log_backup_count}.")

    check(cfg.mime_fallback == DEFAULT_MIME_FALLBACK,
          f"Default mime_fallback is {DEFAULT_MIME_FALLBACK!r}.",
          f"Default mime_fallback wrong: {cfg.mime_fallback!r}.")

except Exception as exc:
    print(exc)
    _fail("Unexpected exception while checking default config values.")

# ---------------------------------------------------------------------------
# 2. Custom values are stored correctly
# ---------------------------------------------------------------------------

try:
    from pathlib import Path

    custom = ShareConfig(
        host="0.0.0.0",
        port=9090,
        expire_seconds=3_600,
        max_downloads=5,
        chunk_size=131_072,
        http_timeout=60.0,
        session_sweep_interval=30,
        reconnect_delay=2.5,
        reconnect_retries=-1,          # unlimited
        log_filename="custom.log",
        log_rotation_when="midnight",
        log_backup_count=14,
        mime_fallback="application/octet-stream",
    )

    check(custom.host == "0.0.0.0",           "Custom host stored correctly.",           f"Custom host wrong: {custom.host!r}.")
    check(custom.port == 9090,                 "Custom port stored correctly.",           f"Custom port wrong: {custom.port}.")
    check(custom.expire_seconds == 3_600,      "Custom expire_seconds stored correctly.", f"Custom expire_seconds wrong: {custom.expire_seconds}.")
    check(custom.max_downloads == 5,           "Custom max_downloads stored correctly.",  f"Custom max_downloads wrong: {custom.max_downloads}.")
    check(custom.chunk_size == 131_072,        "Custom chunk_size stored correctly.",     f"Custom chunk_size wrong: {custom.chunk_size}.")
    check(custom.http_timeout == 60.0,         "Custom http_timeout stored correctly.",   f"Custom http_timeout wrong: {custom.http_timeout}.")
    check(custom.session_sweep_interval == 30, "Custom sweep interval stored correctly.", f"Custom sweep interval wrong: {custom.session_sweep_interval}.")
    check(custom.reconnect_delay == 2.5,       "Custom reconnect_delay stored correctly.",f"Custom reconnect_delay wrong: {custom.reconnect_delay}.")
    check(custom.reconnect_retries == -1,      "Custom reconnect_retries (-1) stored.",   f"Custom reconnect_retries wrong: {custom.reconnect_retries}.")
    check(custom.log_backup_count == 14,       "Custom log_backup_count stored.",         f"Custom log_backup_count wrong: {custom.log_backup_count}.")

except Exception as exc:
    print(exc)
    _fail("Unexpected exception while checking custom config values.")

# ---------------------------------------------------------------------------
# 3. Frozen dataclass – reassignment must raise
# ---------------------------------------------------------------------------

try:
    cfg_frozen = ShareConfig()
    raised = False
    try:
        cfg_frozen.port = 1234  # type: ignore[misc]
    except Exception:
        raised = True
    check(raised,
          "Frozen dataclass correctly prevents attribute reassignment.",
          "Frozen dataclass unexpectedly allowed attribute reassignment.")

except Exception as exc:
    print(exc)
    _fail("Unexpected exception while testing frozen dataclass.")

# ---------------------------------------------------------------------------
# 4. mime_types is always a MappingProxyType (even when dict supplied)
# ---------------------------------------------------------------------------

try:
    cfg_proxy = ShareConfig(mime_types={".xyz": "application/x-test"})
    check(
        isinstance(cfg_proxy.mime_types, types.MappingProxyType),
        "mime_types is wrapped in MappingProxyType when a plain dict is supplied.",
        "mime_types was NOT wrapped in MappingProxyType.",
    )
    # Default config must also be a MappingProxyType
    check(
        isinstance(ShareConfig().mime_types, types.MappingProxyType),
        "Default mime_types is a MappingProxyType.",
        "Default mime_types is NOT a MappingProxyType.",
    )

except Exception as exc:
    print(exc)
    _fail("Unexpected exception while testing mime_types immutability.")

# ---------------------------------------------------------------------------
# 5. Invalid values raise ValueError
# ---------------------------------------------------------------------------

INVALID_CASES = [
    # (description, kwargs that should trigger ValueError)
    ("port=0 (below range)",              {"port": 0}),
    ("port=65536 (above range)",          {"port": 65536}),
    ("expire_seconds=0",                  {"expire_seconds": 0}),
    ("expire_seconds=-1",                 {"expire_seconds": -1}),
    ("max_downloads=0",                   {"max_downloads": 0}),
    ("max_downloads=-1",                  {"max_downloads": -1}),
    ("chunk_size=0",                      {"chunk_size": 0}),
    ("http_timeout=0.0",                  {"http_timeout": 0.0}),
    ("http_timeout negative",             {"http_timeout": -1.0}),
    ("session_sweep_interval=0",          {"session_sweep_interval": 0}),
    ("reconnect_delay negative",          {"reconnect_delay": -0.1}),
    ("reconnect_retries=-2",              {"reconnect_retries": -2}),
    ("log_backup_count=-1",               {"log_backup_count": -1}),
    ("log_rotation_when invalid string",  {"log_rotation_when": "weekly"}),
    ("host empty string",                 {"host": ""}),
    ("mime_fallback empty string",        {"mime_fallback": ""}),
]

for description, bad_kwargs in INVALID_CASES:
    try:
        ShareConfig(**bad_kwargs)
        # If we reach here, no exception was raised – that's a failure
        _fail(f"No ValueError raised for: {description}.")
    except ValueError:
        _pass(f"ValueError raised correctly for: {description}.")
    except Exception as exc:
        print(exc)
        _fail(f"Wrong exception type for: {description}.")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print()
print("=================================")
if _failures == 0:
    print("TEST RESULT : PASS")
else:
    print("TEST RESULT : FAIL")
print("=================================")

sys.exit(0 if _failures == 0 else 1)

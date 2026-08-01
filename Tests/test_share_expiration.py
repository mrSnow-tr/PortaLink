"""
test_share_expiration.py
------------------------
Standalone test for PortaLink share expiration.

Steps:
  1. Create a temporary file.
  2. Create a share with a very short TTL (expire_seconds=5).
  3. Verify the share starts in ACTIVE state (not expired).
  4. Wait long enough for the share to expire.
  5. Confirm is_expired is True and is_active is False.
  6. Clean up all resources (temp file, manager).

Exit code: 0 on full success, 1 on any failure.
"""

import sys
import tempfile
import time
import os

# ── Helpers ──────────────────────────────────────────────────────────────────

failures = 0  # global counter; incremented by check()


def check(condition: bool, pass_msg: str, fail_msg: str) -> bool:
    """Print PASS or FAIL and update the global failure counter."""
    global failures
    if condition:
        print(f"[PASS] {pass_msg}")
        return True
    else:
        print(f"[FAIL] {fail_msg}")
        failures += 1
        return False


def die(exc: Exception, context: str) -> None:
    """Print the exception, a FAIL line, the summary, and exit with code 1."""
    global failures
    print(f"Exception in {context}: {exc}")
    print(f"[FAIL] Unexpected exception during: {context}")
    failures += 1
    print_summary()
    sys.exit(1)


def print_summary() -> None:
    if failures == 0:
        print("\n=================================")
        print("TEST RESULT : PASS")
        print("=================================")
    else:
        print("\n=================================")
        print("TEST RESULT : FAIL")
        print("=================================")


# ── Step 1 – import PortaLink ─────────────────────────────────────────────────

try:
    from sharelink import ShareManager, ShareConfig, ShareState
    check(True, "PortaLink imported successfully.", "")
except Exception as exc:
    die(exc, "importing PortaLink")

# ── Step 2 – create a temporary file ─────────────────────────────────────────

tmp_file = None
tmp_dir  = None

try:
    # Use a named temp file that persists until we delete it explicitly.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="portaLink_test_")
    os.write(tmp_fd, b"PortaLink expiration test payload.\n")
    os.close(tmp_fd)
    check(True, f"Temporary file created at {tmp_path}", "")
except Exception as exc:
    die(exc, "creating temporary file")

# ── Step 3 – create a share with a very short expiry ─────────────────────────

EXPIRE_SECONDS = 5          # TTL given to the share
POLL_INTERVAL  = 1.0        # seconds between expiry checks
MAX_WAIT       = 20         # give up after this many seconds

manager = None
share   = None

try:
    # Use wait_for_url=False so we don't block on the Cloudflare tunnel.
    # The tunnel URL is irrelevant for this test; we only test state transitions.
    config  = ShareConfig(expire_seconds=EXPIRE_SECONDS)
    manager = ShareManager(config=config)

    share = manager.create_share(
        tmp_path,
        expire_seconds=EXPIRE_SECONDS,
        wait_for_url=False,   # don't block waiting for cloudflared
    )
    check(True, "Share created successfully.", "Share creation failed.")
except Exception as exc:
    # Clean up the temp file before dying.
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    die(exc, "creating share")

# ── Step 4 – verify the share starts as ACTIVE and not expired ────────────────

try:
    assert share.state == ShareState.ACTIVE, (
        f"Expected ACTIVE, got {share.state.name}"
    )
    check(True, "Share is initially ACTIVE.", "Share did not start as ACTIVE.")
except AssertionError as exc:
    print(f"AssertionError: {exc}")
    check(False, "", "Share did not start as ACTIVE.")
except Exception as exc:
    die(exc, "checking initial share state")

try:
    assert not share.is_expired, "Share is already expired right after creation."
    check(True, "Share is NOT expired immediately after creation.", "")
except AssertionError as exc:
    print(f"AssertionError: {exc}")
    check(False, "", "Share is already expired right after creation.")
except Exception as exc:
    die(exc, "checking initial is_expired")

try:
    assert share.is_active, "Share.is_active should be True right after creation."
    check(True, "Share.is_active is True initially.", "")
except AssertionError as exc:
    print(f"AssertionError: {exc}")
    check(False, "", "Share.is_active was False right after creation.")
except Exception as exc:
    die(exc, "checking initial is_active")

# ── Step 5 – wait for the share to expire ────────────────────────────────────

print(
    f"\nWaiting up to {MAX_WAIT}s for the share to expire "
    f"(TTL={EXPIRE_SECONDS}s) …"
)

expired_detected = False
elapsed = 0.0

try:
    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        # is_expired reflects both the state flag and the real-time clock,
        # so it becomes True at expires_at even before the timer thread fires.
        if share.is_expired:
            expired_detected = True
            break

    check(
        expired_detected,
        f"Share expired after ~{elapsed:.0f}s (TTL was {EXPIRE_SECONDS}s).",
        f"Share did NOT expire within {MAX_WAIT}s.",
    )
except Exception as exc:
    die(exc, "polling for share expiration")

# ── Step 6 – confirm post-expiry state ───────────────────────────────────────

try:
    assert share.is_expired, "share.is_expired should be True after TTL elapsed."
    check(True, "share.is_expired is True after TTL.", "")
except AssertionError as exc:
    print(f"AssertionError: {exc}")
    check(False, "", "share.is_expired was not True after TTL elapsed.")
except Exception as exc:
    die(exc, "checking is_expired post-expiry")

try:
    assert not share.is_active, "share.is_active should be False after expiry."
    check(True, "share.is_active is False after expiry.", "")
except AssertionError as exc:
    print(f"AssertionError: {exc}")
    check(False, "", "share.is_active was still True after expiry.")
except Exception as exc:
    die(exc, "checking is_active post-expiry")

# ── Step 7 – clean up ────────────────────────────────────────────────────────

try:
    manager.shutdown()
    check(True, "ShareManager shut down cleanly.", "")
except Exception as exc:
    die(exc, "shutting down ShareManager")

try:
    os.unlink(tmp_path)
    check(True, "Temporary file removed.", "")
except Exception as exc:
    # Non-fatal – the OS will clean it up eventually.
    print(f"Warning: could not remove temp file {tmp_path}: {exc}")

# ── Summary ───────────────────────────────────────────────────────────────────

print_summary()
sys.exit(0 if failures == 0 else 1)

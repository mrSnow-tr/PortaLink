"""
test_cleanup.py
---------------
Tests that Sharelink's internal share manager correctly expires and
cleans up shares after their TTL elapses.

Strategy
--------
1. Import the internal ShareManager (manager.py) directly so we can call
   cleanup_expired() and cleanup_finished() without a real Cloudflare
   tunnel or HTTP server.
2. Create several ShareSession objects whose ShareInfo already has an
   expires_at in the past, simulating expired shares.
3. Run cleanup_expired() → verify sessions are expired.
4. Run cleanup_finished() → verify sessions are removed from the registry.
5. Confirm no active resources remain.

We bypass the tunnel / HTTP server by patching ShareSession.start() so
it transitions state without actually binding sockets or spawning
cloudflared.  Sharelink source files are NEVER modified.
"""

import sys
import tempfile
import time
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Track overall pass / fail
# ---------------------------------------------------------------------------
_failures = 0

def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")

def _fail(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")

def _check(condition: bool, pass_msg: str, fail_msg: str) -> None:
    if condition:
        _pass(pass_msg)
    else:
        _fail(fail_msg)

# ---------------------------------------------------------------------------
# Locate the Sharelink package (parent directory of this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ---------------------------------------------------------------------------
# Import Sharelink internals
# ---------------------------------------------------------------------------
try:
    from sharelink.manager import ShareManager
    from sharelink.models import ShareInfo, ShareState, ShareStatistics, SourceType
    from sharelink.config import ShareConfig
    from sharelink.session import ShareSession
    from sharelink.utils import generate_share_id, utc_now
    _pass("Sharelink internals imported successfully.")
except Exception as exc:
    print(exc)
    _fail("Failed to import Sharelink internals.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helper: build a ShareInfo that is already past its expiry time
# ---------------------------------------------------------------------------
def _make_expired_share_info(tmp_file: Path) -> ShareInfo:
    """Return a ShareInfo whose expires_at is 10 seconds in the past."""
    now = utc_now()
    return ShareInfo(
        share_id=generate_share_id(),
        source_path=str(tmp_file),
        source_type=SourceType.LOCAL_FILE,
        state=ShareState.PENDING,
        token="test-token-" + generate_share_id(8),
        created_at=now - timedelta(seconds=60),
        expires_at=now - timedelta(seconds=10),   # already expired
        max_downloads=5,
        statistics=ShareStatistics(),
    )

# ---------------------------------------------------------------------------
# Helper: build a ShareSession that is pre-started without real I/O
#
# We monkeypatch the instance's start() so it only sets state to ACTIVE
# and marks _running=True, skipping the actual HTTP server and tunnel.
# Sharelink source code is never touched.
# ---------------------------------------------------------------------------
def _make_lightweight_session(share_info: ShareInfo, manager: ShareManager) -> ShareSession:
    """Create a session wired into *manager* but without real I/O."""
    session = ShareSession(
        share_info=share_info,
        config=manager._config,
        on_state_change=manager._on_session_state_change,
    )

    # Patch start() on this *instance* only (not the class).
    def _fake_start():
        with session._lock:
            session._info.state = ShareState.ACTIVE
            session._running = True

    session.start = _fake_start  # type: ignore[method-assign]
    return session

# ---------------------------------------------------------------------------
# Main test body
# ---------------------------------------------------------------------------
tmpdir = None
try:
    # Create a temporary directory with a few dummy files to use as share sources.
    tmpdir = tempfile.mkdtemp(prefix="portaink_test_")
    dummy_files = []
    for i in range(3):
        p = Path(tmpdir) / f"dummy_{i}.txt"
        p.write_text(f"dummy content {i}")
        dummy_files.append(p)

    # Build a ShareManager with a fast sweep interval so we don't have to wait long.
    config = ShareConfig(
        expire_seconds=1,           # default TTL (overridden per-share anyway)
        session_sweep_interval=1,   # sweep every second
    )
    manager = ShareManager(config=config)

    # ------------------------------------------------------------------
    # 1. Register N expired sessions directly into the manager registry.
    # ------------------------------------------------------------------
    NUM_SHARES = 3
    sessions = []
    for i in range(NUM_SHARES):
        info = _make_expired_share_info(dummy_files[i])
        session = _make_lightweight_session(info, manager)
        session.start()   # calls our fake start
        with manager._lock:
            manager._sessions[info.share_id] = session
        sessions.append(session)

    _check(
        manager.total_share_count == NUM_SHARES,
        f"All {NUM_SHARES} sessions registered in the manager.",
        f"Expected {NUM_SHARES} sessions, got {manager.total_share_count}.",
    )

    # ------------------------------------------------------------------
    # 2. Confirm they are all flagged as past expiry before cleanup.
    # ------------------------------------------------------------------
    all_past_expiry = all(s.share_info.is_past_expiry for s in sessions)
    _check(
        all_past_expiry,
        "All sessions are past their expiry time (pre-cleanup check).",
        "Some sessions are not past their expiry time — test setup error.",
    )

    # ------------------------------------------------------------------
    # 3. Run cleanup_expired() — should transition each to EXPIRED.
    # ------------------------------------------------------------------
    expired_count = manager.cleanup_expired()
    _check(
        expired_count == NUM_SHARES,
        f"cleanup_expired() reported {expired_count} newly expired sessions.",
        f"Expected {NUM_SHARES} newly expired, got {expired_count}.",
    )

    # Give the expire() calls (which call stop() internally) a moment to finish.
    time.sleep(0.5)

    # Verify state on each session.
    all_expired = all(s.share_info.state == ShareState.EXPIRED for s in sessions)
    _check(
        all_expired,
        "All sessions transitioned to EXPIRED state.",
        "Some sessions did not reach EXPIRED state after cleanup_expired().",
    )

    # Verify none are still running.
    none_running = all(not s.is_running for s in sessions)
    _check(
        none_running,
        "No sessions are still running after expiry.",
        "Some sessions are still marked as running after expiry.",
    )

    # ------------------------------------------------------------------
    # 4. Run cleanup_finished() — should remove them from the registry.
    # ------------------------------------------------------------------
    removed_count = manager.cleanup_finished()
    _check(
        removed_count == NUM_SHARES,
        f"cleanup_finished() removed {removed_count} terminal sessions.",
        f"Expected {NUM_SHARES} removed, got {removed_count}.",
    )

    _check(
        manager.total_share_count == 0,
        "Registry is empty after cleanup_finished().",
        f"Registry still has {manager.total_share_count} session(s) after cleanup.",
    )

    _check(
        manager.active_share_count == 0,
        "No active shares remain.",
        f"{manager.active_share_count} active share(s) remain after cleanup.",
    )

    # ------------------------------------------------------------------
    # 5. Running cleanup again on an empty registry should be harmless.
    # ------------------------------------------------------------------
    try:
        extra_expired = manager.cleanup_expired()
        extra_removed = manager.cleanup_finished()
        _check(
            extra_expired == 0 and extra_removed == 0,
            "Second cleanup pass on empty registry is a safe no-op.",
            f"Second cleanup pass unexpectedly reported expired={extra_expired}, "
            f"removed={extra_removed}.",
        )
    except Exception as exc:
        print(exc)
        _fail("Second cleanup pass raised an unexpected exception.")

    # ------------------------------------------------------------------
    # 6. Shut down the manager cleanly.
    # ------------------------------------------------------------------
    try:
        manager.stop()
        _pass("ShareManager stopped cleanly.")
    except Exception as exc:
        print(exc)
        _fail("ShareManager.stop() raised an exception.")

except AssertionError as exc:
    print(exc)
    _fail("Assertion error during test execution.")
    sys.exit(1)
except Exception as exc:
    print(exc)
    _fail("Unexpected exception during test execution.")
    sys.exit(1)
finally:
    # ------------------------------------------------------------------
    # Clean up temporary directory regardless of outcome.
    # ------------------------------------------------------------------
    if tmpdir and os.path.isdir(tmpdir):
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

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

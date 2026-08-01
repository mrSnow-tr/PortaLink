"""
test_statistics.py
------------------
Standalone test for the Sharelink share statistics API.

Steps:
  1. Create a temporary file to share.
  2. Create a share via ShareManager with wait_for_url=False so the test
     does not hang waiting for a Cloudflare tunnel (tunnel is not required
     for statistics inspection).
  3. Retrieve the share via list_shares() and get_share().
  4. Inspect the ShareStatistics object and the Share's to_dict() snapshot.
  5. Verify field presence, types, and sane initial values.
  6. Clean up: revoke the share, stop the manager, delete the temp file.

Exit code 0 = all checks passed.
Exit code 1 = at least one check failed or an unexpected exception occurred.
"""

import sys
import tempfile
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "[PASS]"
FAIL = "[FAIL]"
_failures = 0


def check(condition: bool, pass_msg: str, fail_msg: str) -> bool:
    """Print a PASS/FAIL line and track global failure count."""
    global _failures
    if condition:
        print(f"{PASS} {pass_msg}")
        return True
    else:
        print(f"{FAIL} {fail_msg}")
        _failures += 1
        return False


def assert_check(value, pass_msg: str, fail_msg: str):
    """Wrap assert-style checks so AssertionError produces a readable FAIL."""
    global _failures
    try:
        assert value
        print(f"{PASS} {pass_msg}")
    except AssertionError:
        print(f"{FAIL} {fail_msg}")
        _failures += 1


# ---------------------------------------------------------------------------
# Main test body
# ---------------------------------------------------------------------------

def run_tests():
    global _failures

    # ------------------------------------------------------------------
    # 0. Import Sharelink
    # ------------------------------------------------------------------
    try:
        from sharelink import ShareManager, ShareState, ShareStatistics, SourceType
        print(f"{PASS} Sharelink imported successfully.")
    except Exception as exc:
        print(exc)
        print(f"{FAIL} Could not import Sharelink.")
        return

    # ------------------------------------------------------------------
    # 1. Create a temporary file
    # ------------------------------------------------------------------
    tmp_file = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="sharelink_test_")
        os.write(fd, b"Sharelink statistics test file.\n" * 10)
        os.close(fd)
        tmp_file = tmp_path
        print(f"{PASS} Temporary file created: {tmp_path}")
    except Exception as exc:
        print(exc)
        print(f"{FAIL} Could not create temporary file.")
        return

    manager = None
    share = None

    try:
        # ------------------------------------------------------------------
        # 2. Start ShareManager and create a share
        #    wait_for_url=False: skip blocking on Cloudflare tunnel so the
        #    test runs without a live internet connection.
        # ------------------------------------------------------------------
        try:
            manager = ShareManager()
            print(f"{PASS} ShareManager created.")
        except Exception as exc:
            print(exc)
            print(f"{FAIL} ShareManager could not be created.")
            return

        try:
            share = manager.create_share(
                tmp_path,
                expire_seconds=3600,   # 1 hour
                max_downloads=5,
                wait_for_url=False,    # do not block on tunnel
            )
            print(f"{PASS} Share created (id={share.share_id}).")
        except Exception as exc:
            print(exc)
            print(f"{FAIL} create_share() raised an exception.")
            return

        # ------------------------------------------------------------------
        # 3. Verify share is findable via list_shares() and get_share()
        # ------------------------------------------------------------------
        listed = manager.list_shares()
        check(
            any(s.share_id == share.share_id for s in listed),
            "Share appears in list_shares().",
            "Share NOT found in list_shares().",
        )

        fetched = manager.get_share(share.share_id)
        check(
            fetched is not None,
            "get_share() returned a Share object.",
            "get_share() returned None.",
        )

        # ------------------------------------------------------------------
        # 4. Inspect ShareStatistics object
        # ------------------------------------------------------------------
        try:
            stats = share.statistics
            print(f"{PASS} share.statistics accessed without error.")
        except Exception as exc:
            print(exc)
            print(f"{FAIL} Accessing share.statistics raised an exception.")
            return

        # Type check
        check(
            isinstance(stats, ShareStatistics),
            "share.statistics is a ShareStatistics instance.",
            f"share.statistics is {type(stats)}, not ShareStatistics.",
        )

        # Integer counters start at zero
        assert_check(
            isinstance(stats.total_downloads, int) and stats.total_downloads == 0,
            "total_downloads is int and equals 0 at creation.",
            f"total_downloads unexpected: {stats.total_downloads!r}",
        )
        assert_check(
            isinstance(stats.completed_downloads, int) and stats.completed_downloads == 0,
            "completed_downloads is int and equals 0 at creation.",
            f"completed_downloads unexpected: {stats.completed_downloads!r}",
        )
        assert_check(
            isinstance(stats.active_downloads, int) and stats.active_downloads == 0,
            "active_downloads is int and equals 0 at creation.",
            f"active_downloads unexpected: {stats.active_downloads!r}",
        )
        assert_check(
            isinstance(stats.total_bytes_transferred, int)
            and stats.total_bytes_transferred == 0,
            "total_bytes_transferred is int and equals 0 at creation.",
            f"total_bytes_transferred unexpected: {stats.total_bytes_transferred!r}",
        )

        # unique_ips should be an iterable (frozenset in the public API)
        assert_check(
            hasattr(stats.unique_ips, '__iter__') and len(stats.unique_ips) == 0,
            "unique_ips is iterable and empty at creation.",
            f"unique_ips unexpected: {stats.unique_ips!r}",
        )

        # Timestamps must be None before any access
        assert_check(
            stats.first_accessed is None,
            "first_accessed is None before any downloads.",
            f"first_accessed should be None, got {stats.first_accessed!r}",
        )
        assert_check(
            stats.last_accessed is None,
            "last_accessed is None before any downloads.",
            f"last_accessed should be None, got {stats.last_accessed!r}",
        )

        # ------------------------------------------------------------------
        # 5. Inspect to_dict() snapshot
        # ------------------------------------------------------------------
        try:
            d = share.to_dict()
            print(f"{PASS} share.to_dict() returned without error.")
        except Exception as exc:
            print(exc)
            print(f"{FAIL} share.to_dict() raised an exception.")
            return

        # Required top-level keys
        required_keys = [
            "share_id", "source_path", "source_type", "state",
            "max_downloads", "downloads_remaining", "is_expired",
            "is_exhausted", "statistics",
        ]
        for key in required_keys:
            check(key in d, f"to_dict() contains key '{key}'.",
                  f"to_dict() missing key '{key}'.")

        # statistics sub-dict keys
        stats_d = d.get("statistics", {})
        stat_keys = [
            "total_downloads", "completed_downloads", "active_downloads",
            "total_bytes_transferred", "unique_ips",
        ]
        for key in stat_keys:
            check(key in stats_d,
                  f"statistics dict contains key '{key}'.",
                  f"statistics dict missing key '{key}'.")

        # Validate a few values in the dict
        assert_check(
            d.get("share_id") == share.share_id,
            "to_dict() share_id matches share.share_id.",
            "to_dict() share_id mismatch.",
        )
        assert_check(
            d.get("max_downloads") == 5,
            "to_dict() max_downloads equals 5 as configured.",
            f"to_dict() max_downloads is {d.get('max_downloads')!r}, expected 5.",
        )
        assert_check(
            d.get("downloads_remaining") == 5,
            "to_dict() downloads_remaining equals 5 (no downloads yet).",
            f"to_dict() downloads_remaining is {d.get('downloads_remaining')!r}.",
        )
        assert_check(
            d.get("is_expired") is False,
            "to_dict() is_expired is False for a fresh share.",
            f"to_dict() is_expired is {d.get('is_expired')!r}.",
        )
        assert_check(
            d.get("is_exhausted") is False,
            "to_dict() is_exhausted is False for a fresh share.",
            f"to_dict() is_exhausted is {d.get('is_exhausted')!r}.",
        )

        # ------------------------------------------------------------------
        # 6. Verify share-level convenience properties
        # ------------------------------------------------------------------
        assert_check(
            share.downloads_remaining == 5,
            "share.downloads_remaining equals 5.",
            f"share.downloads_remaining is {share.downloads_remaining!r}.",
        )
        assert_check(
            share.max_downloads == 5,
            "share.max_downloads equals 5.",
            f"share.max_downloads is {share.max_downloads!r}.",
        )
        assert_check(
            share.source_type == SourceType.LOCAL_FILE,
            "share.source_type is LOCAL_FILE.",
            f"share.source_type is {share.source_type!r}.",
        )
        assert_check(
            isinstance(share.created_at, datetime)
            and share.created_at.tzinfo is not None,
            "share.created_at is a timezone-aware datetime.",
            f"share.created_at is {share.created_at!r}.",
        )
        assert_check(
            isinstance(share.expires_at, datetime)
            and share.expires_at.tzinfo is not None,
            "share.expires_at is a timezone-aware datetime.",
            f"share.expires_at is {share.expires_at!r}.",
        )
        assert_check(
            share.expires_at > share.created_at,
            "share.expires_at is after share.created_at.",
            "share.expires_at is NOT after share.created_at.",
        )
        assert_check(
            share.is_active,
            "share.is_active is True immediately after creation.",
            f"share.is_active is {share.is_active!r}.",
        )
        assert_check(
            not share.is_expired,
            "share.is_expired is False for a fresh share.",
            "share.is_expired is unexpectedly True.",
        )
        assert_check(
            not share.is_exhausted,
            "share.is_exhausted is False for a fresh share.",
            "share.is_exhausted is unexpectedly True.",
        )

        # ------------------------------------------------------------------
        # 7. Revoke the share and check state
        # ------------------------------------------------------------------
        result = manager.delete_share(share.share_id)
        check(result is True,
              "delete_share() returned True.",
              f"delete_share() returned {result!r}, expected True.")

        # After deletion the share should no longer appear in list_shares()
        listed_after = manager.list_shares()
        check(
            all(s.share_id != share.share_id for s in listed_after),
            "Share is absent from list_shares() after deletion.",
            "Share still appears in list_shares() after deletion.",
        )

    finally:
        # ------------------------------------------------------------------
        # Cleanup: stop the manager and remove the temporary file
        # ------------------------------------------------------------------
        if manager is not None:
            try:
                manager.shutdown()
                print(f"{PASS} ShareManager shut down cleanly.")
            except Exception as exc:
                print(exc)
                print(f"{FAIL} ShareManager.shutdown() raised an exception.")
                _failures += 1

        if tmp_file and os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
                print(f"{PASS} Temporary file removed.")
            except Exception as exc:
                print(exc)
                print(f"{FAIL} Could not remove temporary file: {tmp_file}")
                _failures += 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_tests()

    print()
    print("=================================")
    if _failures == 0:
        print("TEST RESULT : PASS")
    else:
        print("TEST RESULT : FAIL")
    print("=================================")

    sys.exit(0 if _failures == 0 else 1)

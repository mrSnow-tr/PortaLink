"""
test_manager.py
~~~~~~~~~~~~~~~

Standalone test for the ShareManager public API.

Verifies:
  1. ShareManager can be imported.
  2. A temporary file share can be created.
  3. The share is retrievable by its ID via get_share().
  4. The share appears in list_shares().
  5. The share can be deleted via delete_share().
  6. After deletion the share is no longer in list_shares().
  7. get_share() returns None for a deleted share ID.
  8. The manager shuts down cleanly via shutdown().

No network connections or Cloudflare tunnels are needed because we use
wait_for_url=False and url_timeout=0, so the manager never blocks waiting
for cloudflared.  We test only the registry / lifecycle layer.

Usage:
    python test_manager.py
"""

import sys
import os
import tempfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_all_passed = True  # Track overall result.


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    global _all_passed
    _all_passed = False
    print(f"[FAIL] {msg}")


def _summarise() -> None:
    print()
    print("=================================")
    if _all_passed:
        print("TEST RESULT : PASS")
    else:
        print("TEST RESULT : FAIL")
    print("=================================")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _check(condition: bool, pass_msg: str, fail_msg: str) -> bool:
    """Evaluate condition, print PASS/FAIL, return the boolean result."""
    if condition:
        _pass(pass_msg)
    else:
        _fail(fail_msg)
    return condition


# ---------------------------------------------------------------------------
# Main test body
# ---------------------------------------------------------------------------

def run_tests() -> None:
    # ------------------------------------------------------------------
    # 1. Import sharelink
    # ------------------------------------------------------------------
    try:
        from sharelink import ShareManager, ShareState
        _pass("sharelink imported successfully.")
    except Exception as exc:
        print(exc)
        _fail("Could not import sharelink.")
        _summarise()
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Create a temporary file to share.
    # ------------------------------------------------------------------
    tmp_file = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="sharelink_test_")
        os.write(tmp_fd, b"sharelink test content\n")
        os.close(tmp_fd)
        tmp_file = tmp_path
        _pass(f"Temporary file created: {tmp_path}")
    except Exception as exc:
        print(exc)
        _fail("Could not create a temporary file.")
        _summarise()
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Instantiate ShareManager and create a share.
    #    wait_for_url=False so we never block on cloudflared.
    # ------------------------------------------------------------------
    manager = None
    share = None
    try:
        manager = ShareManager()
        _pass("ShareManager instantiated.")
    except Exception as exc:
        print(exc)
        _fail("ShareManager could not be instantiated.")
        _cleanup(tmp_file, manager)
        _summarise()
        sys.exit(1)

    try:
        share = manager.create_share(
            tmp_file,
            expire_seconds=3600,
            max_downloads=5,
            wait_for_url=False,   # Do not block; tunnel is not required.
            url_timeout=0,
        )
        _pass("create_share() returned a Share object.")
    except Exception as exc:
        print(exc)
        _fail("create_share() raised an exception.")
        _cleanup(tmp_file, manager)
        _summarise()
        sys.exit(1)

    share_id = share.share_id

    # ------------------------------------------------------------------
    # 4. Verify basic share properties.
    # ------------------------------------------------------------------
    try:
        assert isinstance(share_id, str) and share_id, "share_id must be a non-empty string"
        _pass(f"share.share_id is a non-empty string: {share_id!r}")
    except AssertionError as exc:
        _fail(str(exc))

    try:
        assert share.state in (ShareState.PENDING, ShareState.ACTIVE), \
            f"Expected PENDING or ACTIVE state, got {share.state}"
        _pass(f"share.state is {share.state.name} (expected PENDING or ACTIVE).")
    except AssertionError as exc:
        _fail(str(exc))

    try:
        assert share.max_downloads == 5, \
            f"Expected max_downloads=5, got {share.max_downloads}"
        _pass("share.max_downloads == 5.")
    except AssertionError as exc:
        _fail(str(exc))

    # ------------------------------------------------------------------
    # 5. Retrieve the share by ID.
    # ------------------------------------------------------------------
    try:
        retrieved = manager.get_share(share_id)
        _check(
            retrieved is not None,
            f"get_share({share_id!r}) returned the share.",
            f"get_share({share_id!r}) returned None — share not found.",
        )
        if retrieved is not None:
            _check(
                retrieved.share_id == share_id,
                "Retrieved share has the correct share_id.",
                f"Retrieved share has wrong share_id: {retrieved.share_id!r}",
            )
    except Exception as exc:
        print(exc)
        _fail("get_share() raised an unexpected exception.")

    # ------------------------------------------------------------------
    # 6. Verify share appears in list_shares().
    # ------------------------------------------------------------------
    try:
        all_shares = manager.list_shares()
        ids_in_list = [s.share_id for s in all_shares]
        _check(
            share_id in ids_in_list,
            "Share appears in list_shares().",
            f"Share {share_id!r} not found in list_shares() result: {ids_in_list}",
        )
    except Exception as exc:
        print(exc)
        _fail("list_shares() raised an unexpected exception.")

    # ------------------------------------------------------------------
    # 7. Delete the share.
    # ------------------------------------------------------------------
    try:
        deleted = manager.delete_share(share_id)
        _check(
            deleted is True,
            "delete_share() returned True (share was found and deleted).",
            f"delete_share() returned {deleted!r} — expected True.",
        )
    except Exception as exc:
        print(exc)
        _fail("delete_share() raised an unexpected exception.")

    # ------------------------------------------------------------------
    # 8. Confirm the share is gone from list_shares().
    # ------------------------------------------------------------------
    try:
        remaining = manager.list_shares()
        remaining_ids = [s.share_id for s in remaining]
        _check(
            share_id not in remaining_ids,
            "Deleted share no longer appears in list_shares().",
            f"Deleted share {share_id!r} still found in list_shares(): {remaining_ids}",
        )
    except Exception as exc:
        print(exc)
        _fail("list_shares() raised an unexpected exception after deletion.")

    # ------------------------------------------------------------------
    # 9. Confirm get_share() returns None for the deleted ID.
    # ------------------------------------------------------------------
    try:
        ghost = manager.get_share(share_id)
        _check(
            ghost is None,
            "get_share() returns None for the deleted share ID.",
            f"get_share() returned {ghost!r} for a deleted share — expected None.",
        )
    except Exception as exc:
        print(exc)
        _fail("get_share() raised an unexpected exception after deletion.")

    # ------------------------------------------------------------------
    # 10. Deleting a non-existent share returns False.
    # ------------------------------------------------------------------
    try:
        result = manager.delete_share("nonexistent-share-id-12345")
        _check(
            result is False,
            "delete_share() returns False for a non-existent share ID.",
            f"delete_share() returned {result!r} for a fake ID — expected False.",
        )
    except Exception as exc:
        print(exc)
        _fail("delete_share() raised an exception for a non-existent share ID.")

    # ------------------------------------------------------------------
    # 11. Shut the manager down cleanly.
    # ------------------------------------------------------------------
    try:
        manager.shutdown()
        _pass("manager.shutdown() completed without error.")
        manager = None  # Prevent double-shutdown in cleanup.
    except Exception as exc:
        print(exc)
        _fail("manager.shutdown() raised an exception.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    _cleanup(tmp_file, manager)


def _cleanup(tmp_file, manager) -> None:
    """Remove the temporary file and shut down the manager if still live."""
    if manager is not None:
        try:
            manager.shutdown()
        except Exception:
            pass

    if tmp_file and os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_tests()
    except Exception as exc:
        print(exc)
        _fail("Unexpected top-level exception — see above.")

    _summarise()
    sys.exit(0 if _all_passed else 1)

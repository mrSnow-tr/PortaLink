"""
test_local_file.py
------------------
Standalone test for PortaLink local-file sharing.

Steps:
  1. Create a temporary file with known content.
  2. Create a share for that file via ShareManager.
  3. Verify the share is ACTIVE.
  4. Verify a public URL has been generated.
  5. Verify the share filename matches the temporary file's name.
  6. Verify the reported file size matches the actual file size.
  7. Clean up the share and all temporary resources.

Exit codes:
  0 — all checks passed
  1 — one or more checks failed or an unexpected exception occurred
"""

import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_failures = 0  # tally of failed checks


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")


def _check(condition: bool, pass_msg: str, fail_msg: str) -> None:
    """Print a PASS/FAIL line based on *condition*."""
    if condition:
        _pass(pass_msg)
    else:
        _fail(fail_msg)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_tests() -> None:
    # ------------------------------------------------------------------
    # 1. Import PortaLink
    # ------------------------------------------------------------------
    try:
        # Add the project root to sys.path so the package is importable
        # when the script is run from any working directory.
        project_root = os.path.dirname(os.path.abspath(__file__))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        from sharelink import ShareManager, ShareState, ShareConfig
        _pass("PortaLink imported successfully.")
    except Exception as exc:
        print(exc)
        _fail("Could not import PortaLink.")
        return  # Cannot continue without the package.

    # ------------------------------------------------------------------
    # 2. Create a temporary file with known content
    # ------------------------------------------------------------------
    tmp_file = None
    manager = None
    share = None

    try:
        # Use delete=False so we control the lifetime ourselves.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="portaLink_test_")
        known_content = b"PortaLink local-file share test.\n" * 100  # ~3 300 bytes
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(known_content)
        tmp_file = tmp_path
        _pass(f"Temporary file created: {tmp_path} ({len(known_content)} bytes).")
    except Exception as exc:
        print(exc)
        _fail("Could not create temporary file.")
        return

    try:
        # ------------------------------------------------------------------
        # 3. Create a ShareManager and a share
        #    wait_for_url=False so we don't block on the Cloudflare tunnel.
        # ------------------------------------------------------------------
        cfg = ShareConfig(
            expire_seconds=300,   # 5-minute lifetime — plenty for the test
            max_downloads=5,
        )
        manager = ShareManager(config=cfg)
        _pass("ShareManager created.")

        share = manager.create_share(
            tmp_path,
            wait_for_url=False,   # Don't block; tunnel may not be available
        )
        _pass("Share created without error.")

        # ------------------------------------------------------------------
        # 4. Verify share state is ACTIVE
        # ------------------------------------------------------------------
        try:
            assert share.state == ShareState.ACTIVE, (
                f"Expected ACTIVE, got {share.state.name}"
            )
            _pass(f"Share state is ACTIVE (share_id={share.share_id}).")
        except AssertionError as exc:
            _fail(f"Share state check failed: {exc}")

        # ------------------------------------------------------------------
        # 5. Verify a public URL was generated
        #    The tunnel may not be available in CI, so we only require that
        #    share.public_url is a non-empty string once we try to wait briefly.
        # ------------------------------------------------------------------
        url = share.wait_for_url(timeout=20.0)  # 20-second grace period
        if url:
            _pass(f"Public URL generated: {url}")
        else:
            # The tunnel didn't connect, but that's an infrastructure issue,
            # not a PortaLink code issue.  Flag as informational FAIL so the
            # exit code still reflects the environment reality.
            _fail(
                "Public URL not generated within 20 s "
                "(tunnel may be unavailable in this environment)."
            )

        # ------------------------------------------------------------------
        # 6. Verify filename matches the temporary file's basename
        # ------------------------------------------------------------------
        expected_filename = os.path.basename(tmp_path)
        actual_filename = share.filename
        try:
            assert actual_filename == expected_filename, (
                f"Expected {expected_filename!r}, got {actual_filename!r}"
            )
            _pass(f"Filename matches: {actual_filename!r}")
        except AssertionError as exc:
            _fail(f"Filename mismatch: {exc}")

        # ------------------------------------------------------------------
        # 7. Verify reported file_size matches the actual file on disk
        # ------------------------------------------------------------------
        expected_size = len(known_content)
        actual_size = share.file_size
        try:
            assert actual_size == expected_size, (
                f"Expected {expected_size} bytes, got {actual_size}"
            )
            _pass(f"File size matches: {actual_size} bytes.")
        except AssertionError as exc:
            _fail(f"File size mismatch: {exc}")

        # ------------------------------------------------------------------
        # 8. Verify source_path points to our temporary file
        # ------------------------------------------------------------------
        try:
            assert os.path.realpath(share.source_path) == os.path.realpath(tmp_path), (
                f"source_path {share.source_path!r} != {tmp_path!r}"
            )
            _pass("source_path correctly points to the temporary file.")
        except AssertionError as exc:
            _fail(f"source_path check failed: {exc}")

        # ------------------------------------------------------------------
        # 9. Revoke the share and confirm it is no longer ACTIVE
        # ------------------------------------------------------------------
        revoked = manager.delete_share(share.share_id)
        try:
            assert revoked is True, "delete_share() returned False"
            _pass("Share revoked successfully.")
        except AssertionError as exc:
            _fail(f"Share revocation failed: {exc}")

    except Exception as exc:
        print(exc)
        _fail(f"Unexpected exception during share lifecycle: {exc}")

    finally:
        # ------------------------------------------------------------------
        # Cleanup — always runs regardless of test outcome
        # ------------------------------------------------------------------
        if manager is not None:
            try:
                manager.shutdown()
                _pass("ShareManager shut down cleanly.")
            except Exception as exc:
                print(exc)
                _fail(f"ShareManager shutdown raised an exception: {exc}")

        if tmp_file is not None and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
                _pass("Temporary file removed.")
            except Exception as exc:
                print(exc)
                _fail(f"Could not remove temporary file: {exc}")


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

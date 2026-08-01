"""
test_download_limit.py
======================
Standalone test for PortaLink's per-share download-limit enforcement.

Test flow:
  1. Create a temporary file on disk.
  2. Create a share with max_downloads=1 and wait for a public URL.
  3. Download the file once via the public URL.
  4. Verify that the download counter incremented to 1.
  5. Verify that the share is now exhausted (or no longer active).
  6. Attempt a second download and confirm it is rejected (HTTP 410 Gone).
  7. Clean up the temporary file and shut down the manager.

Exit codes:
  0 – all checks passed
  1 – at least one check failed or an unexpected exception occurred
"""

import sys
import os
import tempfile
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_failures = 0


def passed(msg: str) -> None:
    print(f"[PASS] {msg}")


def failed(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")


def summary() -> None:
    bar = "================================="
    if _failures == 0:
        print(f"\n{bar}\nTEST RESULT : PASS\n{bar}")
    else:
        print(f"\n{bar}\nTEST RESULT : FAIL\n{bar}")


# ---------------------------------------------------------------------------
# Helper: perform a simple HTTP GET and return (status_code, body_bytes)
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: float = 30.0):
    """Return (status, body) for a GET request; handles urllib HTTP errors."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_tests() -> None:
    tmp_path = None
    manager = None

    try:
        # ------------------------------------------------------------------
        # Step 0: Import PortaLink
        # ------------------------------------------------------------------
        try:
            # The project root must be on sys.path so we can import sharelink.
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from sharelink import ShareManager, ShareConfig, ShareState
            passed("PortaLink imported successfully.")
        except Exception as exc:
            print(exc)
            failed("Could not import PortaLink.")
            return

        # ------------------------------------------------------------------
        # Step 1: Create a temporary file
        # ------------------------------------------------------------------
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="portaLink_test_")
            with os.fdopen(fd, "w") as fh:
                fh.write("PortaLink download-limit test payload.\n" * 10)
            passed(f"Temporary file created: {tmp_path}")
        except Exception as exc:
            print(exc)
            failed("Could not create temporary file.")
            return

        # ------------------------------------------------------------------
        # Step 2: Start a ShareManager and create a share (max_downloads=1)
        # ------------------------------------------------------------------
        try:
            cfg = ShareConfig(expire_seconds=300)  # short-lived, 5 min
            manager = ShareManager(config=cfg)
            share = manager.create_share(
                tmp_path,
                max_downloads=1,
                wait_for_url=True,
                url_timeout=60.0,
            )
            passed("ShareManager started and share created.")
        except Exception as exc:
            print(exc)
            failed("Could not create share.")
            return

        # ------------------------------------------------------------------
        # Step 3: Verify a public URL was produced
        # ------------------------------------------------------------------
        try:
            public_url = share.public_url
            assert public_url.startswith("https://"), (
                f"Expected https:// URL, got: {public_url!r}"
            )
            passed(f"Public URL obtained: {public_url}")
        except AssertionError as exc:
            print(exc)
            failed("Public URL is missing or malformed.")
            return

        # Build the direct download URL (the share URL itself is the download URL)
        download_url = public_url

        # ------------------------------------------------------------------
        # Step 4: Perform the first (and only permitted) download
        # ------------------------------------------------------------------
        try:
            status, body = http_get(download_url)
            assert status == 200, f"Expected HTTP 200, got {status}"
            assert len(body) > 0, "Response body was empty."
            passed(f"First download succeeded (HTTP {status}, {len(body)} bytes).")
        except AssertionError as exc:
            print(exc)
            failed("First download did not return expected HTTP 200 response.")
            return

        # Give the session layer a moment to record the completed download.
        time.sleep(2)

        # ------------------------------------------------------------------
        # Step 5: Verify the download counter incremented
        # ------------------------------------------------------------------
        try:
            stats = share.statistics
            assert stats.total_downloads >= 1, (
                f"Expected total_downloads >= 1, got {stats.total_downloads}"
            )
            passed(
                f"Download counter incremented correctly "
                f"(total_downloads={stats.total_downloads})."
            )
        except AssertionError as exc:
            print(exc)
            failed("Download counter did not increment after the first download.")

        # ------------------------------------------------------------------
        # Step 6: Verify the share is exhausted / no longer active
        # ------------------------------------------------------------------
        try:
            # is_exhausted or state == EXHAUSTED both signal the same condition.
            exhausted = share.is_exhausted
            still_active = share.is_active
            assert exhausted or not still_active, (
                f"Share should be exhausted after 1 download, but "
                f"is_exhausted={exhausted}, is_active={still_active}, "
                f"state={share.state}"
            )
            passed(
                f"Share is correctly marked as exhausted/inactive "
                f"(is_exhausted={exhausted}, is_active={still_active})."
            )
        except AssertionError as exc:
            print(exc)
            failed("Share is still active after reaching max_downloads=1.")

        # ------------------------------------------------------------------
        # Step 7: Second download attempt must be rejected (HTTP 410 Gone)
        # ------------------------------------------------------------------
        try:
            status2, _ = http_get(download_url)
            # 410 Gone is the expected response for exhausted/expired shares.
            # Some implementations may return 404 if the share is removed from
            # the registry before the second request arrives; we accept both.
            assert status2 in (410, 404), (
                f"Expected HTTP 410 or 404 for exhausted share, got {status2}"
            )
            passed(
                f"Second download correctly rejected with HTTP {status2}."
            )
        except AssertionError as exc:
            print(exc)
            failed("Second download was not rejected after share exhaustion.")

    except Exception as exc:
        # Catch-all for any unexpected error not handled above.
        print(exc)
        failed("Unexpected exception during test execution.")

    finally:
        # ------------------------------------------------------------------
        # Cleanup: shut down manager and remove temporary file
        # ------------------------------------------------------------------
        if manager is not None:
            try:
                manager.shutdown()
                passed("ShareManager shut down cleanly.")
            except Exception as exc:
                print(exc)
                failed("Error during ShareManager shutdown.")

        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                passed("Temporary file removed.")
            except Exception as exc:
                print(exc)
                failed("Could not remove temporary file.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_tests()
    summary()
    sys.exit(0 if _failures == 0 else 1)

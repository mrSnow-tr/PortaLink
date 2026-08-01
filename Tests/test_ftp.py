#!/usr/bin/env python3
"""
test_ftp.py
-----------
Verify FTP sharing functionality in PortaLink (sharelink package).

If no FTP server is reachable, the test prints a SKIPPED message and
exits 0 (success).  If an FTP server is available, it verifies that
PortaLink can create a share and produce a non-empty public URL.

Usage:
    python test_ftp.py                        # auto-detect local FTP
    FTP_HOST=ftp.example.com python test_ftp.py
    FTP_HOST=ftp.example.com FTP_PORT=2121 \
        FTP_USER=alice FTP_PASS=secret FTP_PATH=/pub/data.txt \
        python test_ftp.py

Environment variables (all optional):
    FTP_HOST   – FTP server hostname  (default: 127.0.0.1)
    FTP_PORT   – FTP server port      (default: 21)
    FTP_USER   – FTP username         (default: anonymous)
    FTP_PASS   – FTP password         (default: anonymous@)
    FTP_PATH   – Remote file path     (default: /)
    URL_TIMEOUT – Seconds to wait for the tunnel URL (default: 30)
"""

import ftplib
import os
import sys

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def passed(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"[PASS] {msg}")


def failed(msg: str) -> None:
    global _failed
    _failed += 1
    print(f"[FAIL] {msg}")


def summary_exit() -> None:
    """Print final summary and exit with the appropriate code."""
    print()
    print("=================================")
    if _failed == 0:
        print("TEST RESULT : PASS")
        print("=================================")
        sys.exit(0)
    else:
        print("TEST RESULT : FAIL")
        print("=================================")
        sys.exit(1)


# ---------------------------------------------------------------------------
# FTP connectivity probe
# ---------------------------------------------------------------------------

def probe_ftp(host: str, port: int, user: str, password: str,
              remote_path: str, timeout: float = 10.0) -> bool:
    """
    Return True when we can log in to the FTP server and the remote path
    exists (either SIZE or NLST succeeds).  Never raises.
    """
    try:
        ftp = ftplib.FTP()
        ftp.connect(host=host, port=port, timeout=timeout)
        ftp.login(user=user, passwd=password)
        ftp.set_pasv(True)

        # Try to confirm the path exists
        try:
            ftp.size(remote_path)        # works for files
        except ftplib.error_perm:
            ftp.nlst(remote_path)        # works for directories

        ftp.quit()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main test logic
# ---------------------------------------------------------------------------

def main() -> None:
    # --- Read configuration from environment --------------------------------
    ftp_host    = os.environ.get("FTP_HOST", "127.0.0.1")
    ftp_port    = int(os.environ.get("FTP_PORT", "21"))
    ftp_user    = os.environ.get("FTP_USER", "anonymous")
    ftp_pass    = os.environ.get("FTP_PASS", "anonymous@")
    ftp_path    = os.environ.get("FTP_PATH", "/")
    url_timeout = float(os.environ.get("URL_TIMEOUT", "30"))

    print(f"FTP target: {ftp_user}@{ftp_host}:{ftp_port}{ftp_path}")
    print()

    # --- Step 1: import PortaLink -------------------------------------------
    try:
        import sharelink                        # noqa: F401 (verify importable)
        from sharelink import ShareManager, ShareConfig
        passed("PortaLink imported successfully.")
    except Exception as exc:
        print(exc)
        failed("Could not import PortaLink (sharelink package).")
        summary_exit()
        return                                  # unreachable; satisfies linter

    # --- Step 2: probe FTP connectivity ------------------------------------
    reachable = probe_ftp(ftp_host, ftp_port, ftp_user, ftp_pass, ftp_path)
    if not reachable:
        # Graceful skip: no FTP server available
        print(
            f"[SKIPPED] FTP server at {ftp_host}:{ftp_port} is not reachable "
            "or the remote path does not exist.  FTP tests are skipped."
        )
        print()
        # Exit 0 – skipping is not a failure
        print("=================================")
        print("TEST RESULT : PASS")
        print("=================================")
        sys.exit(0)

    passed(f"FTP server reachable at {ftp_host}:{ftp_port}.")

    # --- Step 3: create a PortaLink FTP share ------------------------------
    manager = None
    share   = None
    try:
        # Use a short expiry and low max_downloads to keep tests clean.
        cfg = ShareConfig(expire_seconds=300, max_downloads=1)
        manager = ShareManager(config=cfg)
        passed("ShareManager created.")
    except Exception as exc:
        print(exc)
        failed("Could not create ShareManager.")
        summary_exit()
        return

    try:
        share = manager.create_share(
            source      = ftp_path,
            ftp_host    = ftp_host,
            ftp_port    = ftp_port,
            ftp_username = ftp_user,
            ftp_password = ftp_pass,
            ftp_passive  = True,
            wait_for_url = True,
            url_timeout  = url_timeout,
        )
        passed("FTP share created successfully.")
    except Exception as exc:
        print(exc)
        failed("Could not create FTP share.")
        # Clean up manager before exiting
        try:
            manager.shutdown()
        except Exception:
            pass
        summary_exit()
        return

    # --- Step 4: validate share metadata -----------------------------------
    try:
        assert share is not None, "create_share returned None"
        passed("Share object is not None.")
    except AssertionError as exc:
        print(exc)
        failed("Share object is None.")
        manager.shutdown()
        summary_exit()
        return

    try:
        share_id = share.share_id
        assert share_id, "share_id is empty"
        passed(f"Share ID present: {share_id}")
    except AssertionError as exc:
        print(exc)
        failed("Share has no valid share_id.")

    try:
        from sharelink import SourceType
        assert share.source_type == SourceType.FTP, (
            f"Expected FTP source type, got {share.source_type}"
        )
        passed(f"Source type is FTP.")
    except AssertionError as exc:
        print(exc)
        failed(str(exc))

    # --- Step 5: validate public URL ---------------------------------------
    try:
        url = share.public_url
        assert url, "public_url is empty (tunnel may not have connected)"
        assert url.startswith("https://"), f"URL does not start with https://: {url!r}"
        passed(f"Public URL obtained: {url}")
    except AssertionError as exc:
        print(exc)
        failed(str(exc))

    # --- Step 6: share state should be ACTIVE ------------------------------
    try:
        from sharelink import ShareState
        assert share.state == ShareState.ACTIVE, (
            f"Expected ACTIVE, got {share.state.name}"
        )
        passed("Share state is ACTIVE.")
    except AssertionError as exc:
        print(exc)
        failed(str(exc))

    # --- Cleanup -----------------------------------------------------------
    try:
        manager.delete_share(share.share_id)
        passed("Share revoked cleanly.")
    except Exception as exc:
        print(exc)
        failed("Error revoking share.")

    try:
        manager.shutdown()
        passed("ShareManager shut down cleanly.")
    except Exception as exc:
        print(exc)
        failed("Error shutting down ShareManager.")

    # --- Final summary -----------------------------------------------------
    summary_exit()


if __name__ == "__main__":
    main()

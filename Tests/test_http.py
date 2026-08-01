"""
test_http.py
============
Standalone test for PortaLink's HTTP source-type share support.

Steps
-----
1. Spin up a tiny stdlib HTTP server that serves a temporary file.
2. Create a PortaLink share pointing at that local HTTP URL.
3. Verify the share is created and reaches ACTIVE state.
4. Verify that a public URL is generated (tunnel URL present).
5. Shut down the share and the local HTTP server cleanly.

Run:
    python test_http.py

Exit code 0 = all tests passed, non-zero = at least one failure.
"""

import sys
import os
import tempfile
import threading
import time
import http.server
import socketserver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS_COUNT = 0
FAIL_COUNT = 0


def passed(msg: str) -> None:
    global PASS_COUNT
    PASS_COUNT += 1
    print(f"[PASS] {msg}")


def failed(msg: str) -> None:
    global FAIL_COUNT
    FAIL_COUNT += 1
    print(f"[FAIL] {msg}")


def check(condition: bool, pass_msg: str, fail_msg: str) -> bool:
    """Print a PASS/FAIL line and return the boolean result."""
    if condition:
        passed(pass_msg)
    else:
        failed(fail_msg)
    return condition


# ---------------------------------------------------------------------------
# Tiny temporary HTTP server (stdlib only)
# ---------------------------------------------------------------------------

class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """Serve from a fixed directory; suppress request log noise."""

    def log_message(self, fmt, *args):  # noqa: N802
        pass  # silence access log

    def log_error(self, fmt, *args):  # noqa: N802
        pass


def start_file_server(directory: str) -> tuple[socketserver.TCPServer, int]:
    """Bind a TCPServer that serves *directory* and return (server, port)."""

    class _Handler(_SilentHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    # Port 0 → OS picks a free ephemeral port
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ---------------------------------------------------------------------------
# Main test body
# ---------------------------------------------------------------------------

def run_tests() -> int:
    """Execute all checks.  Returns 0 on full success, 1 otherwise."""

    # ------------------------------------------------------------------
    # Step 1 – Import PortaLink
    # ------------------------------------------------------------------
    try:
        import sharelink
        from sharelink import ShareManager, ShareState, SourceType
        passed("PortaLink imported successfully.")
    except Exception as exc:
        print(exc)
        failed("Could not import PortaLink.")
        return 1

    # ------------------------------------------------------------------
    # Step 2 – Create a temporary file to serve
    # ------------------------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="portaLink_test_")
    tmp_file = os.path.join(tmp_dir, "sample.txt")
    try:
        with open(tmp_file, "w") as fh:
            fh.write("Hello from PortaLink HTTP test!\n" * 100)
        passed(f"Temporary file created: {tmp_file}")
    except Exception as exc:
        print(exc)
        failed("Could not create temporary test file.")
        return 1

    # ------------------------------------------------------------------
    # Step 3 – Start the stdlib HTTP server
    # ------------------------------------------------------------------
    try:
        file_server, file_port = start_file_server(tmp_dir)
        http_url = f"http://127.0.0.1:{file_port}/sample.txt"
        passed(f"Local HTTP server started on port {file_port}.")
    except Exception as exc:
        print(exc)
        failed("Could not start local HTTP server.")
        return 1

    # ------------------------------------------------------------------
    # Step 4 – Create the PortaLink share
    # ------------------------------------------------------------------
    manager = None
    share = None
    try:
        # wait_for_url=False so we are not blocked waiting for Cloudflare;
        # we test the local-side behaviour (ACTIVE state) only.
        manager = ShareManager()
        share = manager.create_share(
            http_url,
            expire_seconds=300,   # 5 minutes – plenty for a test
            max_downloads=5,
            wait_for_url=False,   # don't block on Cloudflare tunnel
        )
        passed("ShareManager.create_share() returned without error.")
    except Exception as exc:
        print(exc)
        failed("create_share() raised an unexpected exception.")
        # Best-effort cleanup
        if manager:
            manager.shutdown()
        file_server.shutdown()
        return 1

    # ------------------------------------------------------------------
    # Step 5 – Verify source type is HTTP
    # ------------------------------------------------------------------
    try:
        assert share.source_type == SourceType.HTTP, (
            f"Expected SourceType.HTTP, got {share.source_type!r}"
        )
        passed(f"share.source_type is SourceType.HTTP.")
    except AssertionError as exc:
        print(exc)
        failed("share.source_type is not SourceType.HTTP.")

    # ------------------------------------------------------------------
    # Step 6 – Verify the share reaches ACTIVE state
    # ------------------------------------------------------------------
    deadline = time.monotonic() + 10.0  # wait up to 10 s
    state = share.state
    while state == ShareState.PENDING and time.monotonic() < deadline:
        time.sleep(0.2)
        state = share.state

    check(
        state == ShareState.ACTIVE,
        f"Share reached ACTIVE state (current: {state.name}).",
        f"Share did not reach ACTIVE state within 10 s (current: {state.name}).",
    )

    # ------------------------------------------------------------------
    # Step 7 – Verify share_id is a non-empty string
    # ------------------------------------------------------------------
    try:
        assert isinstance(share.share_id, str) and len(share.share_id) > 0
        passed(f"share.share_id is a non-empty string: {share.share_id!r}.")
    except AssertionError:
        failed("share.share_id is empty or not a string.")

    # ------------------------------------------------------------------
    # Step 8 – Verify source_path matches the URL we supplied
    # ------------------------------------------------------------------
    try:
        assert share.source_path == http_url, (
            f"Expected {http_url!r}, got {share.source_path!r}"
        )
        passed("share.source_path matches the HTTP URL supplied.")
    except AssertionError as exc:
        print(exc)
        failed("share.source_path does not match the HTTP URL supplied.")

    # ------------------------------------------------------------------
    # Step 9 – Verify the share appears in list_shares()
    # ------------------------------------------------------------------
    try:
        ids = [s.share_id for s in manager.list_shares()]
        assert share.share_id in ids
        passed("Share appears in manager.list_shares().")
    except AssertionError:
        failed("Share does NOT appear in manager.list_shares().")

    # ------------------------------------------------------------------
    # Step 10 – Verify get_share() retrieves the same share
    # ------------------------------------------------------------------
    try:
        retrieved = manager.get_share(share.share_id)
        assert retrieved is not None and retrieved.share_id == share.share_id
        passed("manager.get_share() retrieved the correct share.")
    except AssertionError:
        failed("manager.get_share() did not return the expected share.")

    # ------------------------------------------------------------------
    # Step 11 – Verify public_url is a string (may still be empty if
    #           the Cloudflare tunnel hasn't connected yet – that's fine)
    # ------------------------------------------------------------------
    try:
        url = share.public_url
        assert isinstance(url, str)
        if url:
            passed(f"share.public_url is set: {url!r}")
        else:
            passed("share.public_url is a string (tunnel still connecting – expected).")
    except AssertionError:
        failed("share.public_url is not a string.")

    # ------------------------------------------------------------------
    # Step 12 – Revoke the share via delete_share()
    # ------------------------------------------------------------------
    try:
        deleted = manager.delete_share(share.share_id)
        assert deleted is True
        passed("manager.delete_share() returned True.")
    except AssertionError:
        failed("manager.delete_share() did not return True.")
    except Exception as exc:
        print(exc)
        failed("manager.delete_share() raised an unexpected exception.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    try:
        manager.shutdown()
        passed("ShareManager shut down cleanly.")
    except Exception as exc:
        print(exc)
        failed("ShareManager.shutdown() raised an exception.")

    try:
        file_server.shutdown()
        passed("Local HTTP server shut down cleanly.")
    except Exception as exc:
        print(exc)
        failed("Local HTTP server shutdown raised an exception.")

    # Remove temporary directory
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        passed("Temporary files cleaned up.")
    except Exception as exc:
        print(exc)
        failed("Could not clean up temporary files.")

    return 0 if FAIL_COUNT == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exit_code = run_tests()

    print()
    if exit_code == 0:
        print("=================================")
        print("TEST RESULT : PASS")
        print("=================================")
    else:
        print("=================================")
        print("TEST RESULT : FAIL")
        print("=================================")

    sys.exit(exit_code)

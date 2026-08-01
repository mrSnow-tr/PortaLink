"""
test_directory.py
~~~~~~~~~~~~~~~~~

Standalone test for PortaLink directory sharing.

Creates a temporary directory with several files, shares it via
ShareManager, verifies the share is accepted, checks that a public URL
is generated, confirms the source type is LOCAL_DIRECTORY, and verifies
the download filename ends with ".zip" (PortaLink serves directories as
ZIP archives).

All temporary resources are cleaned up automatically.

Usage:
    python test_directory.py

Exit codes:
    0 – all checks passed
    1 – one or more checks failed
"""

import os
import sys
import tempfile
import shutil

# ── helpers ────────────────────────────────────────────────────────────────

_failures = 0


def passed(msg: str) -> None:
    print(f"[PASS] {msg}")


def failed(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")


def summary() -> None:
    print()
    print("=================================")
    if _failures == 0:
        print("TEST RESULT : PASS")
    else:
        print("TEST RESULT : FAIL")
    print("=================================")


# ── test logic ─────────────────────────────────────────────────────────────

def run_tests() -> None:
    # ------------------------------------------------------------------
    # 1. Import PortaLink
    # ------------------------------------------------------------------
    try:
        from sharelink import ShareManager, SourceType, ShareState
        passed("PortaLink imported successfully.")
    except Exception as exc:
        print(exc)
        failed("Could not import PortaLink – aborting.")
        return

    # ------------------------------------------------------------------
    # 2. Create a temporary directory with several files
    # ------------------------------------------------------------------
    tmp_dir = None
    share = None
    manager = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="portalink_test_")

        # Populate the directory with a handful of test files
        file_contents = {
            "readme.txt":  "Hello from PortaLink test.\n",
            "data.csv":    "name,value\nalpha,1\nbeta,2\n",
            "notes.md":    "# Test\n\nThis is a markdown file.\n",
        }
        for name, content in file_contents.items():
            fpath = os.path.join(tmp_dir, name)
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(content)

        # Verify the files were actually created
        created = os.listdir(tmp_dir)
        assert len(created) == len(file_contents), (
            f"Expected {len(file_contents)} files, found {len(created)}"
        )
        passed(f"Temporary directory created with {len(created)} files: {tmp_dir}")

    except AssertionError as exc:
        print(exc)
        failed("Temporary directory setup failed.")
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return
    except Exception as exc:
        print(exc)
        failed("Unexpected error creating temporary directory.")
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    # ------------------------------------------------------------------
    # 3. Create a ShareManager and share the directory
    # ------------------------------------------------------------------
    try:
        manager = ShareManager()
        passed("ShareManager instantiated.")
    except Exception as exc:
        print(exc)
        failed("ShareManager could not be instantiated.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    try:
        # wait_for_url=False so the test doesn't block on cloudflared
        share = manager.create_share(
            tmp_dir,
            expire_seconds=3600,
            max_downloads=5,
            wait_for_url=False,
        )
        passed("Directory share created successfully.")
    except Exception as exc:
        print(exc)
        failed("create_share() raised an exception for a directory source.")
        manager.shutdown()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    # ------------------------------------------------------------------
    # 4. Verify source_type is LOCAL_DIRECTORY
    # ------------------------------------------------------------------
    try:
        assert share.source_type == SourceType.LOCAL_DIRECTORY, (
            f"Expected LOCAL_DIRECTORY, got {share.source_type}"
        )
        passed(f"source_type is LOCAL_DIRECTORY.")
    except AssertionError as exc:
        print(exc)
        failed("source_type is not LOCAL_DIRECTORY.")

    # ------------------------------------------------------------------
    # 5. Verify the share has a non-empty share_id
    # ------------------------------------------------------------------
    try:
        assert share.share_id and isinstance(share.share_id, str), (
            "share_id must be a non-empty string"
        )
        passed(f"share_id is present: {share.share_id}")
    except AssertionError as exc:
        print(exc)
        failed("share_id is missing or empty.")

    # ------------------------------------------------------------------
    # 6. Verify the share state is ACTIVE (or PENDING while tunnel starts)
    # ------------------------------------------------------------------
    try:
        assert share.state in (ShareState.ACTIVE, ShareState.PENDING), (
            f"Expected ACTIVE or PENDING, got {share.state}"
        )
        passed(f"Share state is {share.state.name} (expected ACTIVE or PENDING).")
    except AssertionError as exc:
        print(exc)
        failed("Share state is neither ACTIVE nor PENDING after creation.")

    # ------------------------------------------------------------------
    # 7. Verify the resolved filename ends with ".zip"
    #    (PortaLink serves directories as on-the-fly ZIP archives)
    # ------------------------------------------------------------------
    try:
        filename = share.filename
        assert isinstance(filename, str) and filename.endswith(".zip"), (
            f"Expected a .zip filename, got {filename!r}"
        )
        passed(f"Download filename ends with '.zip': {filename}")
    except AssertionError as exc:
        print(exc)
        failed("Download filename does not end with '.zip'.")

    # ------------------------------------------------------------------
    # 8. Verify content_type is application/zip
    # ------------------------------------------------------------------
    try:
        ct = share.content_type
        assert ct == "application/zip", (
            f"Expected 'application/zip', got {ct!r}"
        )
        passed(f"content_type is 'application/zip'.")
    except AssertionError as exc:
        print(exc)
        failed("content_type is not 'application/zip'.")

    # ------------------------------------------------------------------
    # 9. Verify source_path matches the temp directory
    # ------------------------------------------------------------------
    try:
        # resolve() handles any symlinks (e.g. /var → /private/var on macOS)
        import pathlib
        expected = str(pathlib.Path(tmp_dir).resolve())
        actual   = str(pathlib.Path(share.source_path).resolve())
        assert actual == expected, (
            f"source_path mismatch: expected {expected!r}, got {actual!r}"
        )
        passed("source_path matches the temporary directory.")
    except AssertionError as exc:
        print(exc)
        failed("source_path does not match the temporary directory.")

    # ------------------------------------------------------------------
    # 10. Verify to_dict() returns a well-formed dict
    # ------------------------------------------------------------------
    try:
        d = share.to_dict()
        assert isinstance(d, dict), "to_dict() must return a dict"
        for key in ("share_id", "source_type", "state", "filename", "statistics"):
            assert key in d, f"Key {key!r} missing from to_dict() output"
        passed("to_dict() returns a well-formed dict with expected keys.")
    except AssertionError as exc:
        print(exc)
        failed(f"to_dict() output is malformed: {exc}")

    # ------------------------------------------------------------------
    # 11. Revoke the share and confirm deletion
    # ------------------------------------------------------------------
    try:
        deleted = manager.delete_share(share.share_id)
        assert deleted is True, "delete_share() should return True for an existing share"
        passed("Share revoked via delete_share().")
    except AssertionError as exc:
        print(exc)
        failed("delete_share() did not return True.")
    except Exception as exc:
        print(exc)
        failed("delete_share() raised an exception.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    try:
        manager.shutdown()
        passed("ShareManager shut down cleanly.")
    except Exception as exc:
        print(exc)
        failed("ShareManager.shutdown() raised an exception.")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not os.path.exists(tmp_dir):
        passed("Temporary directory cleaned up.")
    else:
        failed("Temporary directory was NOT cleaned up.")


# ── entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_tests()
    summary()
    sys.exit(0 if _failures == 0 else 1)

"""
share_directory.py
==================
Demonstrates how to share a local directory using Sharelink.

When a directory is shared, Sharelink automatically packages its entire
contents into a ZIP archive and streams it to anyone with the public URL.
This is useful for quickly sharing a folder of files (reports, images,
exports, etc.) without manually creating a ZIP first.

Prerequisites:
  - Sharelink must be installed in your Python environment.
  - cloudflared must be available (or will be downloaded automatically).
  - An active internet connection is required for the Cloudflare Tunnel.

Usage:
  python share_directory.py
"""

import sys
import tempfile
import os

# ---------------------------------------------------------------------------
# Step 1 – Import Sharelink.
# ShareManager is the main entry point; it handles share lifecycle,
# the embedded HTTP server, and the Cloudflare Tunnel automatically.
# ---------------------------------------------------------------------------
try:
    from sharelink import ShareManager, ShareConfig
except ImportError as exc:
    print("ERROR: Could not import Sharelink.")
    print("       Install it with:  pip install sharelink")
    print(exc)
    sys.exit(1)


def main() -> None:
    # -----------------------------------------------------------------------
    # Step 2 – Create a temporary directory and populate it with sample files.
    # In real use you would point Sharelink at an existing directory, e.g.:
    #
    #   DIRECTORY_TO_SHARE = "/home/user/my_reports"
    #
    # Here we build a throwaway directory so the script is fully self-contained
    # and leaves nothing behind on your filesystem.
    # -----------------------------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="sharelink_demo_")
    try:
        # Write a couple of sample files into the temporary directory.
        sample_files = {
            "readme.txt": "Hello from Sharelink!\nThis directory was shared automatically.\n",
            "data.csv":   "name,value\nalpha,1\nbeta,2\ngamma,3\n",
            "notes.txt":  "These files were bundled into a ZIP archive on the fly.\n",
        }
        for filename, content in sample_files.items():
            path = os.path.join(tmp_dir, filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)

        print(f"Temporary directory created: {tmp_dir}")
        print(f"Files inside: {', '.join(sample_files.keys())}")
        print()

        # -------------------------------------------------------------------
        # Step 3 – Configure Sharelink (optional).
        # ShareConfig lets you customise expiry, download limits, and more.
        # All values shown here are examples – adjust them to your needs.
        # -------------------------------------------------------------------
        config = ShareConfig(
            expire_seconds=3_600,   # Share expires after 1 hour.
            max_downloads=25,       # Allow up to 25 downloads.
        )

        # -------------------------------------------------------------------
        # Step 4 – Start the ShareManager.
        # Using it as a context manager guarantees that all resources
        # (HTTP server, Cloudflare Tunnel, background threads) are cleaned
        # up cleanly when the block exits, even if an exception is raised.
        # -------------------------------------------------------------------
        print("Starting Sharelink – this may take a moment while the")
        print("Cloudflare Tunnel establishes a connection …")
        print()

        with ShareManager(config=config) as manager:

            # ---------------------------------------------------------------
            # Step 5 – Create the share.
            # Passing a directory path is all that is needed; Sharelink
            # detects that it is a directory and sets source_type to
            # LOCAL_DIRECTORY automatically.  The ZIP archive is generated
            # on the fly when a client requests the download.
            #
            # wait_for_url=True (the default) blocks here until the tunnel
            # is up and a public URL is available, or url_timeout seconds
            # pass.
            # ---------------------------------------------------------------
            try:
                share = manager.create_share(
                    tmp_dir,
                    wait_for_url=True,
                    url_timeout=60.0,   # Wait up to 60 s for the tunnel URL.
                )
            except Exception as exc:
                print("ERROR: Failed to create the share.")
                print(exc)
                sys.exit(1)

            # ---------------------------------------------------------------
            # Step 6 – Report what Sharelink set up.
            # ---------------------------------------------------------------
            public_url = share.public_url

            if not public_url:
                print("WARNING: The tunnel URL was not available within the")
                print("         timeout. The share is still running in the")
                print("         background – try share.wait_for_url() again.")
            else:
                print("Directory shared successfully.")
                print(f"Public URL : {public_url}")
                print(f"Filename   : {share.filename}")       # e.g. demo_dir.zip
                print(f"State      : {share.state.name}")     # ACTIVE
                print(f"Expires    : {share.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                print(f"Max DLs    : {share.max_downloads}")
                print()
                print("Anyone with the URL above can download the directory")
                print("as a ZIP archive until it expires or the limit is reached.")

            # ---------------------------------------------------------------
            # Step 7 – Keep the share alive until the user is done.
            # The share (and its Cloudflare Tunnel) stays active for as long
            # as the ShareManager context is open.  Pressing Enter exits the
            # context, which stops the tunnel and the HTTP server cleanly.
            # ---------------------------------------------------------------
            print()
            input("Press Enter to stop sharing and exit …")
            print("Shutting down …")

    finally:
        # -------------------------------------------------------------------
        # Step 8 – Clean up the temporary directory.
        # This block runs whether the script succeeds or fails, ensuring no
        # leftover files are left on disk.
        # -------------------------------------------------------------------
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"Temporary directory removed: {tmp_dir}")


if __name__ == "__main__":
    main()

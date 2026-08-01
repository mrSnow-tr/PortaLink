"""
one_time_share.py — Demonstrate creating a one-time, time-limited download link.

What it shows:
    How to use ShareManager.create_share() with expire_seconds and max_downloads
    to produce a secure, single-use download URL that expires automatically.

Why it's useful:
    One-time links are ideal for sharing sensitive files (contracts, credentials,
    reports) where you want to ensure the file can only be downloaded once and
    becomes inaccessible after a short window — even if the URL leaks.

Prerequisites:
    - The sharelink package must be installed and importable.
    - cloudflared must be available (sharelink downloads it automatically
      on first run if it is not found on your PATH).
    - An active internet connection is required for the Cloudflare Tunnel.
"""

import sys
import tempfile
import os

# ---------------------------------------------------------------------------
# Import Sharelink
# ---------------------------------------------------------------------------

try:
    from sharelink import ShareManager, ShareConfig
except ImportError as exc:
    print("ERROR: Could not import the sharelink package.")
    print("       Make sure it is installed before running this script.")
    print(exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration — adjust these values to suit your use case
# ---------------------------------------------------------------------------

# How many seconds until the link stops working (900 s = 15 minutes).
EXPIRE_SECONDS = 900

# Maximum number of times the file may be downloaded before the link dies.
# Setting this to 1 creates a true "one-time" link.
MAX_DOWNLOADS = 1

# ---------------------------------------------------------------------------
# Create a temporary file to share
# ---------------------------------------------------------------------------
# In a real script you would replace this section with the path to the
# actual file you want to share, e.g.:
#
#   FILE_TO_SHARE = "/home/alice/confidential_report.pdf"
#
# Here we create a small throwaway file so the example is self-contained.

tmp_file = None  # Declared here so the finally block can always reference it.

try:
    # Create a temporary file and write some demo content into it.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="sharelink_demo_")
    tmp_file = tmp_path  # Remember the path so we can delete it later.

    with os.fdopen(tmp_fd, "w") as fh:
        fh.write("This file was shared via a one-time Sharelink URL.\n")
        fh.write("After one download (or 15 minutes), this link will stop working.\n")

    print(f"Temporary demo file created: {tmp_path}")

    # ---------------------------------------------------------------------------
    # Build a ShareConfig with the one-time link constraints
    # ---------------------------------------------------------------------------

    config = ShareConfig(
        expire_seconds=EXPIRE_SECONDS,  # Link becomes invalid after 15 minutes.
        max_downloads=MAX_DOWNLOADS,    # Link becomes invalid after 1 download.
    )

    # ---------------------------------------------------------------------------
    # Create the share and wait for the public URL
    # ---------------------------------------------------------------------------

    print("\nStarting Sharelink server and Cloudflare Tunnel …")
    print("(This may take up to 30 seconds on the first run while cloudflared")
    print(" is downloaded and the tunnel negotiates a hostname.)\n")

    # The context manager ensures the server and tunnel are shut down cleanly
    # when we leave the block, even if an exception occurs.
    with ShareManager(config=config) as manager:

        # create_share blocks until the Cloudflare Tunnel has announced a
        # public URL (wait_for_url=True is the default).
        share = manager.create_share(
            source=tmp_path,           # The file we want to share.
            expire_seconds=EXPIRE_SECONDS,
            max_downloads=MAX_DOWNLOADS,
            # Optional: give the download a friendly name instead of the
            # temp-file basename.
            display_name="one_time_demo.txt",
            wait_for_url=True,         # Block until the URL is ready.
            url_timeout=60.0,          # Give the tunnel up to 60 s to connect.
        )

        # Retrieve the live public URL announced by the Cloudflare Tunnel.
        public_url = share.public_url

        if not public_url:
            print("ERROR: The Cloudflare Tunnel did not produce a public URL")
            print("       within the allowed timeout. Check your internet")
            print("       connection and try again.")
            sys.exit(1)

        # ---------------------------------------------------------------------------
        # Report results to the user
        # ---------------------------------------------------------------------------

        print("=" * 60)
        print("Share created successfully.")
        print(f"Public URL:     {public_url}")
        print(f"Expires in:     {EXPIRE_SECONDS} seconds ({EXPIRE_SECONDS // 60} minutes)")
        print(f"Max downloads:  {MAX_DOWNLOADS}  (link dies after a single download)")
        print("=" * 60)
        print()
        print("Why this is useful for secure file sharing:")
        print("  • The link works for only ONE download. Once the recipient")
        print("    downloads the file the URL returns 410 Gone to anyone else.")
        print("  • Even if no one downloads it, the link expires automatically")
        print(f"    in {EXPIRE_SECONDS // 60} minutes — no manual cleanup needed.")
        print("  • The file never leaves your machine; Cloudflare Tunnel")
        print("    proxies the request directly to this script.")
        print()
        print("Press Ctrl+C to stop the server early, or wait for the")
        print("script to exit on its own after the link expires.")

        # Keep the server alive long enough for a real download.
        # In a production script you might wait on user input instead.
        try:
            import time
            time.sleep(EXPIRE_SECONDS)
        except KeyboardInterrupt:
            print("\nServer stopped by user.")

    # The 'with' block has exited — the server and tunnel are now shut down.

except Exception as exc:
    print("\nERROR: An unexpected error occurred.")
    print(exc)
    sys.exit(1)

finally:
    # ---------------------------------------------------------------------------
    # Clean up the temporary demo file regardless of how we exit
    # ---------------------------------------------------------------------------
    if tmp_file and os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
            print(f"\nTemporary file cleaned up: {tmp_file}")
        except OSError as cleanup_exc:
            print(f"\nWarning: could not remove temporary file {tmp_file}: {cleanup_exc}")

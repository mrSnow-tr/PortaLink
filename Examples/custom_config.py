"""
custom_config.py – Sharelink Custom Configuration Example

What this demonstrates:
    How to create a ShareConfig object with several custom settings and use it
    when creating a ShareManager.  Every tunable option is explained with an
    inline comment so you can copy, adjust, and understand each knob.

When it is useful:
    - You need a shorter or longer share expiry than the 24-hour default.
    - You want to limit how many times a file can be downloaded.
    - You are behind a slow or unreliable network and need to tweak the
      Cloudflare Tunnel reconnect behaviour.
    - You want finer control over the streaming chunk size for large files.

Prerequisites:
    - Sharelink installed in the active Python environment.
    - The cloudflared binary reachable on PATH, or Sharelink will download it
      automatically on first run (requires internet access).
    - A local file to share.  A temporary file is created and cleaned up
      automatically by this script so you can run it without any setup.

Usage:
    python custom_config.py
"""

import sys
import tempfile
import os

# ---------------------------------------------------------------------------
# Import the two public symbols we need from Sharelink.
# ---------------------------------------------------------------------------
try:
    from sharelink import ShareConfig, ShareManager
except ImportError as exc:
    print("ERROR: Could not import Sharelink.")
    print("Make sure it is installed:  pip install sharelink")
    print(exc)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1.  Create a temporary file to share.
#
#     In a real script you would replace this section with the path to an
#     actual file on your system, e.g.:
#
#         FILE_TO_SHARE = "/home/alice/report.pdf"   # <-- replace with your path
# ---------------------------------------------------------------------------
tmp_file = None  # Keep a reference so we can delete it in the finally block.
try:
    # Create a small temporary file with some sample content.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="sharelink_demo_")
    with os.fdopen(tmp_fd, "w") as fh:
        fh.write("Hello from the Sharelink custom-config example!\n")
        fh.write("This file was created automatically and will be deleted when\n")
        fh.write("the script finishes.\n")
    tmp_file = tmp_path  # Store path so the finally block can remove it.

    # ---------------------------------------------------------------------------
    # 2.  Build a ShareConfig with custom settings.
    #
    #     Every parameter shown below has a sensible default inside Sharelink;
    #     you only need to pass the ones you actually want to change.
    # ---------------------------------------------------------------------------
    config = ShareConfig(
        # --- Share lifetime ---------------------------------------------------
        # How long (in seconds) before the share link automatically expires.
        # Default: 86400 (24 hours).  Here we use 1 hour.
        expire_seconds=3_600,

        # Maximum number of times the file may be downloaded before the link
        # is automatically disabled.  Default: 10.
        max_downloads=3,

        # --- Networking -------------------------------------------------------
        # Size of each streaming chunk sent to the client, in bytes.
        # Larger values reduce system-call overhead but use more memory per
        # concurrent connection.  Default: 65536 (64 KiB).
        chunk_size=32_768,  # 32 KiB – smaller, better for slow connections

        # --- Cloudflare Tunnel reconnect behaviour ----------------------------
        # Seconds to wait between consecutive reconnect attempts after the
        # tunnel drops.  Default: 5.0.
        reconnect_delay=10.0,  # Be gentler with the Cloudflare edge network.

        # Maximum number of consecutive reconnect attempts before giving up.
        # Pass -1 for unlimited retries.  Default: 10.
        reconnect_retries=5,
    )

    # ---------------------------------------------------------------------------
    # 3.  Create the ShareManager using our custom config.
    #
    #     The context manager guarantees that the HTTP server and Cloudflare
    #     Tunnel subprocess are shut down cleanly even if an exception occurs.
    # ---------------------------------------------------------------------------
    print("Starting Sharelink with custom configuration …")
    print(f"  expire_seconds  : {config.expire_seconds}  (1 hour)")
    print(f"  max_downloads   : {config.max_downloads}")
    print(f"  chunk_size      : {config.chunk_size} bytes")
    print(f"  reconnect_delay : {config.reconnect_delay}s")
    print(f"  reconnect_retries: {config.reconnect_retries}")
    print()

    with ShareManager(config=config) as manager:
        # ---------------------------------------------------------------------
        # 4.  Create a share for the temporary file.
        #
        #     create_share() blocks until the Cloudflare Tunnel announces a
        #     public URL (up to url_timeout seconds).  Raise the timeout if you
        #     are on a slow connection.
        # ---------------------------------------------------------------------
        try:
            share = manager.create_share(
                tmp_file,           # Path of the file to share.
                wait_for_url=True,  # Block until the public URL is ready.
                url_timeout=60.0,   # Wait up to 60 seconds for the tunnel.
            )
        except FileNotFoundError as exc:
            print("ERROR: The file to share was not found.")
            print(exc)
            sys.exit(1)
        except Exception as exc:
            print("ERROR: Failed to create the share.")
            print(exc)
            sys.exit(1)

        # ---------------------------------------------------------------------
        # 5.  Report the result.
        # ---------------------------------------------------------------------
        public_url = share.public_url or "(tunnel still connecting – see warning above)"

        print("Share created successfully.")
        print(f"Public URL      : {public_url}")
        print(f"Share ID        : {share.share_id}")
        print(f"Expires in      : {config.expire_seconds // 60} minutes")
        print(f"Downloads left  : {share.downloads_remaining} of {share.max_downloads}")
        print()
        print("Press Ctrl+C to stop the server and revoke the share early,")
        print("or wait for the share to expire on its own.")

        # Keep the server alive until the user interrupts.
        try:
            import time
            while share.is_active:
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nInterrupted – shutting down …")

    # ShareManager.__exit__ has already stopped the server and tunnel here.
    print("Server stopped.")

except Exception as exc:
    print("ERROR: An unexpected error occurred.")
    print(exc)
    sys.exit(1)

finally:
    # ---------------------------------------------------------------------------
    # 6.  Clean up the temporary file regardless of how the script ended.
    # ---------------------------------------------------------------------------
    if tmp_file and os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
        except OSError as exc:
            print(f"Warning: could not remove temporary file {tmp_file}: {exc}")

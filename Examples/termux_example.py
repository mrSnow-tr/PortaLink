"""
termux_example.py – Sharing a file from Termux on Android using Sharelink.

What this demonstrates:
    How to create a public download link for a file stored on your Android
    device's shared storage, using Sharelink from inside a Termux session.

When it is useful:
    Any time you want to quickly share a photo, document, or other file from
    your Android phone over the internet without installing a separate app.
    The generated Cloudflare URL works from any device on any network.

Prerequisites:
    1. Termux installed from F-Droid (https://f-droid.org/packages/com.termux/).
    2. Storage permission granted:
           termux-setup-storage
       This creates ~/storage/ symlinks into /storage/emulated/0/.
    3. Sharelink installed in your Termux Python environment:
           pip install sharelink
    4. An internet connection (Cloudflare Tunnel requires outbound HTTPS).

Usage:
    python termux_example.py

    Edit the FILE_TO_SHARE variable below to point at the file you want to
    share before running the script.
"""

import sys
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION – change this to the file you want to share
# ---------------------------------------------------------------------------

# After running `termux-setup-storage`, your Android shared storage is
# accessible at ~/storage/shared/ (a symlink to /storage/emulated/0/).
# Replace the filename below with an actual file on your device.
#
# Examples:
#   ~/storage/shared/Download/report.pdf
#   ~/storage/shared/DCIM/Camera/photo.jpg
#   ~/storage/shared/Documents/notes.txt
#
FILE_TO_SHARE = Path.home() / "storage" / "shared" / "Download" / "example.txt"

# How long the link should stay active (in seconds). Default: 1 hour.
EXPIRE_SECONDS = 3_600

# Maximum number of times the file can be downloaded before the link expires.
MAX_DOWNLOADS = 5

# ---------------------------------------------------------------------------
# Demonstration helper – create a small sample file if FILE_TO_SHARE is the
# default placeholder and does not yet exist.  This lets you run the script
# without editing anything first, so you can see it working immediately.
# Remove this block (and set _created_sample = False) once you point
# FILE_TO_SHARE at a real file.
# ---------------------------------------------------------------------------

_created_sample = False

if not FILE_TO_SHARE.exists():
    # Only auto-create the sample when the path is still the default one.
    # If the user edited FILE_TO_SHARE but the file is missing, we stop
    # with a clear message instead of silently creating something unexpected.
    default_path = Path.home() / "storage" / "shared" / "Download" / "example.txt"

    if FILE_TO_SHARE == default_path:
        # Make sure the parent directory exists.
        try:
            FILE_TO_SHARE.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # ~/storage/shared/ does not exist yet – storage permission not granted.
            print(
                "\nError: The directory ~/storage/shared/Download/ does not exist.\n"
                "This usually means storage permission has not been granted.\n"
                "Run the following command in Termux and then try again:\n\n"
                "    termux-setup-storage\n"
            )
            sys.exit(1)

        FILE_TO_SHARE.write_text(
            "Hello from Sharelink on Android!\n"
            "This file was created automatically as a demo.\n"
        )
        _created_sample = True
        print(f"[demo] Created sample file: {FILE_TO_SHARE}")
    else:
        print(f"\nError: File not found: {FILE_TO_SHARE}")
        print("Please edit the FILE_TO_SHARE variable at the top of this script.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Import Sharelink
# ---------------------------------------------------------------------------

try:
    from sharelink import ShareManager
except ImportError as exc:
    print("\nError: Sharelink is not installed.")
    print("Install it with:  pip install sharelink")
    print(exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Create the share
# ---------------------------------------------------------------------------

print(f"\nSharing file : {FILE_TO_SHARE}")
print(f"Expires in   : {EXPIRE_SECONDS // 60} minutes")
print(f"Max downloads: {MAX_DOWNLOADS}")
print("\nStarting Cloudflare Tunnel… (this may take up to 30 seconds)\n")

try:
    # ShareManager handles the embedded HTTP server and Cloudflare Tunnel
    # automatically.  Using it as a context manager ensures clean shutdown
    # when the script exits – even if an exception occurs.
    with ShareManager() as manager:

        # create_share() blocks until the tunnel is connected and a public
        # URL is available (or until url_timeout seconds elapse).
        share = manager.create_share(
            source=str(FILE_TO_SHARE),
            expire_seconds=EXPIRE_SECONDS,
            max_downloads=MAX_DOWNLOADS,
            wait_for_url=True,
            url_timeout=60.0,   # give Cloudflare up to 60 s to connect
        )

        if not share.public_url:
            print("Warning: Tunnel connected but no public URL was returned yet.")
            print("The URL may appear shortly – check share.public_url again.")
        else:
            print("Share created successfully.")
            print(f"Public URL   : {share.public_url}")
            print(f"Filename     : {share.filename}")
            print(f"File size    : {share.file_size} bytes")

        # Keep the server alive until the user presses Enter.
        # Without this, the context manager would exit immediately and
        # the tunnel would shut down before anyone could download the file.
        print("\nPress Enter to stop sharing and exit…")
        input()

        print("\nShutting down…")

except KeyboardInterrupt:
    # Ctrl-C is a normal way to exit on Android; handle it gracefully.
    print("\n\nInterrupted by user. Shutting down…")

except Exception as exc:
    print(f"\nError: {exc}")
    sys.exit(1)

finally:
    # Clean up the auto-created sample file so we leave no clutter behind.
    if _created_sample and FILE_TO_SHARE.exists():
        try:
            FILE_TO_SHARE.unlink()
            print(f"[demo] Removed sample file: {FILE_TO_SHARE}")
        except OSError:
            pass   # Non-fatal; the file is harmless if it stays.

print("Done.")

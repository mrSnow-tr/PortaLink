"""
share_local_video.py
====================
Demonstrates sharing a large local video file using Sharelink.

Sharelink streams the file in chunks (default: 64 KiB) rather than loading
it entirely into memory first. This makes it well-suited for large media files
like videos, where loading the whole file would exhaust RAM and stall the
server before the first byte reaches the client. HTTP clients (browsers, media
players, download managers) can also request specific byte ranges, so viewers
can seek within the video without re-downloading everything from the start.

When is this useful?
    - Quickly sharing a local video recording with a colleague or friend.
    - Letting someone preview a large render or export before you upload it.
    - Sending a one-time screenshare link without needing cloud storage.

Prerequisites:
    - Sharelink installed:  pip install sharelink
    - cloudflared will be downloaded automatically on first run if absent.
    - A video file on your local machine (see VIDEO_PATH below).
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration – replace these values before running the script
# ---------------------------------------------------------------------------

# Path to the video file you want to share.
# Replace this with the actual path to your video, e.g.:
#   VIDEO_PATH = Path("/home/alice/recordings/demo.mp4")
#   VIDEO_PATH = Path(r"C:\Users\Alice\Videos\demo.mp4")
VIDEO_PATH: Path | None = None  # <-- REPLACE with your video path, or leave
                                 #     None to use a small temporary file for
                                 #     demonstration purposes.

# How long (in seconds) the share link remains active before it auto-expires.
# Default: 3600 seconds = 1 hour.
EXPIRE_SECONDS: int = 3600

# Maximum number of times the file can be downloaded before the link is
# automatically revoked. Set to a high number for liberal sharing.
MAX_DOWNLOADS: int = 5

# ---------------------------------------------------------------------------
# Imports – Sharelink public API only
# ---------------------------------------------------------------------------

try:
    from sharelink import ShareManager, ShareConfig
except ImportError:
    print("Error: Sharelink is not installed.")
    print("Install it with:  pip install sharelink")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helper: create a temporary demo video if no path was provided
# ---------------------------------------------------------------------------

def create_demo_file() -> tuple[Path, bool]:
    """
    Return a path to a video file to share.

    If VIDEO_PATH is set and the file exists, use it directly.
    Otherwise, write a small temporary file that mimics a binary blob so
    the demo can run without a real video on disk. The second return value
    indicates whether the caller should delete the file afterwards.
    """
    if VIDEO_PATH is not None:
        p = Path(VIDEO_PATH)
        if not p.exists():
            print(f"Error: video file not found: {p}")
            sys.exit(1)
        print(f"Using existing video file: {p}")
        return p, False  # do not delete a file the user owns

    # Create a temporary file that is large enough to feel realistic but
    # small enough to generate quickly (1 MiB of pseudo-binary data).
    print("No VIDEO_PATH set – creating a temporary 1 MiB demo file.")
    tmp_dir = Path(tempfile.mkdtemp())
    demo_path = tmp_dir / "demo_video.mp4"

    # Write 1 MiB of repeating bytes to simulate a binary video stream.
    chunk = bytes(range(256)) * 4  # 1 KiB chunk
    with demo_path.open("wb") as fh:
        for _ in range(1024):  # 1024 × 1 KiB = 1 MiB
            fh.write(chunk)

    file_size_mb = demo_path.stat().st_size / (1024 * 1024)
    print(f"Temporary demo file created: {demo_path} ({file_size_mb:.2f} MiB)")
    return demo_path, True  # caller should clean up


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    video_path, is_temp = create_demo_file()
    cleanup_path = video_path.parent if is_temp else None

    try:
        # Build a configuration tailored for video sharing.
        # chunk_size controls how many bytes are read and sent at a time;
        # 256 KiB gives a good balance between latency and system-call overhead
        # for large sequential reads.
        config = ShareConfig(
            expire_seconds=EXPIRE_SECONDS,
            max_downloads=MAX_DOWNLOADS,
            chunk_size=256 * 1024,  # 256 KiB streaming chunks
        )

        print("\nStarting Sharelink manager…")

        # The context manager automatically stops all sessions and releases
        # the Cloudflare tunnel when the 'with' block exits.
        with ShareManager(config=config) as manager:

            print(f"Sharing file: {video_path.name}")
            print("Waiting for the Cloudflare tunnel to connect…")

            # create_share() resolves the file on disk, starts an embedded
            # HTTP server on a random port, and launches cloudflared.
            # wait_for_url=True (default) blocks until the public URL is ready
            # or url_timeout seconds elapse.
            share = manager.create_share(
                source=video_path,
                expire_seconds=EXPIRE_SECONDS,
                max_downloads=MAX_DOWNLOADS,
                # Give the link a friendly filename regardless of the local path.
                display_name=video_path.name,
                # Explicitly set the MIME type so clients treat it as a video.
                content_type="video/mp4",
                url_timeout=60.0,  # wait up to 60 s for the tunnel URL
            )

            if not share.public_url:
                print("Warning: tunnel URL not yet available.")
                print("The tunnel is still connecting – you can call")
                print("  share.wait_for_url(timeout=60)  to block further.")
            else:
                print("\nShare created successfully.")
                print(f"Public URL : {share.public_url}")
                print(f"Filename   : {share.filename}")
                print(f"MIME type  : {share.content_type}")
                print(f"File size  : {(video_path.stat().st_size / (1024*1024)):.2f} MiB")
                print(f"Expires in : {EXPIRE_SECONDS // 3600}h "
                      f"{(EXPIRE_SECONDS % 3600) // 60}m")
                print(f"Downloads  : 0 / {MAX_DOWNLOADS} used")
                print()
                print("Note: Sharelink streams the video in chunks – it never")
                print("      loads the entire file into memory. Clients can also")
                print("      seek freely using HTTP Range requests.")
                print()
                print("Press Ctrl+C to stop sharing early, or wait for expiry.")

            # Keep the process alive so the tunnel stays open.
            # In a real application you would do useful work here instead.
            try:
                while True:
                    time.sleep(5)

                    # Refresh statistics from the live session.
                    stats = share.statistics
                    remaining = share.downloads_remaining
                    print(
                        f"\r  Active downloads: {stats.active_downloads}"
                        f"  |  Completed: {stats.completed_downloads}"
                        f"  |  Remaining slots: {remaining}"
                        f"  |  Bytes sent: {stats.total_bytes_transferred:,}",
                        end="",
                        flush=True,
                    )

                    # Stop automatically once all download slots are used.
                    if share.is_exhausted:
                        print("\nDownload limit reached – stopping.")
                        break

            except KeyboardInterrupt:
                print("\nInterrupted – shutting down…")

    except Exception as exc:
        print(f"\nError: could not create share.")
        print(exc)
        sys.exit(1)

    finally:
        # Remove the temporary demo file and its parent directory if we
        # created them; never delete files that belonged to the user.
        if cleanup_path is not None and cleanup_path.exists():
            try:
                for item in cleanup_path.iterdir():
                    item.unlink(missing_ok=True)
                cleanup_path.rmdir()
                print(f"Temporary demo file cleaned up: {cleanup_path}")
            except OSError as exc:
                print(f"Warning: could not remove temporary files: {exc}")


if __name__ == "__main__":
    main()

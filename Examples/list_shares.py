"""
list_shares.py — Demonstrate creating multiple shares and listing them.

What this demonstrates:
    How to use ShareManager.create_share() to share several local files and
    how to use ShareManager.list_shares() to retrieve and inspect all active
    shares at once.  Useful fields such as share ID, filename, state,
    expiration time, and public URL are printed for each share.

When it would be useful:
    - Learning how to manage more than one share in a single session.
    - Building a script that audits or reports on what is currently shared.
    - Understanding the Share object's available properties.

Prerequisites:
    - The sharelink package must be installed and importable.
    - cloudflared must be on PATH (or will be downloaded automatically on
      first run to ~/.sharelink/bin/cloudflared).
    - An active internet connection is required for the Cloudflare tunnel.
"""

import sys
import tempfile
import os
from pathlib import Path
from datetime import timezone

# ---------------------------------------------------------------------------
# Import Sharelink.  If the package is not installed this will fail clearly.
# ---------------------------------------------------------------------------
try:
    from sharelink import ShareManager, ShareConfig
except ImportError as exc:
    print("ERROR: Could not import sharelink.")
    print(exc)
    sys.exit(1)


def main() -> None:
    # -----------------------------------------------------------------------
    # Create temporary files to share.
    # We use tempfile so nothing is left on disk after the script exits.
    # Replace these with real file paths if you want to share actual files.
    # -----------------------------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="sharelink_example_")
    tmp_files: list[str] = []

    try:
        # Create three small dummy files inside the temp directory.
        sample_names = ["report.txt", "data.csv", "notes.md"]
        for name in sample_names:
            file_path = os.path.join(tmp_dir, name)
            with open(file_path, "w") as fh:
                fh.write(f"This is a sample file: {name}\n")
            tmp_files.append(file_path)

        # -----------------------------------------------------------------------
        # Configure Sharelink.
        #
        # expire_seconds=300  → each share expires in 5 minutes (demo only).
        # max_downloads=3     → each share allows up to 3 downloads.
        #
        # Replace these values with whatever suits your use case.
        # -----------------------------------------------------------------------
        config = ShareConfig(
            expire_seconds=300,   # 5 minutes — adjust as needed
            max_downloads=3,      # maximum downloads per share — adjust as needed
        )

        # -----------------------------------------------------------------------
        # Start the ShareManager.
        # Using it as a context manager ensures clean shutdown on exit, even
        # if an exception occurs inside the block.
        # -----------------------------------------------------------------------
        print("Starting ShareManager and creating shares…")
        print("(This may take a moment while Cloudflare tunnels are established.)\n")

        with ShareManager(config=config) as manager:
            # -------------------------------------------------------------------
            # Create one share per file.
            # wait_for_url=True (default) blocks until the public URL is ready.
            # -------------------------------------------------------------------
            for file_path in tmp_files:
                try:
                    share = manager.create_share(
                        file_path,
                        wait_for_url=True,
                        url_timeout=30.0,  # wait up to 30 s for the tunnel URL
                    )
                    print(f"  ✓ Shared: {Path(file_path).name}")
                except Exception as exc:
                    print(f"ERROR: Failed to create share for {file_path}")
                    print(exc)
                    sys.exit(1)

            # -------------------------------------------------------------------
            # Retrieve all active shares via list_shares().
            # Returns a list of Share objects — one per active session.
            # -------------------------------------------------------------------
            active_shares = manager.list_shares()

            print(f"\n{'─' * 60}")
            print(f"  {len(active_shares)} active share(s) found")
            print(f"{'─' * 60}\n")

            # -------------------------------------------------------------------
            # Print useful information for each share.
            # -------------------------------------------------------------------
            for index, share in enumerate(active_shares, start=1):
                # Format the expiration time in local-friendly ISO format.
                expires_utc = share.expires_at
                # Convert to a readable string (UTC).
                expires_str = expires_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

                # downloads_remaining tells us how many downloads are left.
                remaining = share.downloads_remaining

                print(f"Share #{index}")
                print(f"  Share ID  : {share.share_id}")
                print(f"  Filename  : {share.filename}")
                print(f"  State     : {share.state.name}")
                print(f"  Expires   : {expires_str}")
                print(f"  Downloads : {share.statistics.total_downloads} used "
                      f"/ {share.max_downloads} max "
                      f"({remaining} remaining)")
                print(f"  Public URL: {share.public_url or '(not yet available)'}")
                print()

            print("All shares listed successfully.")
            print("\nPress Ctrl+C or wait for the script to finish.")
            print("(Shares will be revoked automatically when the context exits.)")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    finally:
        # -----------------------------------------------------------------------
        # Clean up all temporary files and the temp directory regardless of
        # whether an error occurred.
        # -----------------------------------------------------------------------
        for file_path in tmp_files:
            try:
                os.remove(file_path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        print("\nTemporary files cleaned up.")
        print("Done.")


if __name__ == "__main__":
    main()

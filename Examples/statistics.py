"""
statistics.py – Demonstrate how to retrieve and display statistics from a Sharelink Share.

What this example shows:
    - Creating a temporary file and sharing it via ShareManager.
    - Reading per-share statistics (downloads, bytes, active sessions, etc.)
      from the ShareStatistics object returned by share.statistics.
    - Converting a Share to a plain dictionary with share.to_dict().
    - Explaining what each statistic field means.

When it is useful:
    - When you want to monitor transfer activity, unique visitors, or download
      counts for a share you created programmatically.
    - As a starting point for building dashboards or alerting on share metrics.

Prerequisites:
    - sharelink must be installed in the current Python environment.
    - cloudflared must be available (sharelink downloads it automatically if
      it is not found on PATH).
    - An active internet connection is required for the Cloudflare Tunnel.
"""

import sys
import tempfile
import textwrap
from pathlib import Path

# ── Import the public Sharelink API ──────────────────────────────────────────
try:
    from sharelink import ShareManager
except ImportError as exc:
    print("ERROR: Could not import sharelink.")
    print(exc)
    sys.exit(1)


def print_statistics(share) -> None:
    """Print every field from ShareStatistics in a readable format."""

    # share.statistics returns a frozen ShareStatistics snapshot.
    # All counters reflect the state at the instant of the call.
    stats = share.statistics

    print("\n── Share Statistics ─────────────────────────────────────────")

    # Total number of download sessions ever started for this share,
    # including ones that were interrupted before finishing.
    print(f"  total_downloads      : {stats.total_downloads}")

    # Count of sessions that delivered every requested byte successfully.
    print(f"  completed_downloads  : {stats.completed_downloads}")

    # Sessions currently streaming bytes to a client right now.
    print(f"  active_downloads     : {stats.active_downloads}")

    # Sum of all bytes sent to clients across every session so far.
    print(f"  total_bytes_xferred  : {stats.total_bytes_transferred} bytes")

    # Distinct client IP addresses that have ever accessed this share.
    # Useful for approximating unique visitor counts.
    unique_ip_list = sorted(stats.unique_ips) if stats.unique_ips else ["(none yet)"]
    print(f"  unique_ips           : {', '.join(unique_ip_list)}")

    # UTC timestamp of the very first download, or None if untouched.
    first = stats.first_accessed.isoformat() if stats.first_accessed else "never"
    print(f"  first_accessed       : {first}")

    # UTC timestamp of the most recent download, or None if untouched.
    last = stats.last_accessed.isoformat() if stats.last_accessed else "never"
    print(f"  last_accessed        : {last}")

    print("─────────────────────────────────────────────────────────────")


def print_share_dict(share) -> None:
    """Convert the share to a dict and print selected top-level fields."""

    # to_dict() returns a JSON-serialisable snapshot of the share's full state.
    # This is the same structure served by the REST API at /api/shares/<id>.
    data = share.to_dict()

    print("\n── share.to_dict() – selected fields ────────────────────────")
    for key in (
        "share_id",
        "state",
        "filename",
        "content_type",
        "file_size",
        "max_downloads",
        "downloads_remaining",
        "is_expired",
        "is_exhausted",
        "created_at",
        "expires_at",
        "public_url",
    ):
        # Use .get() so the script stays safe if the schema ever changes.
        print(f"  {key:<22}: {data.get(key)}")
    print("─────────────────────────────────────────────────────────────")


def main() -> None:
    # ── Create a temporary file to share ─────────────────────────────────────
    # We write a small text file so the share has a real, readable resource.
    # The file is cleaned up in the finally block below.
    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="sharelink_stats_demo_",
            delete=False,   # We manage deletion ourselves.
        ) as fh:
            fh.write(
                textwrap.dedent("""\
                    Hello from the sharelink statistics example!
                    This file was created temporarily to demonstrate the
                    ShareStatistics API.  It will be deleted when the
                    script finishes.
                """)
            )
            tmp_file = Path(fh.name)

        print(f"Temporary file created : {tmp_file}")

        # ── Start ShareManager and create a share ─────────────────────────────
        # ShareManager starts a background session sweeper automatically.
        # The context manager calls shutdown() on exit to release all resources.
        print("Starting ShareManager and creating share …")
        with ShareManager() as manager:

            # create_share() binds an HTTP server, launches the Cloudflare
            # Tunnel, and (by default) blocks until the public URL is ready.
            #
            # Replace the path below with any file you want to share.
            share = manager.create_share(
                source=str(tmp_file),        # ← REPLACE with your file path
                expire_seconds=300,          # 5-minute expiry for the demo
                max_downloads=10,            # allow up to 10 downloads
            )

            # ── Display core share info ───────────────────────────────────────
            print("\n── Share created successfully ───────────────────────────────")
            print(f"  share_id  : {share.share_id}")
            print(f"  state     : {share.state.name}")
            print(f"  filename  : {share.filename}")
            print(f"  file_size : {share.file_size} bytes")
            print(f"  expires   : {share.expires_at.isoformat()}")

            # Public URL may be empty if the tunnel hasn't connected yet.
            # wait_for_url() blocks until the URL arrives (already done above
            # because wait_for_url=True is the default in create_share()).
            public_url = share.public_url or "(tunnel not yet connected)"
            print(f"  public URL: {public_url}")

            # ── Print statistics (will show zeros – no downloads yet) ─────────
            # A real script could loop here, sleeping between polls, to watch
            # the counters change as clients download the file.
            print_statistics(share)

            # ── Print the full dict representation ────────────────────────────
            print_share_dict(share)

            # ── Final success message ─────────────────────────────────────────
            print("\nShare created successfully.")
            print(f"Public URL: {share.public_url or '(tunnel connecting…)'}")
            print(
                "\nNote: Statistics show zeros because no downloads have "
                "occurred yet.\nIn a real scenario, poll share.statistics "
                "after clients access the URL."
            )

    except Exception as exc:
        print("\nERROR: An unexpected error occurred.")
        print(exc)
        sys.exit(1)

    finally:
        # ── Clean up the temporary file ───────────────────────────────────────
        if tmp_file is not None and tmp_file.exists():
            tmp_file.unlink()
            print(f"\nTemporary file deleted  : {tmp_file}")


if __name__ == "__main__":
    main()

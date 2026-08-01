"""
share_private_network.py
------------------------
Demonstrates how to use Sharelink to expose an HTTP resource that lives on
a private network (e.g. a local NAS, home server, or internal web service)
through a temporary public Cloudflare Tunnel URL.

When it is useful:
    - You have a file hosted on an internal web server (e.g. http://192.168.1.100/file.mp4)
      that outside users cannot reach directly.
    - You want to share it with someone outside your network without
      configuring port-forwarding or a VPN.
    - Sharelink acts as a proxy: it fetches the file from the private URL
      and serves it through a public Cloudflare Tunnel link.

Prerequisites:
    - Sharelink must be installed (the project files must be on sys.path).
    - The private URL must be reachable from the machine running this script.
    - cloudflared will be downloaded automatically if it is not already present.

Usage:
    python share_private_network.py
"""

import sys
import time

# ---------------------------------------------------------------------------
# CONFIGURATION — replace these values before running
# ---------------------------------------------------------------------------

# The HTTP URL of the resource on your private network.
# The machine running this script must be able to reach this address.
PRIVATE_URL = "http://192.168.1.100/videos/demo.mp4"  # <-- replace this

# How many seconds the share should remain active.
EXPIRE_SECONDS = 300  # 5 minutes

# Maximum number of times the file may be downloaded before the link expires.
MAX_DOWNLOADS = 3

# How long (in seconds) to keep the share alive in this demo before cleaning up.
DEMO_DURATION_SECONDS = 30

# ---------------------------------------------------------------------------
# Import Sharelink
# ---------------------------------------------------------------------------

try:
    from sharelink import ShareManager, ShareConfig
except ImportError as exc:
    print("ERROR: Could not import Sharelink.")
    print("Make sure the sharelink package is on your Python path.")
    print(exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Main demonstration
# ---------------------------------------------------------------------------

def main() -> None:
    # Build a configuration with our chosen expiry and download limit.
    config = ShareConfig(
        expire_seconds=EXPIRE_SECONDS,
        max_downloads=MAX_DOWNLOADS,
    )

    print(f"Private URL to share : {PRIVATE_URL}")
    print(f"Share expires after  : {EXPIRE_SECONDS} seconds")
    print(f"Max downloads        : {MAX_DOWNLOADS}")
    print()

    # ShareManager is used as a context manager so all resources (the embedded
    # HTTP server and the Cloudflare Tunnel process) are cleaned up on exit,
    # even if an exception occurs.
    try:
        with ShareManager(config=config) as manager:
            print("Starting Sharelink — this may take a moment while the")
            print("Cloudflare Tunnel is established …")
            print()

            # create_share() accepts an HTTP/HTTPS URL directly.
            # Sharelink will proxy requests to that URL on behalf of clients.
            # wait_for_url=True (the default) blocks until the public URL is ready.
            share = manager.create_share(
                PRIVATE_URL,
                expire_seconds=EXPIRE_SECONDS,
                max_downloads=MAX_DOWNLOADS,
                wait_for_url=True,
                url_timeout=60.0,   # wait up to 60 s for the tunnel to connect
            )

            # Check that a public URL was actually assigned.
            if not share.public_url:
                print("ERROR: Tunnel connected but no public URL was returned.")
                print("Check your internet connection and try again.")
                sys.exit(1)

            print("Share created successfully.")
            print(f"Public URL : {share.public_url}")
            print()
            print(f"Anyone with the link above can download the file up to")
            print(f"{MAX_DOWNLOADS} time(s) within the next {EXPIRE_SECONDS} seconds.")
            print()
            print(f"Keeping the share alive for {DEMO_DURATION_SECONDS} seconds …")
            print("Press Ctrl+C to stop early.")
            print()

            # Keep the script (and therefore the tunnel) alive for the demo period.
            try:
                time.sleep(DEMO_DURATION_SECONDS)
            except KeyboardInterrupt:
                print()
                print("Interrupted by user.")

            print()
            print("Shutting down — the public URL is now inactive.")

    except Exception as exc:
        print()
        print("ERROR: An unexpected error occurred.")
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
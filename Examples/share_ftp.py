"""
share_ftp.py
------------
Demonstrates how to share a file hosted on a remote FTP server using Sharelink.

Sharelink proxies the FTP resource through a local HTTP server and exposes it
publicly via a Cloudflare Tunnel, producing a shareable HTTPS download link —
no direct access to the FTP server is required from the downloader's side.

When is this useful?
    - Sharing files stored on legacy FTP servers without granting FTP access.
    - Wrapping private FTP credentials behind a time-limited public link.
    - Bridging FTP storage into a modern HTTP download experience.

Prerequisites:
    - Sharelink installed and importable (pip install sharelink or local install).
    - cloudflared available on PATH, or Sharelink will download it automatically.
    - A reachable FTP server with valid credentials and a file to share.
      (Replace the placeholder values below with your own server details.)
"""

import sys

# ---------------------------------------------------------------------------
# FTP connection details — REPLACE THESE with your own server information.
# ---------------------------------------------------------------------------

FTP_HOST     = "ftp.example.com"          # Hostname or IP of your FTP server
FTP_PORT     = 21                          # Standard FTP port; change if needed
FTP_USERNAME = "your_ftp_username"        # FTP login username
FTP_PASSWORD = "your_ftp_password"        # FTP login password
FTP_PATH     = "/pub/example/dataset.csv" # Absolute path to the file on the FTP server

# ---------------------------------------------------------------------------
# Share settings — adjust expiry and download limits as appropriate.
# ---------------------------------------------------------------------------

EXPIRE_SECONDS = 3_600   # Link expires after 1 hour
MAX_DOWNLOADS  = 5       # Allow at most 5 downloads before the link is closed
URL_TIMEOUT    = 30.0    # Seconds to wait for the Cloudflare tunnel URL

# ---------------------------------------------------------------------------
# Main script
# ---------------------------------------------------------------------------

def main() -> None:
    # Import Sharelink here so any ImportError is caught by the except block.
    try:
        from sharelink import ShareManager
    except ImportError as exc:
        print("Error: Could not import Sharelink.")
        print("Install it with:  pip install sharelink")
        print(exc)
        sys.exit(1)

    print("Starting Sharelink FTP example...")
    print(f"  FTP server : {FTP_HOST}:{FTP_PORT}")
    print(f"  Remote path: {FTP_PATH}")
    print(f"  Expires in : {EXPIRE_SECONDS} seconds")
    print(f"  Max DLs    : {MAX_DOWNLOADS}")
    print()

    # Use the context manager so the tunnel and HTTP server are always shut
    # down cleanly when we leave the block, even if an exception is raised.
    try:
        with ShareManager() as manager:

            # create_share() resolves the FTP URL, binds the embedded HTTP
            # server on an ephemeral port, launches cloudflared, and (because
            # wait_for_url=True by default) blocks until the public HTTPS URL
            # is available or url_timeout seconds elapse.
            share = manager.create_share(
                source       = FTP_PATH,    # Remote path on the FTP server
                ftp_host     = FTP_HOST,
                ftp_port     = FTP_PORT,
                ftp_username = FTP_USERNAME,
                ftp_password = FTP_PASSWORD,
                ftp_passive  = True,        # Passive mode works behind NAT/firewalls
                expire_seconds = EXPIRE_SECONDS,
                max_downloads  = MAX_DOWNLOADS,
                url_timeout    = URL_TIMEOUT,
            )

            # Check whether the tunnel produced a URL within the timeout.
            if not share.public_url:
                print("Warning: tunnel URL was not ready within the timeout.")
                print("The tunnel is still connecting in the background.")
                print("Call share.wait_for_url() to block until it is ready.")
            else:
                print("Share created successfully.")
                print(f"Public URL : {share.public_url}")
                print(f"Share ID   : {share.share_id}")
                print(f"Expires at : {share.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                print()

            # Keep the share alive until the user presses Enter.
            # The tunnel remains open for the full duration of this block.
            input("Press Enter to revoke the share and shut down...")

            # Explicitly revoke the share before leaving the context manager.
            # This is optional — the context manager will stop everything on
            # exit — but revoking early closes the public link immediately.
            manager.delete_share(share.share_id)
            print("Share revoked. Shutting down.")

    except KeyboardInterrupt:
        # Allow Ctrl-C to exit gracefully without a traceback.
        print("\nInterrupted by user. Shutting down.")

    except Exception as exc:
        print("\nError: an unexpected error occurred while running the example.")
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
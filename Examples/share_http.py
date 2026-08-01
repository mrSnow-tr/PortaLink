"""
share_http.py — Sharelink HTTP/HTTPS resource sharing example.

What this demonstrates:
    How to use Sharelink to proxy an existing public HTTP or HTTPS resource
    through a Cloudflare Tunnel and expose it as a new public download URL.
    This is useful when you want to re-share a remote file (e.g. a dataset,
    release binary, or report) without downloading it first — Sharelink
    streams the bytes directly from the upstream URL to the downloader.

When it is useful:
    - Sharing a remote file via a short-lived, download-limited link.
    - Wrapping an unauthenticated upstream URL with Sharelink's expiry and
      download-count controls.
    - Testing the HTTP source-type code path without needing a local file.

Prerequisites:
    - Sharelink installed and importable (run from the project root or after
      installing the package).
    - cloudflared available on PATH or downloadable by Sharelink automatically.
    - An internet connection so the Cloudflare Tunnel can be established.
"""

import sys

# ---------------------------------------------------------------------------
# Configuration — replace this URL with any publicly reachable HTTP/HTTPS
# resource you want to re-share.
# ---------------------------------------------------------------------------

# REPLACE THIS: point to any real HTTP/HTTPS file URL you want to share.
UPSTREAM_URL = "https://speed.hetzner.de/100MB.bin"  # 100 MB test file

# How long (in seconds) the generated share link should remain valid.
EXPIRE_SECONDS = 3600  # 1 hour

# Maximum number of times the link can be downloaded before it is revoked.
MAX_DOWNLOADS = 5

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Import Sharelink here so any ImportError surfaces as a clear message.
    try:
        from sharelink import ShareManager, ShareConfig
    except ImportError as exc:
        print("ERROR: Could not import Sharelink.")
        print("Make sure you are running this script from the project root,")
        print("or that the package has been installed (pip install -e .).")
        print(exc)
        sys.exit(1)

    print("Sharelink — HTTP resource sharing example")
    print("=========================================")
    print(f"Upstream URL : {UPSTREAM_URL}")
    print(f"Expires in   : {EXPIRE_SECONDS} seconds")
    print(f"Max downloads: {MAX_DOWNLOADS}")
    print()

    # Build a configuration object with our chosen expiry and download limit.
    config = ShareConfig(
        expire_seconds=EXPIRE_SECONDS,
        max_downloads=MAX_DOWNLOADS,
    )

    # ShareManager is the main entry point.  Using it as a context manager
    # guarantees that all background threads and the Cloudflare Tunnel process
    # are cleaned up automatically when the block exits, even on error.
    try:
        with ShareManager(config=config) as manager:

            print("Starting share — waiting for Cloudflare Tunnel URL …")

            # Pass the upstream HTTP URL directly as the source.
            # Sharelink detects the http/https scheme and selects the HTTP
            # download handler automatically (see manager._resolve_source).
            # wait_for_url=True (the default) blocks here until the tunnel
            # announces its public URL or url_timeout seconds elapse.
            share = manager.create_share(
                source=UPSTREAM_URL,
                wait_for_url=True,
                url_timeout=60.0,   # give the tunnel up to 60 s to connect
            )

            # If the tunnel did not produce a URL within the timeout, public_url
            # will be an empty string.  We treat that as a soft failure and let
            # the user decide whether to wait longer.
            if not share.public_url:
                print(
                    "WARNING: The tunnel did not report a public URL within the "
                    "timeout.\nThe tunnel may still be connecting — check your "
                    "internet connection and try again."
                )
                sys.exit(1)

            # The share is live.  Print the details the user needs.
            print()
            print("Share created successfully.")
            print(f"Public URL   : {share.public_url}")
            print(f"Share ID     : {share.share_id}")
            print(f"State        : {share.state.name}")
            print()
            print("Anyone with the URL above can download the file.")
            print("The link will expire automatically when the time limit or")
            print("download limit is reached, whichever comes first.")
            print()

            # Keep the tunnel alive until the user presses Enter.
            # Pressing Ctrl-C also works — the KeyboardInterrupt falls through
            # to the except block below and the context manager cleans up.
            input("Press Enter to revoke the share and exit …")

            # (The context manager calls manager.shutdown() here, which stops
            # all sessions and the background Cloudflare Tunnel process.)

    except KeyboardInterrupt:
        # Ctrl-C is a normal way to stop an interactive example.
        print("\nInterrupted — shutting down.")

    except Exception as exc:
        print(f"\nERROR: An unexpected error occurred: {exc}")
        sys.exit(1)

    print("Share revoked. Tunnel closed. Goodbye.")


if __name__ == "__main__":
    main()
"""
basic_file.py — Hello World for Sharelink

Demonstrates:
    Creating a temporary file, sharing it with ShareManager using default
    settings, and printing the public Cloudflare Tunnel URL so anyone on
    the internet can download the file.

Useful when:
    You want the simplest possible introduction to Sharelink, or you need
    to quickly share a single file without any custom configuration.

Prerequisites:
    - sharelink package installed in your Python environment
    - An internet connection (Cloudflare Tunnel needs to phone home)
    - cloudflared binary available, or sharelink will download it automatically
      to ~/.sharelink/bin/cloudflared on first run
"""

import sys
import tempfile
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Import Sharelink — the only non-stdlib dependency required here
# ---------------------------------------------------------------------------
try:
    from sharelink import ShareManager
except ImportError as exc:
    print("Error: sharelink is not installed.")
    print(exc)
    sys.exit(1)


def main() -> None:
    # -----------------------------------------------------------------------
    # Step 1: Create a temporary file with some content to share.
    #
    # In a real script you would replace this with a path to your own file,
    # for example:
    #   file_to_share = Path("/home/alice/documents/report.pdf")
    # -----------------------------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="sharelink_example_")
    file_to_share = Path(tmp_dir) / "hello_sharelink.txt"

    file_to_share.write_text(
        textwrap.dedent("""\
            Hello from Sharelink!
            =====================
            This file was shared using the Sharelink "Hello World" example.
            Anyone with the public URL can download it until the share expires
            or is manually shut down.
        """),
        encoding="utf-8",
    )

    print(f"Temporary file created: {file_to_share}")

    # -----------------------------------------------------------------------
    # Step 2: Start a ShareManager.
    #
    # Using it as a context manager ensures that all background threads,
    # HTTP server sockets, and the Cloudflare Tunnel process are cleaned up
    # automatically when the block exits — even if an exception is raised.
    # -----------------------------------------------------------------------
    try:
        with ShareManager() as manager:

            # -----------------------------------------------------------------
            # Step 3: Share the file.
            #
            # create_share() binds a local HTTP server to an ephemeral port,
            # launches cloudflared, and — because wait_for_url=True by default
            # — blocks until the tunnel reports a public HTTPS URL.
            #
            # Default settings applied here:
            #   expire_seconds = 86 400  (24 hours)
            #   max_downloads  = 10
            # -----------------------------------------------------------------
            print("Starting Cloudflare Tunnel… (this may take a few seconds)")

            share = manager.create_share(str(file_to_share))

            # -----------------------------------------------------------------
            # Step 4: Print the result.
            #
            # public_url is the full HTTPS link that anyone can paste into a
            # browser or pass to curl/wget to download the file.
            # -----------------------------------------------------------------
            print("\nShare created successfully.")
            print(f"Public URL : {share.public_url}")
            print(f"File       : {file_to_share.name}")
            print(f"Expires in : 24 hours  |  Max downloads: 10")
            print("\nPress Enter to stop sharing and exit…")

            # Wait for the user — the share stays live until they press Enter.
            input()

        # ShareManager.__exit__ has now stopped the server and tunnel.
        print("Share stopped. Goodbye!")

    except Exception as exc:
        print("\nError: something went wrong while sharing the file.")
        print(exc)
        sys.exit(1)

    finally:
        # -------------------------------------------------------------------
        # Step 5: Clean up the temporary file and directory we created.
        #
        # This block runs whether the share succeeded, failed, or the user
        # pressed Ctrl+C, so we never leave temp files behind.
        # -------------------------------------------------------------------
        try:
            file_to_share.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass  # Best-effort cleanup; not critical if it fails.


if __name__ == "__main__":
    main()
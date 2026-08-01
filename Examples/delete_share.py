"""
delete_share.py
---------------
Demonstrates how to manually delete an active share using ShareManager.

What this example shows:
    - Creating a temporary local file to share.
    - Starting a share with ShareManager and obtaining its public URL.
    - Explicitly revoking the share via delete_share().
    - Verifying the share is gone by checking the registry afterward.

When it is useful:
    - When you need to invalidate a share before its expiry time (e.g. after
      the recipient confirms receipt, or if the link was shared by mistake).

Prerequisites:
    - sharelink must be installed in your Python environment.
    - cloudflared must be available (sharelink will download it automatically
      if it is not found on your PATH).
    - An internet connection is required so cloudflared can establish a
      Cloudflare Tunnel and obtain a public URL.
"""

import sys
import time
import tempfile
import os

# ---------------------------------------------------------------------------
# Import ShareManager from the sharelink package.
# ---------------------------------------------------------------------------
try:
    from sharelink import ShareManager
except ImportError as exc:
    print("ERROR: Could not import sharelink.")
    print("       Install it with:  pip install sharelink")
    print(exc)
    sys.exit(1)


def main() -> None:
    # -----------------------------------------------------------------------
    # 1. Create a temporary file to use as the shared resource.
    #    In a real scenario you would point this at an existing file, e.g.:
    #        source = "/home/alice/report.pdf"
    # -----------------------------------------------------------------------
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,   # We will clean up manually in the finally block.
    )
    tmp_path = tmp_file.name
    tmp_file.write("Hello from sharelink! This file is a demo.\n")
    tmp_file.close()

    print(f"Temporary file created: {tmp_path}")

    try:
        # -------------------------------------------------------------------
        # 2. Start a ShareManager.
        #    Using the context manager guarantees that all sessions and the
        #    background sweeper are shut down cleanly when the block exits,
        #    even if an exception occurs.
        # -------------------------------------------------------------------
        with ShareManager() as manager:

            # ---------------------------------------------------------------
            # 3. Create a share for our temporary file.
            #
            #    expire_seconds=3600  → the link expires in 1 hour at most.
            #    max_downloads=5      → the link allows up to 5 downloads.
            #    wait_for_url=True    → block until the tunnel URL is ready.
            #    url_timeout=30.0     → give up waiting after 30 seconds.
            #
            #    Adjust these values to suit your use-case.
            # ---------------------------------------------------------------
            print("\nCreating share – waiting for tunnel URL (up to 30 s)…")
            try:
                share = manager.create_share(
                    source=tmp_path,
                    expire_seconds=3600,   # 1 hour  ← change if needed
                    max_downloads=5,       # 5 downloads ← change if needed
                    wait_for_url=True,
                    url_timeout=30.0,
                )
            except Exception as exc:
                print("ERROR: Failed to create share.")
                print(exc)
                sys.exit(1)

            # ---------------------------------------------------------------
            # 4. Print the share details so the user can see what was created.
            # ---------------------------------------------------------------
            print(f"\nShare created successfully.")
            print(f"  Share ID : {share.share_id}")
            print(f"  State    : {share.state.name}")
            print(f"  Public URL: {share.public_url or '(URL not yet available)'}")

            # Warn if the tunnel did not produce a URL in time.
            if not share.public_url:
                print(
                    "\nWARNING: No public URL was obtained within the timeout.\n"
                    "         The tunnel may still be connecting in the background.\n"
                    "         Proceeding to demonstrate delete_share() anyway."
                )

            # ---------------------------------------------------------------
            # 5. Pause briefly to simulate the share being "in use".
            #    In a real script you might wait for user input, or run other
            #    application logic here while the share is live.
            # ---------------------------------------------------------------
            print("\nShare is now active. Waiting 3 seconds before revoking…")
            time.sleep(3)

            # ---------------------------------------------------------------
            # 6. Revoke the share manually.
            #
            #    delete_share() returns True when the share was found and
            #    removed, or False when the share_id was not in the registry
            #    (e.g. it had already expired or been deleted).
            # ---------------------------------------------------------------
            share_id = share.share_id
            deleted = manager.delete_share(share_id)

            if deleted:
                print(f"\nShare '{share_id}' was successfully revoked.")
            else:
                # This would happen if the share expired on its own between
                # creation and the delete_share() call, which is unlikely
                # with a 1-hour TTL but handled here for completeness.
                print(
                    f"\nWARNING: delete_share() returned False for '{share_id}'.\n"
                    "         The share may have already expired or been removed."
                )

            # ---------------------------------------------------------------
            # 7. Verify the deletion by trying to look up the share.
            #
            #    get_share() returns None when the share is not in the
            #    registry, confirming that the revocation was effective.
            # ---------------------------------------------------------------
            lookup = manager.get_share(share_id)
            if lookup is None:
                print("Verification passed: share is no longer in the registry.")
            else:
                # The share is still present (possibly in REVOKED state while
                # an active download is finishing).  This is not an error –
                # the session will be evicted by the background sweeper once
                # all active transfers complete.
                print(
                    f"Note: share is still visible in the registry "
                    f"with state '{lookup.state.name}'. "
                    "It will be evicted once all active downloads finish."
                )

        # ShareManager.__exit__ has run; all tunnels and servers are stopped.
        print("\nShareManager shut down cleanly.")

    finally:
        # -------------------------------------------------------------------
        # 8. Remove the temporary file regardless of what happened above.
        # -------------------------------------------------------------------
        try:
            os.unlink(tmp_path)
            print(f"Temporary file removed: {tmp_path}")
        except OSError as exc:
            print(f"WARNING: Could not remove temporary file '{tmp_path}': {exc}")


if __name__ == "__main__":
    main()

"""
test_import.py
--------------
Verify that the PortaLink package can be imported successfully and that all
primary public symbols are present, correctly typed, and usable in isolation.

The script locates the sharelink package automatically: it first checks whether
'sharelink' is already importable (i.e. installed or on PYTHONPATH), then falls
back to searching for an __init__.py in common relative locations (same dir,
parent dir, or the sibling 'project' directory used in the PortaLink dev layout).

Run with:
    python test_import.py

Exit code 0 = all checks passed
Exit code 1 = at least one check failed
"""

import sys
import importlib.util
import pathlib

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_failures = 0


def pass_(msg: str) -> None:
    print(f"[PASS] {msg}")


def fail_(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {msg}")


def check(condition: bool, pass_msg: str, fail_msg: str) -> None:
    if condition:
        pass_(pass_msg)
    else:
        fail_(fail_msg)


# ---------------------------------------------------------------------------
# 1. Locate and import the sharelink package
#    Try normal import first; fall back to importlib for dev-layout scenarios
#    where the package directory is not named 'sharelink' on sys.path.
# ---------------------------------------------------------------------------

sharelink = None

# Attempt 1: standard import (works when installed or PYTHONPATH is set)
try:
    import sharelink as _sl
    sharelink = _sl
except ImportError:
    pass

# Attempt 2: importlib search relative to this script's location
if sharelink is None:
    script_dir = pathlib.Path(__file__).resolve().parent
    candidates = [
        script_dir / "sharelink",          # sharelink/ next to this script
        script_dir.parent / "sharelink",   # one level up
        script_dir.parent / "project",     # dev layout: repo root / project
        pathlib.Path("/mnt/project"),      # container dev layout
    ]
    for candidate in candidates:
        init = candidate / "__init__.py"
        if init.exists():
            try:
                spec = importlib.util.spec_from_file_location(
                    "sharelink",
                    init,
                    submodule_search_locations=[str(candidate)],
                )
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                sys.modules["sharelink"] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                sharelink = mod
                break
            except Exception as exc:
                print(exc)

if sharelink is None:
    fail_("PortaLink package could not be imported (tried standard import and importlib fallback).")
    print("\n=================================")
    print("TEST RESULT : FAIL")
    print("=================================")
    sys.exit(1)

pass_("PortaLink package imported successfully.")

# ---------------------------------------------------------------------------
# 2. Expected public symbols from __all__
# ---------------------------------------------------------------------------

EXPECTED_EXPORTS = [
    "Share",
    "ShareConfig",
    "ShareManager",
    "ShareState",
    "ShareStatistics",
    "SourceType",
    "configure_logging",
]

for name in EXPECTED_EXPORTS:
    check(
        hasattr(sharelink, name),
        f"sharelink.{name} is present.",
        f"sharelink.{name} is MISSING from the package.",
    )

# ---------------------------------------------------------------------------
# 3. __all__ completeness
# ---------------------------------------------------------------------------

try:
    pkg_all = sharelink.__all__
    assert isinstance(pkg_all, list), "__all__ must be a list"
    for name in EXPECTED_EXPORTS:
        check(
            name in pkg_all,
            f"'{name}' is listed in sharelink.__all__.",
            f"'{name}' is NOT listed in sharelink.__all__.",
        )
except AssertionError as exc:
    print(exc)
    fail_("sharelink.__all__ has unexpected type.")
except Exception as exc:
    print(exc)
    fail_("Unexpected error while inspecting sharelink.__all__.")

# ---------------------------------------------------------------------------
# 4. ShareConfig – instantiation with defaults
# ---------------------------------------------------------------------------

try:
    cfg = sharelink.ShareConfig()
    pass_("ShareConfig() instantiated with default values.")

    check(cfg.expire_seconds == 86_400,
          "ShareConfig.expire_seconds defaults to 86400.",
          f"ShareConfig.expire_seconds unexpected: {cfg.expire_seconds!r}")

    check(cfg.max_downloads == 10,
          "ShareConfig.max_downloads defaults to 10.",
          f"ShareConfig.max_downloads unexpected: {cfg.max_downloads!r}")

    check(cfg.host == "127.0.0.1",
          "ShareConfig.host defaults to '127.0.0.1'.",
          f"ShareConfig.host unexpected: {cfg.host!r}")

    check(cfg.chunk_size > 0,
          "ShareConfig.chunk_size is a positive integer.",
          f"ShareConfig.chunk_size is not positive: {cfg.chunk_size!r}")

except Exception as exc:
    print(exc)
    fail_("ShareConfig() raised an unexpected exception.")

# ---------------------------------------------------------------------------
# 5. ShareConfig – frozen (mutation must be rejected)
# ---------------------------------------------------------------------------

try:
    cfg2 = sharelink.ShareConfig()
    try:
        cfg2.expire_seconds = 999  # type: ignore[misc]
        fail_("ShareConfig is NOT frozen – mutation was silently accepted.")
    except Exception:
        pass_("ShareConfig is correctly frozen (mutation raises an error).")
except Exception as exc:
    print(exc)
    fail_("Unexpected error while testing ShareConfig immutability.")

# ---------------------------------------------------------------------------
# 6. ShareConfig – custom values round-trip
# ---------------------------------------------------------------------------

try:
    custom = sharelink.ShareConfig(expire_seconds=3_600, max_downloads=5)
    assert custom.expire_seconds == 3_600, "expire_seconds mismatch"
    assert custom.max_downloads == 5, "max_downloads mismatch"
    pass_("ShareConfig accepts and stores custom expire_seconds and max_downloads.")
except AssertionError as exc:
    print(exc)
    fail_("ShareConfig custom values did not round-trip correctly.")
except Exception as exc:
    print(exc)
    fail_("ShareConfig raised an exception on valid custom values.")

# ---------------------------------------------------------------------------
# 7. ShareConfig – invalid values are rejected
# ---------------------------------------------------------------------------

try:
    try:
        sharelink.ShareConfig(expire_seconds=-1)
        fail_("ShareConfig accepted a negative expire_seconds (should be rejected).")
    except ValueError:
        pass_("ShareConfig rejects negative expire_seconds with ValueError.")
    except Exception as exc:
        fail_(f"ShareConfig raised unexpected exception type for bad expire_seconds: {exc!r}")
except Exception as exc:
    print(exc)
    fail_("Unexpected error during ShareConfig validation test.")

# ---------------------------------------------------------------------------
# 8. ShareState enum completeness
# ---------------------------------------------------------------------------

EXPECTED_STATES = {"PENDING", "ACTIVE", "EXPIRED", "EXHAUSTED", "REVOKED"}

try:
    actual_states = {m.name for m in sharelink.ShareState}
    check(
        EXPECTED_STATES == actual_states,
        f"ShareState contains all expected members: {sorted(EXPECTED_STATES)}.",
        f"ShareState members mismatch. Expected {sorted(EXPECTED_STATES)}, "
        f"got {sorted(actual_states)}.",
    )
except Exception as exc:
    print(exc)
    fail_("Unexpected error while inspecting ShareState.")

# ---------------------------------------------------------------------------
# 9. SourceType enum – core members present
# ---------------------------------------------------------------------------

CORE_SOURCE_TYPES = {"LOCAL_FILE", "LOCAL_DIRECTORY", "FTP"}

try:
    actual_types = {m.name for m in sharelink.SourceType}
    check(
        CORE_SOURCE_TYPES.issubset(actual_types),
        f"SourceType contains all core members: {sorted(CORE_SOURCE_TYPES)}.",
        f"SourceType is missing core members. Got {sorted(actual_types)}.",
    )
except Exception as exc:
    print(exc)
    fail_("Unexpected error while inspecting SourceType.")

# ---------------------------------------------------------------------------
# 10. ShareManager is a class and has expected methods
# ---------------------------------------------------------------------------

try:
    check(
        isinstance(sharelink.ShareManager, type),
        "sharelink.ShareManager is a class.",
        "sharelink.ShareManager is NOT a class.",
    )
    for method in ("create_share", "get_share", "list_shares", "delete_share", "shutdown"):
        check(
            callable(getattr(sharelink.ShareManager, method, None)),
            f"ShareManager.{method} is callable.",
            f"ShareManager.{method} is missing or not callable.",
        )
except Exception as exc:
    print(exc)
    fail_("Unexpected error while checking ShareManager.")

# ---------------------------------------------------------------------------
# 11. configure_logging is callable
# ---------------------------------------------------------------------------

try:
    check(
        callable(sharelink.configure_logging),
        "sharelink.configure_logging is callable.",
        "sharelink.configure_logging is NOT callable.",
    )
except Exception as exc:
    print(exc)
    fail_("Unexpected error while checking configure_logging.")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print()
print("=================================")
if _failures == 0:
    print("TEST RESULT : PASS")
    print("=================================")
    sys.exit(0)
else:
    print("TEST RESULT : FAIL")
    print("=================================")
    sys.exit(1)

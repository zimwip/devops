# Canonical source: platform/scripts/output.py — do not modify here directly.
# Run 'make sync-shared' in bootstrap/ to refresh from the canonical source.

"""output.py — Shared console output helpers."""

import sys


def out(msg: str):
    print(f"  {msg}")


def step(msg: str):
    print(f"  → {msg}")


def success(msg: str):
    print(f"  ✓ {msg}")


def warn(msg: str):
    print(f"  ! {msg}", file=sys.stderr)


def error_exit(msg: str):
    print(f"\n  [error] {msg}\n", file=sys.stderr)
    sys.exit(1)


def confirm(msg: str):
    """Ask for simple yes/no confirmation; exit if refused."""
    answer = input(f"\n  {msg} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("  Aborted.")
        sys.exit(0)


def confirm_with_actor(disclaimer: str, force: bool = False) -> bool:
    """
    Print the formatted disclaimer and ask for confirmation.

    Parameters
    ----------
    disclaimer : str
        Output of identity.format_disclaimer() — the full box with actor info.
    force : bool
        When True, print the disclaimer (informational) but skip the prompt.

    Returns True when confirmed (always True if force=True).
    Calls sys.exit(0) if the user declines.
    """
    print(disclaimer)
    if force:
        print("  --force flag set — skipping confirmation prompt.")
        print()
        return True
    answer = input("  Proceed? [y/N] ").strip().lower()
    print()
    if answer not in ("y", "yes"):
        print("  Aborted.")
        sys.exit(0)
    return True

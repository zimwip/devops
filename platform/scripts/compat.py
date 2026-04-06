"""
compat.py — Cross-platform compatibility helpers.

Abstracts the differences between Windows and Unix/macOS so that
the rest of the codebase never needs to check sys.platform directly.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def python_cmd() -> str:
    """Return the correct Python executable name for the current OS."""
    if IS_WINDOWS:
        # On Windows 'python' is the standard; 'python3' may not exist
        return "python"
    return "python3"


def git_cmd() -> list[str]:
    """Return the base git command."""
    return ["git"]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """
    Cross-platform subprocess.run wrapper.
    On Windows, shell=True is needed for some commands (npm, mvn wrappers).
    Adds CREATE_NO_WINDOW flag on Windows to avoid console popups.
    """
    flags = {}
    if IS_WINDOWS:
        flags["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(cmd, **flags, **kwargs)


def which(tool: str) -> str | None:
    """Cross-platform shutil.which — checks both 'tool' and 'tool.cmd' on Windows."""
    found = shutil.which(tool)
    if found:
        return found
    if IS_WINDOWS:
        return shutil.which(tool + ".cmd") or shutil.which(tool + ".exe")
    return None


def open_in_browser(url: str):
    """Open a URL in the default browser, cross-platform."""
    import webbrowser
    webbrowser.open(url)


def clear_screen():
    """Clear the terminal, cross-platform."""
    os.system("cls" if IS_WINDOWS else "clear")


def set_executable(path: Path):
    """Make a file executable (no-op on Windows, chmod +x on Unix)."""
    if not IS_WINDOWS:
        import stat
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def paths_equal(a: Path, b: Path) -> bool:
    """Case-insensitive path comparison on Windows, case-sensitive on Unix."""
    if IS_WINDOWS:
        return str(a).lower() == str(b).lower()
    return a == b


def env_path_sep() -> str:
    """Return the PATH separator for the current OS."""
    return ";" if IS_WINDOWS else ":"


def format_path_for_display(p: Path) -> str:
    """Return a path string suitable for display in terminal output."""
    return str(p).replace("/", os.sep)


def git_executable() -> str:
    """Return the git executable, raising a clear error if not found."""
    exe = which("git")
    if not exe:
        raise FileNotFoundError(
            "git not found. Install Git from https://git-scm.com/download/win"
            if IS_WINDOWS else
            "git not found. Install via your package manager."
        )
    return exe


def node_executable() -> str | None:
    """Return the node executable or None if not installed."""
    return which("node")


def npm_executable() -> str | None:
    """Return the npm executable or None if not installed."""
    exe = which("npm")
    if IS_WINDOWS and not exe:
        exe = which("npm.cmd")
    return exe


def helm_executable() -> str | None:
    """Return the helm executable or None."""
    return which("helm")


def kubectl_executable() -> str | None:
    """Return kubectl or oc (OpenShift CLI), whichever is available."""
    return which("oc") or which("kubectl")


def uvicorn_cmd() -> list[str]:
    """Return the uvicorn command appropriate for the OS."""
    uvicorn = which("uvicorn")
    if uvicorn:
        return [uvicorn]
    # Fallback: run as Python module
    return [python_cmd(), "-m", "uvicorn"]

"""Connection to DaVinci Resolve's scripting API.

Resolve ships an official Python module, ``DaVinciResolveScript`` (a thin
wrapper over ``fusionscript``). It is only importable when Resolve is running
and the environment points at the right paths. This module does that bootstrap
across platforms and hands back the live ``resolve`` app object.

Docs: https://deric.github.io/DaVinciResolve-API-Docs/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class ResolveUnavailable(RuntimeError):
    """Raised when Resolve isn't running or the API can't be reached."""


# Default install locations of the scripting API per platform.
_DEFAULT_API = {
    "win32": r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "linux": "/opt/resolve/Developer/Scripting",
}
_DEFAULT_LIB = {
    "win32": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "linux": "/opt/resolve/libs/Fusion/fusionscript.so",
}


def _bootstrap_paths() -> None:
    """Make ``DaVinciResolveScript`` importable by setting the env Resolve expects."""
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    api = os.environ.get("RESOLVE_SCRIPT_API", _DEFAULT_API.get(platform, ""))
    lib = os.environ.get("RESOLVE_SCRIPT_LIB", _DEFAULT_LIB.get(platform, ""))
    if api:
        os.environ.setdefault("RESOLVE_SCRIPT_API", api)
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)
        modules = str(Path(api) / "Modules")
        if modules not in sys.path:
            sys.path.append(modules)


_resolve = None  # cached app handle


def get_resolve(refresh: bool = False):
    """Return the live Resolve app object, connecting on first use.

    Raises ``ResolveUnavailable`` with an actionable message if Resolve isn't
    reachable (not installed, not running, or scripting disabled in prefs).
    """
    global _resolve
    if _resolve is not None and not refresh:
        return _resolve

    _bootstrap_paths()
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except ImportError as exc:  # module path wrong / not installed
        raise ResolveUnavailable(
            "Could not import DaVinciResolveScript. Is DaVinci Resolve installed? "
            "Set RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB if it's in a custom path."
        ) from exc

    app = dvr.scriptapp("Resolve")
    if app is None:
        raise ResolveUnavailable(
            "Resolve is not running, or external scripting is disabled. "
            "Open Resolve and set Preferences > System > General > "
            "'External scripting using' to Local or Network."
        )
    _resolve = app
    return app


def is_available() -> bool:
    try:
        get_resolve(refresh=True)
        return True
    except ResolveUnavailable:
        return False

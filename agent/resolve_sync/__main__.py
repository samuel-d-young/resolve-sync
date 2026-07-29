"""Entry point:  python -m resolve_sync   (or the frozen Resolve Sync.exe)

Runs as a desktop app by default: a real window plus a tray icon, with the
FastAPI server on a background thread. Falls back to the browser whenever the
window cannot start, so the app is never unusable.

  --web     skip the window, just serve and open a browser tab
  --server  skip the window AND the browser (headless; useful for debugging)

Written to survive having NO CONSOLE. Under pythonw.exe or a PyInstaller
``--noconsole`` build, ``sys.stdout``/``sys.stderr`` are ``None``; any print()
or logging write raises immediately, and uvicorn's logging setup dies during
startup — leaving a live process that never binds its port and reports nothing.
So the first thing we do is give the process real streams.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import webbrowser
from pathlib import Path


def _log_path() -> Path:
    base = os.environ.get("APPDATA")
    d = Path(base) / "ResolveSync" if base else Path.home() / ".resolve-sync"
    d.mkdir(parents=True, exist_ok=True)
    return d / "agent.log"


def _attach_streams() -> None:
    """Guarantee sys.stdout/sys.stderr exist BEFORE anything imports uvicorn."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        fh = open(_log_path(), "a", encoding="utf-8", buffering=1)
    except OSError:
        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def main() -> None:
    # Must precede everything else in a frozen Windows app, or it spawn-loops.
    multiprocessing.freeze_support()
    _attach_streams()

    import uvicorn

    from .config import Config
    from .desktop import already_running, run_desktop
    from .server import app

    cfg = Config.load()
    url = f"http://{cfg.host}:{cfg.port}"
    mode = "web" if "--web" in sys.argv else "server" if "--server" in sys.argv else "desktop"

    # A second launch should surface the running copy, not fight for the port.
    if already_running(cfg.port):
        print(f"Resolve Sync is already running at {url}", flush=True)
        if mode != "server":
            webbrowser.open(url)
        return

    # Pass the app OBJECT, not an import string: the string form asks uvicorn to
    # re-import by name, which is the path into reload/worker machinery that a
    # frozen build cannot support.
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info", workers=1)
    )

    def serve() -> None:
        server.run()

    def stop_server() -> None:
        server.should_exit = True

    print(f"Resolve Sync -> {url}  (mode: {mode})", flush=True)

    if mode == "desktop":
        if run_desktop(url, serve, stop_server):
            return                      # window closed and the user quit
        # Window unavailable (no WebView2 runtime, etc.) — carry on in a browser.
        print("Desktop window unavailable; falling back to the browser.", flush=True)
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        server.run()
        return

    if mode == "web":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server.run()


if __name__ == "__main__":
    main()

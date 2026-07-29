"""Desktop shell: a real application window and a tray icon.

The same dashboard HTML that the web interface serves is rendered here in an
embedded WebView2 window, so there is ONE user interface to build and polish
rather than two.

Hard constraints this module exists to satisfy:
  * The webview MUST own the main thread on Windows, so uvicorn runs in a
    background thread and the window blocks the main one.
  * A failed webview must never make the app unusable — we fall back to opening
    the user's browser at the same URL.
  * Closing the window hides to tray (a sync tool that quits when you close a
    window silently stops syncing). Quit is explicit, from the tray menu.
  * Only one instance may own port 7788, so a second launch focuses the first
    rather than fighting over the port.
"""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
import webbrowser
from dataclasses import dataclass

APP_NAME = "Resolve Sync"
_MUTEX_NAME = "Global\\ResolveSyncSingleInstance"


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
def already_running(port: int) -> bool:
    """True if another copy already owns the port.

    Two checks because they fail differently: the mutex catches a second launch
    before its server binds, the socket probe catches an agent started some
    other way (e.g. `python -m resolve_sync`).
    """
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if handle and kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
                return True
        except Exception:  # noqa: BLE001 - never block startup on this
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def _icon_path() -> "Path | None":
    """The shipped icon, in the frozen bundle or the source tree."""
    from pathlib import Path
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "static" / "icon" / "icon-64.png",
                 here.parent / "static" / "icon" / "icon.png"):
        if cand.is_file():
            return cand
    return None


def _icon_image(colour: str = "#4b9fff"):
    from PIL import Image, ImageDraw

    # Prefer the real artwork; the drawn circle is only a fallback so a missing
    # asset can never stop the tray icon appearing.
    path = _icon_path()
    if path is not None:
        try:
            return Image.open(path).convert("RGBA")
        except Exception:  # noqa: BLE001
            pass
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=colour)
    d.ellipse((20, 20, size - 20, size - 20), fill=(14, 17, 22, 255))
    return img


@dataclass
class TrayState:
    status: str = "Starting…"


class Tray:
    """System-tray presence. Optional: failure here must not stop the app."""

    def __init__(self, url: str, on_show, on_quit):
        self.url, self.on_show, self.on_quit = url, on_show, on_quit
        self.state = TrayState()
        self._icon = None

    def start(self) -> None:
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open Resolve Sync", lambda *_: self.on_show(), default=True),
                pystray.MenuItem("Open in browser", lambda *_: webbrowser.open(self.url)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda *_: self.on_quit()),
            )
            self._icon = pystray.Icon(APP_NAME, _icon_image(), APP_NAME, menu)
            self._icon.run_detached()
        except Exception:  # noqa: BLE001 - tray is a nicety, not a requirement
            self._icon = None

    def set_status(self, text: str, colour: str = "#4b9fff") -> None:
        self.state.status = text
        if self._icon is None:
            return
        try:
            self._icon.title = f"{APP_NAME} — {text}"
            self._icon.icon = _icon_image(colour)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def run_desktop(url: str, serve, stop_server) -> bool:
    """Start the server thread, then own the main thread with the window.

    Returns True if the desktop window ran; False if the caller should fall
    back to a browser.
    """
    server_thread = threading.Thread(target=serve, name="uvicorn", daemon=True)
    server_thread.start()

    try:
        import webview
    except ImportError:
        return False

    window = webview.create_window(
        APP_NAME, url,
        width=1180, height=820, min_size=(900, 640),
        background_color="#28282e",
        text_select=True,
    )

    tray = Tray(url, on_show=lambda: _safe(window.show), on_quit=lambda: _quit(window, tray, stop_server))
    tray.start()
    tray.set_status("Running")

    # Closing the window hides it; the agent keeps syncing in the background.
    quitting = {"flag": False}

    def _on_closing():
        if quitting["flag"]:
            return True
        _safe(window.hide)
        tray.set_status("Running in the background")
        return False       # veto the close

    try:
        window.events.closing += _on_closing
    except Exception:  # noqa: BLE001 - older pywebview API
        pass

    def _quit_now():
        quitting["flag"] = True
        _quit(window, tray, stop_server)

    tray.on_quit = _quit_now

    try:
        webview.start()          # blocks on the main thread until the app quits
    except Exception:            # noqa: BLE001 - no WebView2 runtime, etc.
        tray.stop()
        return False
    tray.stop()
    return True


def _safe(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


def _quit(window, tray, stop_server) -> None:
    _safe(stop_server)
    tray.stop()
    try:
        window.destroy()
    except Exception:  # noqa: BLE001
        pass

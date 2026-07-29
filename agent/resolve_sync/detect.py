"""Auto-detection so the editor picks from a list instead of typing paths.

Two jobs:
  1. Find cloud-sync folders (Dropbox / Google Drive / OneDrive) that can host
     the version store — no OAuth needed, the desktop clients already mirror a
     plain folder for us.
  2. Suggest media roots by reading the media paths already recorded in the
     user's own Resolve project databases, so they tick boxes rather than
     browsing a file picker.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Cloud providers
# ---------------------------------------------------------------------------
@dataclass
class Provider:
    key: str          # "dropbox" | "gdrive" | "onedrive"
    name: str         # display name
    path: str         # the synced folder we'd store versions inside
    note: str = ""    # provider-specific caution, shown in the UI

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "path": self.path, "note": self.note}


def _dropbox_paths() -> list[Path]:
    """Dropbox records its folder(s) in info.json — more reliable than guessing."""
    out: list[Path] = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        info = Path(base) / "Dropbox" / "info.json"
        if info.is_file():
            try:
                data = json.loads(info.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for entry in data.values():
                p = entry.get("path")
                if p and Path(p).is_dir():
                    out.append(Path(p))
    fallback = Path.home() / "Dropbox"
    if not out and fallback.is_dir():
        out.append(fallback)
    return out


def _gdrive_paths() -> list[Path]:
    """Google Drive for desktop: mirrored 'My Drive' folders, or a mounted letter."""
    out: list[Path] = []
    for cand in (Path.home() / "Google Drive", Path.home() / "My Drive"):
        if cand.is_dir():
            out.append(cand)
    if sys.platform == "win32":
        for letter in "GHIJKLMNOPQRSTUVWXYZ":
            my = Path(f"{letter}:/My Drive")
            if my.is_dir():
                out.append(my)
    return out


def _onedrive_paths() -> list[Path]:
    out: list[Path] = []
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(env)
        if v and Path(v).is_dir():
            out.append(Path(v))
    return out


def find_providers() -> list[Provider]:
    """Cloud-sync folders present on this machine, best candidates first."""
    found: list[Provider] = []
    seen: set[str] = set()

    def add(key: str, name: str, path: Path, note: str = "") -> None:
        # Store inside a dedicated subfolder so we never touch their other files.
        target = str(path / "ResolveSync")
        if target.lower() in seen:
            return
        seen.add(target.lower())
        found.append(Provider(key, name, target, note))

    for p in _dropbox_paths():
        add("dropbox", "Dropbox", p)
    for p in _gdrive_paths():
        add("gdrive", "Google Drive", p,
            "Set Google Drive to 'Mirror files' (not 'Stream files') so your "
            "projects are really on this disk.")
    for p in _onedrive_paths():
        add("onedrive", "OneDrive", p,
            "Keep the ResolveSync folder marked 'Always keep on this device'.")
    return found


# ---------------------------------------------------------------------------
# Media roots, mined from Resolve's own project databases
# ---------------------------------------------------------------------------
def _dblist_paths() -> list[Path]:
    """Disk-database roots from dblist.conf.

    The file is colon-delimited AND the drive colon is stripped, so
    'C\\Users\\...' means 'C:\\Users\\...'. A naive split eats the drive letter.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    conf = Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Preferences" / "dblist.conf"
    roots: list[Path] = []
    if not conf.is_file():
        return roots
    for line in conf.read_text("utf-8", errors="replace").splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        # Restore the drive colon: "C\Users\..." -> "C:\Users\..."
        raw = re.sub(r"^([A-Za-z])\\", r"\1:\\", raw)
        p = Path(raw)
        if p.is_dir():
            roots.append(p)
    return roots


def _media_paths_from_db(db: Path, limit: int = 20000) -> list[str]:
    """Read media file paths out of one Resolve Project.db (read-only)."""
    paths: list[str] = []
    try:
        # immutable=1 so we never lock or modify the user's database.
        uri = f"file:{db.as_posix()}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return paths
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Sm2TiItem%'"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT DISTINCT "MediaFilePath" FROM "{t}" LIMIT {limit}')
            except sqlite3.Error:
                continue
            for (val,) in cur.fetchall():
                if val and isinstance(val, str):
                    paths.append(val)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return paths


# Broad containers that are useless as a media root on their own — group one
# level deeper so we suggest "C:\Users\sam\Videos", never "C:\Users".
_TOO_BROAD = {"users", "volumes", "home", "mnt", "media", "program files", "windows"}


def _group_root(raw: str) -> str | None:
    """Collapse a media file path to a sensible suggestible folder."""
    raw = raw.strip()
    if not raw:
        return None
    # Foreign-OS paths (a project edited on a Mac) can't be roots on Windows.
    if sys.platform == "win32" and (raw.startswith("/") or raw.startswith("\\")) \
            and not raw.startswith("\\\\"):
        return None

    parts = Path(raw).parts
    if len(parts) < 2:
        return None
    depth = 2
    if len(parts) > 2 and parts[1].strip("\\/").lower() in _TOO_BROAD:
        depth = min(4, len(parts) - 1)   # descend past Users/<name> etc.
    return str(Path(*parts[:depth]))


@dataclass
class MediaRootSuggestion:
    path: str
    clips: int
    online: bool     # is the folder reachable right now?

    def to_dict(self) -> dict:
        return {"path": self.path, "clips": self.clips, "online": self.online}


def suggest_media_roots(max_dbs: int = 250, top: int = 12) -> list[MediaRootSuggestion]:
    """Suggest media roots from paths already referenced by the user's projects.

    Works with Resolve closed, and does not need the scripting API — so it can
    run during first-run setup before anything else is configured.
    """
    counts: Counter[str] = Counter()
    scanned = 0
    for root in _dblist_paths():
        for db in root.rglob("Project.db"):
            if scanned >= max_dbs:
                break
            scanned += 1
            for raw in _media_paths_from_db(db):
                key = _group_root(raw)
                if key:
                    counts[key] += 1

    out = [
        MediaRootSuggestion(path=path, clips=n, online=Path(path).is_dir())
        for path, n in counts.most_common(top)
    ]
    # Reachable folders first; the rest are probably unplugged drives.
    out.sort(key=lambda s: (not s.online, -s.clips))
    return out

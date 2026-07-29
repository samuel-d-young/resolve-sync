"""Read Resolve's own project metadata cache.

Resolve keeps an unencrypted SQLite cache of every project's headline
properties — the same fields its Project Manager list view shows:

    Name · Last Modified · Timelines · Format · Frame Rate · Date Created · Note

Reading it lets us render a real Project Manager without loading Resolve,
without parsing .drp files, and even while Resolve is closed. Opened read-only
and immutable so we can never disturb Resolve's own file.

    %APPDATA%/Blackmagic Design/DaVinci Resolve/Support/Resolve Disk Database/
        Resolve Projects/Users/guest/ProjectMetadataCache/Metadata.db
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectMeta:
    name: str
    unique_id: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    modified: str = ""
    created: str = ""
    timelines: int = 0
    note: str = ""
    folder: str = ""          # which Resolve folder it lives in ("" = root)

    @property
    def format(self) -> str:
        if not (self.width and self.height):
            return ""
        # Editors read 3840x2160 as "UHD"; keep the exact numbers too.
        common = {
            (1920, 1080): "HD", (3840, 2160): "UHD", (4096, 2160): "4K DCI",
            (1280, 720): "720p", (7680, 4320): "8K",
        }
        label = common.get((self.width, self.height))
        return f"{self.width} x {self.height}" + (f"  {label}" if label else "")

    @property
    def frame_rate(self) -> str:
        if not self.fps:
            return ""
        return f"{self.fps:g} fps"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "unique_id": self.unique_id,
            "width": self.width, "height": self.height,
            "format": self.format, "fps": self.fps, "frame_rate": self.frame_rate,
            "modified": self.modified, "created": self.created,
            "timelines": self.timelines, "note": self.note, "folder": self.folder,
        }


def _cache_dbs() -> list[tuple[Path, str]]:
    """Every Metadata.db plus the Resolve folder it describes."""
    from .detect import _dblist_paths

    out: list[tuple[Path, str]] = []
    for root in _dblist_paths():
        for db in root.rglob("ProjectMetadataCache/Metadata.db"):
            # .../Projects/<folder…>/ProjectMetadataCache/Metadata.db
            parts = db.parts
            folder = ""
            if "Projects" in parts:
                idx = parts.index("Projects")
                between = parts[idx + 1:-2]          # drop ProjectMetadataCache/Metadata.db
                folder = "/".join(between)
            out.append((db, folder))
    return out


def _read_db(db: Path, folder: str) -> list[ProjectMeta]:
    try:
        # immutable=1: never lock or modify Resolve's file, even if it's open.
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    rows: list[ProjectMeta] = []
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_metadata'")
        if not cur.fetchone():
            return []
        cur.execute(
            "SELECT key, uniqueId, width, height, fps, modDateTime, createDateTime,"
            " numTimelines, notes FROM project_metadata"
        )
        for key, uid, w, h, fps, mod, created, tl, note in cur.fetchall():
            if not key:
                continue
            rows.append(ProjectMeta(
                name=str(key), unique_id=str(uid or ""),
                width=int(w or 0), height=int(h or 0), fps=float(fps or 0),
                modified=str(mod or ""), created=str(created or ""),
                timelines=int(tl or 0), note=str(note or ""), folder=folder,
            ))
    except sqlite3.Error:
        return rows
    finally:
        con.close()
    return rows


def folders_on_disk() -> dict[str, str]:
    """project name -> Resolve folder path, read from the database DIRECTORY tree.

    Resolve mirrors its folder tree on disk: each folder is a directory, each
    project is a directory containing Project.db. Deriving the tree this way
    costs one directory walk and — crucially — does NOT touch Resolve's
    scripting API.

    That matters enormously: the API's folder cursor is GLOBAL and shared with
    the Project Manager window the user is looking at. Walking it on a poll
    moves the view under their cursor mid-click. Never navigate Resolve to
    answer a question the filesystem can answer.
    """
    from .detect import _dblist_paths

    out: dict[str, str] = {}
    for root in _dblist_paths():
        base = root / "Resolve Projects" / "Users" / "guest" / "Projects"
        if not base.is_dir():
            continue

        def walk(d: Path, rel: str) -> None:
            try:
                children = list(d.iterdir())
            except OSError:
                return
            for c in children:
                if not c.is_dir():
                    continue
                if (c / "Project.db").is_file():
                    out.setdefault(c.name, rel)
                else:
                    walk(c, f"{rel}/{c.name}" if rel else c.name)

        walk(base, "")
    return out


def all_projects() -> dict[str, ProjectMeta]:
    """Every project Resolve knows about, keyed by name (newest cache wins)."""
    found: dict[str, ProjectMeta] = {}
    for db, folder in _cache_dbs():
        for meta in _read_db(db, folder):
            existing = found.get(meta.name)
            if existing is None or meta.modified > existing.modified:
                found[meta.name] = meta
    return found


def enrich(names: list[str]) -> dict[str, dict]:
    """Metadata for the given project names, as plain dicts for the API."""
    meta = all_projects()
    return {n: meta[n].to_dict() for n in names if n in meta}

"""Real frame thumbnails for the project grid.

Resolve does not store thumbnails anywhere we can read — its Project.db keeps a
`ThumbnailDirtyFlag` but no image data, because it regenerates them on demand.
So we make our own, the same way Resolve would: pick a clip the project
actually uses and decode one frame from it.

The pipeline, all of which works with Resolve CLOSED:

    Project.db  ->  Sm2TiItem.MediaFilePath   (clips the project references)
    ffmpeg      ->  one frame, seeked a little way in
    cache       ->  %APPDATA%/ResolveSync/thumbs/<hash>.jpg

Everything is best-effort: a missing ffmpeg, an unplugged drive or an exotic
codec just means no thumbnail, never an error the user has to care about.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

# Seek a few seconds in — frame 0 of a camera clip is very often black, a slate,
# or a lens cap, which makes for a useless poster frame.
SEEK_SECONDS = 3.0
THUMB_WIDTH = 480

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Only try containers ffmpeg reliably decodes a still from. Skips audio, XML,
# stills and anything that would waste a subprocess launch.
_VIDEO_EXT = {
    ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".mts", ".m2ts",
    ".braw", ".r3d", ".dng", ".insv", ".webm", ".wmv", ".mpg", ".mpeg",
}

_lock = threading.Lock()
_inflight: set[str] = set()


def cache_dir() -> Path:
    base = os.environ.get("APPDATA")
    d = (Path(base) / "ResolveSync" if base else Path.home() / ".resolve-sync") / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _key(project: str) -> str:
    return hashlib.sha1(project.encode("utf-8")).hexdigest()[:16]


def cached_path(project: str) -> Path:
    return cache_dir() / f"{_key(project)}.jpg"


def _project_db(project: str) -> Path | None:
    from .autosync import project_db_index
    return project_db_index().get(project)


def source_clip(project: str) -> Path | None:
    """A media file this project uses that exists on this machine right now."""
    db = _project_db(project)
    if db is None:
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        # Scan generously: the first clips a project references are often on a
        # drive that has since moved, or are macOS paths from a Mac edit. A
        # wider scan is one cheap SQLite query and finds far more posters.
        rows = con.execute(
            "SELECT DISTINCT MediaFilePath FROM Sm2TiItem "
            "WHERE MediaFilePath IS NOT NULL LIMIT 800"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    candidates = [Path(r[0]) for r in rows if r[0]]
    for p in candidates:
        if p.suffix.lower() in _VIDEO_EXT and p.is_file():
            return p

    # Nothing at its recorded path. The media may simply have moved — reuse the
    # same relink logic the app already trusts: look for the filename under the
    # user's configured media roots.
    return _find_under_media_roots(candidates)


def _find_under_media_roots(candidates: list[Path]) -> Path | None:
    from .config import Config

    roots = [r for r in Config.load().roots if r.exists()]
    if not roots:
        return None
    wanted = [c.name.lower() for c in candidates
              if c.suffix.lower() in _VIDEO_EXT][:40]
    if not wanted:
        return None
    wanted_set = set(wanted)
    for root in roots:
        try:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.lower() in wanted_set:
                        return Path(dirpath) / fn
        except OSError:
            continue
    return None


def _extract(src: Path, dest: Path) -> bool:
    """One frame -> JPEG. Returns False on any failure; never raises."""
    tmp = dest.with_suffix(".part.jpg")
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", str(SEEK_SECONDS),      # before -i: fast keyframe seek
        "-i", str(src),
        "-frames:v", "1",
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-q:v", "4",
        str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=25,
                              stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        tmp.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        # Clip shorter than the seek point: retry from the very start.
        if SEEK_SECONDS:
            cmd[cmd.index("-ss") + 1] = "0"
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=25,
                                      stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                tmp.unlink(missing_ok=True)
                return False
            if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                return False
        else:
            return False
    os.replace(tmp, dest)              # atomic: readers never see a partial file
    return True


def get(project: str) -> Path | None:
    """Cached thumbnail for a project, generating it if needed."""
    dest = cached_path(project)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if not have_ffmpeg():
        return None
    with _lock:
        if project in _inflight:
            return None                # another request is already making it
        _inflight.add(project)
    try:
        src = source_clip(project)
        if src is None:
            return None
        return dest if _extract(src, dest) else None
    finally:
        with _lock:
            _inflight.discard(project)


def clear(project: str | None = None) -> None:
    if project is None:
        for f in cache_dir().glob("*.jpg"):
            f.unlink(missing_ok=True)
    else:
        cached_path(project).unlink(missing_ok=True)

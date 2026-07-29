"""Export/import Resolve projects as `.drp`, and collect their media fingerprints.

Uses the official ProjectManager API so every snapshot is a *consistent* export
Resolve produced itself — no reaching into its database, no corruption risk.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .media import MediaFingerprint, fingerprint_path
from .resolve_conn import ResolveUnavailable, get_resolve

# Resolve exposes ONE global session: a single current project and a single
# folder cursor. Several of our operations are read-then-act pairs against that
# shared state — export_project navigates to a folder and THEN exports; a
# concurrent walk_projects() calls GotoRootFolder() and resets the cursor
# between those two calls, so the export silently produces a DIFFERENT project's
# content and still reports success.
#
# Every touch of the scripting API must therefore happen under this lock.
#
# LOCK ORDER (deadlock avoidance): RESOLVE_LOCK is the OUTERMOST lock and is
# never held while calling into a store backend. Acquire it, finish the Resolve
# work, release it, and only then write to the store — which takes its own
# per-workdir lock. Never nest the two.
RESOLVE_LOCK = threading.RLock()


def _pm():
    pm = get_resolve().GetProjectManager()
    if pm is None:
        raise ResolveUnavailable(
            "Resolve's project manager is unavailable — Resolve may be closing "
            "or busy. Try again in a moment."
        )
    return pm


def current_project_name() -> str | None:
    proj = _pm().GetCurrentProject()
    return proj.GetName() if proj else None


def current_project_name_locked() -> str | None:
    """The open project's name, read under the session lock."""
    with RESOLVE_LOCK:
        proj = _pm().GetCurrentProject()
        return proj.GetName() if proj else None


def walk_projects() -> list[tuple[str, str]]:
    """Every project in the database as (folder_path, name), folders included.

    GetProjectListInCurrentFolder() only sees ONE folder. Resolve users routinely
    organise projects into subfolders, so listing just the root silently hides
    them — and a project you cannot see is a project you cannot sync.
    """
    with RESOLVE_LOCK:
        return _walk_projects_locked()


def _walk_projects_locked() -> list[tuple[str, str]]:
    pm = _pm()
    pm.GotoRootFolder()
    found: list[tuple[str, str]] = []

    def walk(path: str) -> None:
        for name in pm.GetProjectListInCurrentFolder() or []:
            found.append((path, name))
        for sub in pm.GetFolderListInCurrentFolder() or []:
            if pm.OpenFolder(sub):
                walk(f"{path}/{sub}" if path else sub)
                pm.GotoParentFolder()

    walk("")
    pm.GotoRootFolder()
    return found


def list_projects() -> list[str]:
    """All project names across every folder in the current database."""
    seen: list[str] = []
    for _folder, name in walk_projects():
        if name not in seen:
            seen.append(name)
    return seen


def folder_of(name: str) -> str | None:
    """Which folder holds `name`, or None if it isn't in the database."""
    for folder, project in walk_projects():
        if project == name:
            return folder
    return None


def goto_folder(path: str) -> None:
    """Navigate the ProjectManager to a '/'-separated folder path."""
    with RESOLVE_LOCK:
        _goto_folder_locked(path)


def _goto_folder_locked(path: str) -> None:
    pm = _pm()
    pm.GotoRootFolder()
    for part in [p for p in path.split("/") if p]:
        pm.OpenFolder(part)


def export_project(name: str, dest: Path) -> Path:
    """Export project `name` to a `.drp` at `dest` (stills + LUTs included)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with RESOLVE_LOCK:
        return _export_project_locked(name, dest)


def _export_project_locked(name: str, dest: Path) -> Path:
    # An unsaved project exists only in memory — GetCurrentProject() reports the
    # name you typed, but it is not in the database, so ExportProject cannot see
    # it. Catch that here; the generic failure message sends people looking for
    # permissions problems that don't exist.
    matches = [f for f, p in _walk_projects_locked() if p == name]
    folder = matches[0] if matches else None
    if len(matches) > 1:
        # Two projects share this name in different folders. Exporting would be
        # a coin flip, and both would share one parent pointer in SyncState.
        raise ResolveUnavailable(
            f"More than one project is called '{name}' (in: "
            + ", ".join(m or "/" for m in matches) +
            "). Rename one in Resolve so syncing can tell them apart."
        )
    if folder is None:
        raise ResolveUnavailable(
            f"'{name}' has not been saved to the database yet, so Resolve cannot "
            "export it. In Resolve press Ctrl+S (or File > Save Project), then push again."
        )

    # ExportProject resolves names against the CURRENT folder, so move there
    # first or a project living in a subfolder fails to export.
    _goto_folder_locked(folder)
    ok = _pm().ExportProject(name, str(dest), True)
    if not ok or not dest.is_file() or dest.stat().st_size == 0:
        # Distinguish "this project won't export" from "Resolve won't export
        # anything right now". Reads keep working when the session is blocked, so
        # a project-specific message sends people hunting the wrong problem — the
        # usual cause is a dialog open in Resolve waiting for a click.
        blocked = _export_blocked_globally(dest.parent)
        if blocked:
            raise ResolveUnavailable(
                "Resolve is not accepting export requests at the moment. This "
                "usually means a dialog is open in Resolve waiting for you (for "
                "example a save, render or media-relink prompt). Switch to "
                "Resolve, clear any dialog, then try again. If nothing is open, "
                "restart Resolve."
            )
        raise ResolveUnavailable(
            f"Resolve could not export '{name}'. It may be open on another "
            "machine, or in a different database than the one Resolve has active."
        )
    return dest


def _export_blocked_globally(tmp_dir: Path) -> bool:
    """True when Resolve refuses to export ANY project, not just this one."""
    try:
        pm = _pm()
        pm.GotoRootFolder()
        for candidate in (pm.GetProjectListInCurrentFolder() or [])[:2]:
            probe = tmp_dir / ".rs-export-probe.drp"
            try:
                if pm.ExportProject(candidate, str(probe), False) and probe.is_file():
                    return False          # something exported: not a global block
            finally:
                probe.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def import_project(drp: Path, as_name: str | None = None):
    """Import a `.drp` and return the live project object (loaded into Resolve)."""
    with RESOLVE_LOCK:
        return _import_project_locked(drp, as_name)


def _import_project_locked(drp: Path, as_name: str | None = None):
    pm = _pm()
    if as_name:
        proj = pm.ImportProject(str(drp), as_name)
    else:
        proj = pm.ImportProject(str(drp))
    if not proj:
        raise ResolveUnavailable(f"ImportProject failed for '{drp}'.")
    # Load it so its media pool is addressable for relinking.
    loaded_name = as_name or drp.stem
    pm.LoadProject(loaded_name)
    return pm.GetCurrentProject()


def collect_fingerprints(project) -> list[MediaFingerprint]:
    """Fingerprint every media file referenced by `project` (currently loaded)."""
    with RESOLVE_LOCK:
        return _collect_fingerprints_locked(project)


def _collect_fingerprints_locked(project) -> list[MediaFingerprint]:
    media_pool = project.GetMediaPool()
    fingerprints: list[MediaFingerprint] = []
    root = media_pool.GetRootFolder()

    def walk(folder):
        for clip in folder.GetClipList() or []:
            path = clip.GetClipProperty("File Path")
            name = clip.GetName() or (Path(path).name if path else "clip")
            if path:
                fp = fingerprint_path(name, path)
                if fp:
                    fingerprints.append(fp)
        for sub in folder.GetSubFolderList() or []:
            walk(sub)

    if root:
        walk(root)
    return fingerprints


def media_roots_from_fingerprints(fps: list[MediaFingerprint]) -> list[str]:
    """Best-effort common roots of the referenced media, for Level 1 remap.

    We record each distinct top few path segments so the receiver can strip a
    sender prefix. Simplest useful heuristic: the parent directories present.
    """
    roots: set[str] = set()
    for fp in fps:
        p = Path(fp.original_path)
        # record the drive/mount + first couple of segments as a candidate root
        parts = p.parts
        if len(parts) >= 2:
            roots.add(str(Path(*parts[:2])))
        roots.add(str(p.parent))
    return sorted(roots)

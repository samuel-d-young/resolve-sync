"""Background auto-sync: push when you save, and notice when someone else has.

Resolve's scripting API has no "project saved" event, and re-exporting every
project on a timer would be slow and pointless. Instead we watch each project's
own ``Project.db`` on disk — Resolve rewrites it when the project is saved — and
only export the ones that actually changed.

Three jobs share one thread:
  push  — a changed project is exported and pushed, but only after its database
          has been QUIET for a moment, so we never snapshot a half-written save.
  check — compare the store's head against what this machine last synced, so the
          UI can say "Laptop has newer work" without the user hunting for it.
  stage — the fetch half of auto-pull: whatever `check` announces is downloaded
          and checksum-verified into a local staging folder, so the user's
          eventual "get the newer version" is instant instead of a 3–20 s
          backend round-trip. Importing into Resolve always stays a user action.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# A save must be this many seconds old before we touch it. Resolve writes the
# database in bursts; snapshotting mid-burst risks exporting a torn state.
QUIET_PERIOD = 6.0

# How often to ask the store whether ANOTHER machine pushed. This is the only
# part of a tick that touches the network, so it runs on its own, slower clock
# than the local save-watch (which is just file stats).
INCOMING_INTERVAL = 300.0


def _database_roots() -> list[Path]:
    """Disk-database roots, reusing the dblist.conf parser from detect."""
    from .detect import _dblist_paths
    return _dblist_paths()


# Walking every project folder costs ~80ms on a real library (164 projects) and
# the result barely changes, so cache it. Keyed by the database roots so tests
# (and users) pointing at different databases never see each other's cache.
_INDEX_TTL = 300.0
_index_cache: dict[tuple, tuple[float, dict[str, Path]]] = {}


def project_db_index(refresh: bool = False) -> dict[str, Path]:
    """Map project name -> its Project.db.

    The folder holding Project.db is named exactly after the project, which is
    what makes cheap per-project change detection possible. mtimes are NOT
    cached — callers stat the paths — so a stale cache can never hide a save;
    it can only briefly hide a brand-new project, which the miss-triggered
    refresh below covers.
    """
    roots = _database_roots()
    key = tuple(str(r) for r in roots)
    now = time.time()
    if not refresh:
        hit = _index_cache.get(key)
        if hit and now - hit[0] < _INDEX_TTL:
            return hit[1]
    index: dict[str, Path] = {}
    for root in roots:
        try:
            for db in root.rglob("Project.db"):
                index.setdefault(db.parent.name, db)
        except OSError:
            continue
    _index_cache[key] = (now, index)
    return index


def _index_for(names: list[str]) -> dict[str, Path]:
    """Cached index, force-refreshed once if a watched project is missing."""
    index = project_db_index()
    if any(n not in index for n in names):
        index = project_db_index(refresh=True)
    return index


def stage_head(store, name: str, parent: str | None, stage_dir: Path) -> dict | None:
    """Download + verify the store's newest version of `name` into `stage_dir`.

    The fetch half of auto-pull. It talks ONLY to the store — Resolve is never
    touched, and nothing is imported until the user explicitly pulls — so it is
    safe on the background thread (the same promise rule 3 makes for pushes).

    Returns the staged-version info (with a local, checksum-verified .drp path)
    or None when there is nothing unambiguous to stage:
      - no versions, or the head is the version this machine already synced
      - two or more heads: a real fork. Staging one side would quietly pick a
        winner; the pull UI must show the fork instead.
      - the payload fails its checksum (still transferring) — retried next cycle
    """
    from .store import _safe, compute_version_id

    tips = store.heads(name)
    if len(tips) != 1:
        return None
    head = tips[0]
    if head.version_id == parent:
        return None
    info = {
        "version_id": head.version_id,
        "author": head.author,
        "created": head.created,
        "message": head.message,
        # The full manifest, so a pull can rebuild the Version (media
        # fingerprints included) without another store round-trip.
        "manifest": head.to_manifest(),
    }
    stage_dir.mkdir(parents=True, exist_ok=True)
    dest = stage_dir / f"{_safe(name)}-{head.version_id}.drp"
    if dest.is_file() and compute_version_id(dest) == head.version_id:
        info["path"] = str(dest)
        return info                    # an earlier cycle already fetched it
    src = store.drp_path(name, head.version_id)  # git/drive verify while materializing
    if not src.is_file() or compute_version_id(src) != head.version_id:
        return None
    tmp = dest.with_suffix(".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)              # rename: a crash never leaves a torn stage file
    if src.parent.name.startswith("resolve-sync-"):
        shutil.rmtree(src.parent, ignore_errors=True)  # git/drive scratch dir
    # One staged file per project: drop superseded versions. Exact-match the
    # name so one project's cleanup can never touch another's staged file.
    stale = re.compile(re.escape(_safe(name)) + r"-[0-9a-f]{16}\.drp")
    for old in stage_dir.iterdir():
        if old != dest and stale.fullmatch(old.name):
            old.unlink(missing_ok=True)
    info["path"] = str(dest)
    return info


@dataclass
class Watcher:
    """Tracks save-times so we can tell 'changed' from 'changed and settled'."""

    seen: dict[str, float] = field(default_factory=dict)     # project -> mtime we've handled
    pending: dict[str, float] = field(default_factory=dict)  # project -> mtime awaiting quiet

    def prime(self, names: list[str], remembered: dict[str, float] | None = None) -> None:
        """Seed state at startup.

        Anything already recorded from a previous run is restored, so a project
        SAVED WHILE THE APP WAS CLOSED is still detected and pushed. Only
        projects we have never seen are marked as already-handled — otherwise a
        first run would try to push the user's entire library at once.
        """
        self.seen.update(remembered or {})
        index = _index_for(names)
        for n in names:
            if n in self.seen:
                continue                      # known project: keep its history
            db = index.get(n)
            if db:
                try:
                    self.seen[n] = db.stat().st_mtime
                except OSError:
                    pass

    def settled_changes(self, names: list[str], now: float | None = None) -> list[str]:
        """Projects saved since we last looked AND quiet for QUIET_PERIOD."""
        now = time.time() if now is None else now
        index = _index_for(names)
        ready: list[str] = []
        for n in names:
            db = index.get(n)
            if not db:
                continue
            try:
                mtime = db.stat().st_mtime
            except OSError:
                continue
            if mtime <= self.seen.get(n, 0.0):
                continue                      # nothing new since last push
            prev = self.pending.get(n)
            if prev is None or mtime > prev:
                self.pending[n] = mtime       # still being written; wait
                continue
            if now - mtime >= QUIET_PERIOD:
                ready.append(n)
        return ready

    def mark_pushed(self, name: str) -> None:
        mtime = self.pending.pop(name, None)
        if mtime is not None:
            self.seen[name] = mtime


class AutoSync:
    """Owns the background thread. Safe to start/stop repeatedly."""

    def __init__(self, cfg, do_push, remote_head, local_parent, log=print,
                 current_project=None, incoming_bulk=None, prefetch=None):
        self.cfg = cfg
        self._do_push = do_push            # (name) -> dict, raises on failure
        self._remote_head = remote_head    # (name) -> version_id | None
        self._local_parent = local_parent  # (name) -> version_id | None
        self._current_project = current_project  # () -> name of the open project
        self._incoming_bulk = incoming_bulk      # (names) -> [names ahead of us]
        self._prefetch = prefetch          # (name) -> staged-version info | None
        self._last_incoming_check = 0.0
        self._log = log
        # Projects that changed but weren't the open project. Auto-sync will NOT
        # load them (that would yank the editor's session away mid-edit), so they
        # wait here and are pushed the next time the user has them open.
        self.deferred: list[str] = []
        self.watcher = Watcher()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Surfaced to the UI.
        self.last_run: float | None = None
        self.last_error: str | None = None
        self.recent: list[dict] = []       # newest-first push log
        self.incoming: list[str] = []      # projects where the store is ahead of us
        self.staged: dict[str, dict] = {}  # project -> pre-fetched head, ready to pull

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.watcher.prime(list(self.cfg.synced_projects), self._load_seen())
        self._thread = threading.Thread(target=self._loop, name="autosync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # --- the loop --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._interval()):
            if not self.cfg.auto_sync:
                continue
            try:
                self._tick()
            except Exception as exc:                      # never kill the thread
                self.last_error = str(exc)
                self._log(f"[autosync] {exc}")

    def _interval(self) -> float:
        return max(15, int(getattr(self.cfg, "auto_sync_interval", 90)))

    def _tick(self) -> None:
        self.last_run = time.time()
        names = list(self.cfg.synced_projects)
        if not names:
            return

        # Which project is the editor actually in? Auto-sync must never load a
        # different one: LoadProject on a live session with unsaved timeline
        # state is exactly where an editor's work gets lost.
        open_project = None
        resolve_ok = True
        if self._current_project is not None:
            try:
                open_project = self._current_project()
            except Exception:  # noqa: BLE001 - Resolve may be busy or closing
                resolve_ok = False    # don't guess: no pushes this tick

        if resolve_ok:
            deferred: list[str] = []
            for name in self.watcher.settled_changes(names):
                if open_project is not None and name != open_project:
                    # Changed, but it isn't the open project. Do NOT steal the
                    # session — leave it pending and push it when the user opens it.
                    deferred.append(name)
                    continue
                try:
                    result = self._do_push(name)
                    self.watcher.mark_pushed(name)
                    self.incoming = [n for n in self.incoming if n != name]
                    self.staged.pop(name, None)  # our push made the staged copy stale
                    self._save_seen()
                    self._record(name, True, str(result.get("version_id", ""))[:8])
                except Exception as exc:
                    # Leave it pending so the next tick retries; a conflict is
                    # expected and must never be auto-forced.
                    self._record(name, False, str(exc)[:120])
            self.deferred = deferred

        # The incoming check + staging below are store-only work: they run even
        # with Resolve closed, so the latest version is already fetched before
        # the editor sits down. Only pushing needs Resolve.

        # Is another machine ahead of us? Network work — runs on its own,
        # slower clock, and via ONE bulk call when the backend supports it.
        now = time.time()
        if now - self._last_incoming_check >= max(self._interval(), INCOMING_INTERVAL):
            self._last_incoming_check = now
            try:
                if self._incoming_bulk is not None:
                    self.incoming = list(self._incoming_bulk(names))
                else:
                    ahead = []
                    for name in names:
                        head = self._remote_head(name)
                        if head and head != self._local_parent(name):
                            ahead.append(name)
                    self.incoming = ahead
                self._stage_incoming()
            except Exception as exc:  # noqa: BLE001 - network trouble is routine
                self.last_error = str(exc)

    def _stage_incoming(self) -> None:
        """Auto-pull, fetch half: pre-download whatever `incoming` announces.

        By the time the user clicks "get the newer version" the bytes are
        already local and verified, so the pull is instant even on the slowest
        backend. Download only — importing into Resolve stays a user action.
        """
        if self._prefetch is None or not getattr(self.cfg, "auto_pull", True):
            return
        self.staged = {n: v for n, v in self.staged.items() if n in self.incoming}
        for name in self.incoming:
            try:
                info = self._prefetch(name)
            except Exception as exc:  # noqa: BLE001 - network trouble is routine
                self.last_error = str(exc)
                continue
            if info:
                self.staged[name] = info
            else:
                self.staged.pop(name, None)   # a fork appeared, or we caught up

    # --- remembering what we already pushed, across restarts ---------------
    def _seen_file(self):
        from .config import CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return CONFIG_DIR / "autosync_seen.json"

    def _load_seen(self) -> dict[str, float]:
        import json
        try:
            return {k: float(v) for k, v in
                    json.loads(self._seen_file().read_text("utf-8")).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_seen(self) -> None:
        import json
        try:
            self._seen_file().write_text(
                json.dumps(self.watcher.seen, indent=2), "utf-8")
        except OSError:
            pass

    def _record(self, project: str, ok: bool, detail: str) -> None:
        self.recent.insert(0, {
            "project": project, "ok": ok, "detail": detail, "at": time.time(),
        })
        del self.recent[20:]

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.cfg.auto_sync),
            "running": self.running,
            "interval": self._interval(),
            "watching": list(self.cfg.synced_projects),
            "last_run": self.last_run,
            "last_error": self.last_error,
            "recent": self.recent[:8],
            "incoming": self.incoming,
            # What is staged, not where: local scratch paths and full manifests
            # stay out of the UI payload.
            "staged": {
                n: {k: v for k, v in info.items()
                    if k in ("version_id", "author", "created", "message")}
                for n, info in self.staged.items()
            },
            "incoming_interval": max(self._interval(), INCOMING_INTERVAL),
            "deferred": self.deferred,
        }

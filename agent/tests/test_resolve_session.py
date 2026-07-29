"""Resolve exposes ONE global session. These tests guard the two ways that bit us.

Resolve has a single current project and a single folder cursor. Both are
read-then-act shared state, so concurrent callers corrupt each other:

  1. export_project() navigates to a folder and THEN exports. A concurrent
     walk_projects() calls GotoRootFolder() in between, so the export produces a
     DIFFERENT project's bytes and still reports success.
  2. Auto-sync calling LoadProject() switches the project the editor is working
     in, mid-edit, with no user action.

A fake ProjectManager models the global cursor faithfully enough to reproduce
both without needing Resolve installed.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve_sync import projects  # noqa: E402
from resolve_sync.autosync import AutoSync  # noqa: E402


class FakePM:
    """Models Resolve's single global folder cursor and current project."""

    def __init__(self, tree: dict[str, dict[str, bytes]], current: str = ""):
        self.tree = tree            # folder path -> {project name: payload}
        self.cursor = ""            # the ONE global folder cursor
        self.current = current      # the ONE open project
        self.loads: list[str] = []
        self.exports: list[tuple[str, str]] = []

    # Navigation sleeps a little so the race window is wide enough to observe
    # reliably; real Resolve RPC calls are far slower than a dict lookup.
    _SLOW = 0.002

    # -- navigation
    def GotoRootFolder(self):
        self.cursor = ""
        time.sleep(self._SLOW)
        return True

    def GotoParentFolder(self):
        self.cursor = self.cursor.rsplit("/", 1)[0] if "/" in self.cursor else ""
        return True

    def OpenFolder(self, name):
        nxt = f"{self.cursor}/{name}" if self.cursor else name
        time.sleep(self._SLOW)
        if nxt in self.tree:
            self.cursor = nxt
            return True
        return False

    def GetProjectListInCurrentFolder(self):
        return list(self.tree.get(self.cursor, {}))

    def GetFolderListInCurrentFolder(self):
        depth = self.cursor.count("/") + 1 if self.cursor else 0
        subs = []
        for path in self.tree:
            if not path:
                continue
            parts = path.split("/")
            if len(parts) == depth + 1 and path.startswith(self.cursor):
                subs.append(parts[-1])
        return subs

    # -- projects
    def GetCurrentProject(self):
        pm = self

        class P:
            def GetName(self_inner):
                return pm.current
        return P() if self.current else None

    def LoadProject(self, name):
        self.loads.append(name)
        self.current = name
        return True

    def ExportProject(self, name, dest, _stills=True):
        payload = self.tree.get(self.cursor, {}).get(name)
        if payload is None:
            return False
        Path(dest).write_bytes(payload)
        self.exports.append((self.cursor, name))
        return True


def _install(pm):
    projects._pm = lambda: pm  # noqa: SLF001


def test_export_is_not_corrupted_by_a_concurrent_listing():
    """The bug: a poll resets the cursor between navigate and export."""
    pm = FakePM({
        "": {"Decoy": b"ROOT-JUNK"},
        "Archive": {"Wedding": b"THE-REAL-PROJECT"},
    }, current="Wedding")
    _install(pm)

    out = Path(__file__).parent / "_export_race.drp"
    stop = threading.Event()

    def poller():                       # simulates GET /api/projects polling
        while not stop.is_set():
            projects.walk_projects()

    threads = [threading.Thread(target=poller, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for _ in range(20):
            # Unserialised, the poller resets the cursor between navigate and
            # export: ExportProject then runs at the ROOT, where "Wedding" does
            # not exist, and raises. Under the lock this never happens.
            projects.export_project("Wedding", out)
            assert out.read_bytes() == b"THE-REAL-PROJECT"
            # And it must have exported from Archive, not wherever the cursor drifted.
            assert pm.exports[-1][0] == "Archive", (
                f"exported from the wrong folder: {pm.exports[-1]}"
            )
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
        out.unlink(missing_ok=True)


def test_duplicate_project_names_are_refused_not_guessed():
    """Same name in two folders: exporting would be a coin flip."""
    pm = FakePM({
        "": {"New Project 1": b"ROOT-COPY"},
        "ANZUK": {"New Project 1": b"ANZUK-COPY"},
    }, current="New Project 1")
    _install(pm)
    out = Path(__file__).parent / "_dup.drp"
    try:
        projects.export_project("New Project 1", out)
    except Exception as exc:  # noqa: BLE001
        assert "more than one project" in str(exc).lower(), exc
    else:
        raise AssertionError("ambiguous project name was silently resolved")
    finally:
        out.unlink(missing_ok=True)


def test_autosync_never_switches_the_editors_open_project():
    """The bug: background push called LoadProject and stole the session."""
    pm = FakePM({"": {"Wedding A": b"A", "Smith Wedding": b"B"}}, current="Wedding A")
    _install(pm)

    pushed: list[str] = []

    class Cfg:
        synced_projects = ["Wedding A", "Smith Wedding"]
        auto_sync = True
        auto_sync_interval = 15

    def do_push(name):
        pm.LoadProject(name)            # what _push_one does for a non-open project
        pushed.append(name)
        return {"version_id": "deadbeefdeadbeef"}

    auto = AutoSync(
        cfg=Cfg(), do_push=do_push,
        remote_head=lambda _n: None, local_parent=lambda _n: None,
        current_project=lambda: pm.current,
    )
    # Both projects look freshly saved.
    auto.watcher.settled_changes = lambda names, now=None: list(names)
    auto._tick()

    assert pm.current == "Wedding A", f"editor was moved to {pm.current!r}"
    assert "Smith Wedding" not in pushed, "auto-sync pushed a project it had to load"
    assert auto.deferred == ["Smith Wedding"], auto.deferred
    assert pushed == ["Wedding A"], pushed


if __name__ == "__main__":
    passed = failed = 0
    for nm, fn in sorted(globals().items()):
        if nm.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {nm}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {nm}: {type(exc).__name__}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

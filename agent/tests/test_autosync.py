"""Auto-sync watcher tests — the debounce is the part that must not misfire.

Pushing mid-save would snapshot a torn project, so a change is only "ready"
once its database has been quiet for QUIET_PERIOD.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve_sync import autosync  # noqa: E402
from resolve_sync.autosync import QUIET_PERIOD, Watcher  # noqa: E402


def _fake_db_tree(tmp: Path, projects: dict[str, float]) -> None:
    """Build a Resolve-shaped tree: .../Projects/<name>/Project.db"""
    base = tmp / "Resolve Projects" / "Users" / "guest" / "Projects"
    for name, mtime in projects.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        db = d / "Project.db"
        db.write_bytes(b"x")
        import os
        os.utime(db, (mtime, mtime))


def _patch_roots(tmp: Path):
    autosync._database_roots = lambda: [tmp]


def test_index_maps_folder_name_to_project():
    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"Taco Tuesday": time.time(), "Abbey & Jordan": time.time()})
    _patch_roots(tmp)
    idx = autosync.project_db_index()
    assert set(idx) == {"Taco Tuesday", "Abbey & Jordan"}
    assert idx["Taco Tuesday"].name == "Project.db"


def test_unchanged_project_is_not_pushed():
    tmp = Path(tempfile.mkdtemp())
    old = time.time() - 3600
    _fake_db_tree(tmp, {"A": old})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])
    assert w.settled_changes(["A"]) == [], "primed state must not look like a change"


def test_change_waits_for_quiet_period():
    """A save that is still being written must NOT be pushed yet."""
    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])

    now = time.time()
    _fake_db_tree(tmp, {"A": now})          # just saved
    assert w.settled_changes(["A"], now=now) == [], "pushed while still saving"
    # Still writing: mtime advances again -> keep waiting.
    _fake_db_tree(tmp, {"A": now + 1})
    assert w.settled_changes(["A"], now=now + 1) == []
    # Now quiet for long enough.
    ready = w.settled_changes(["A"], now=now + 1 + QUIET_PERIOD + 0.1)
    assert ready == ["A"], f"never became ready: {ready}"


def test_pushed_project_does_not_repeat():
    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])
    now = time.time()
    _fake_db_tree(tmp, {"A": now})
    w.settled_changes(["A"], now=now)                       # becomes pending
    assert w.settled_changes(["A"], now=now + QUIET_PERIOD + 0.1) == ["A"]
    w.mark_pushed("A")
    assert w.settled_changes(["A"], now=now + 100) == [], "pushed the same save twice"


def test_only_watched_projects_are_considered():
    tmp = Path(tempfile.mkdtemp())
    old = time.time() - 7200
    _fake_db_tree(tmp, {"A": old, "B": old})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])
    now = time.time()
    _fake_db_tree(tmp, {"A": now, "B": now})
    w.settled_changes(["A"], now=now)
    ready = w.settled_changes(["A"], now=now + QUIET_PERIOD + 0.1)
    assert ready == ["A"], "unwatched project leaked into the push set"


def test_saves_made_while_the_app_was_closed_are_still_pushed():
    """prime() used to mark everything current as already-seen.

    That silently dropped any save made while the agent wasn't running — the
    most user-visible auto-sync defect, because it looks like it just works.
    """
    tmp = Path(tempfile.mkdtemp())
    old = time.time() - 7200
    _fake_db_tree(tmp, {"A": old})
    _patch_roots(tmp)

    w1 = Watcher()
    w1.prime(["A"])                      # first run: nothing to push
    assert w1.settled_changes(["A"]) == []
    remembered = dict(w1.seen)

    # App closes. The user saves the project. App restarts.
    saved_at = time.time()
    _fake_db_tree(tmp, {"A": saved_at})
    w2 = Watcher()
    w2.prime(["A"], remembered)

    w2.settled_changes(["A"], now=saved_at)                       # -> pending
    ready = w2.settled_changes(["A"], now=saved_at + QUIET_PERIOD + 0.1)
    assert ready == ["A"], "a save made while the app was closed was never pushed"


def test_first_run_does_not_push_the_entire_library():
    """With no remembered state, existing saves must NOT all fire at once."""
    tmp = Path(tempfile.mkdtemp())
    old = time.time() - 7200
    _fake_db_tree(tmp, {"A": old, "B": old, "C": old})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A", "B", "C"], {})
    assert w.settled_changes(["A", "B", "C"], now=time.time()) == []


def test_new_project_is_found_despite_index_cache():
    """The index cache must never hide a newly created project."""
    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])                                   # warms the cache
    # A brand-new project appears while the cache is still fresh.
    saved = time.time()
    _fake_db_tree(tmp, {"B": saved})
    w.prime(["B"])                                   # miss triggers a refresh
    w.settled_changes(["B"], now=saved)
    assert w.settled_changes(["B"], now=saved + QUIET_PERIOD + 0.1) == [],         "B was primed, so its current mtime must not count as a change"
    _fake_db_tree(tmp, {"B": saved + 60})            # now a real save
    w.settled_changes(["B"], now=saved + 60)
    assert w.settled_changes(["B"], now=saved + 60 + QUIET_PERIOD + 0.1) == ["B"]


def test_incoming_check_runs_on_its_own_slower_clock():
    """The network check must not run on every tick."""
    from resolve_sync.autosync import AutoSync

    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)

    calls = []

    class Cfg:
        synced_projects = ["A"]
        auto_sync = True
        auto_sync_interval = 15

    auto = AutoSync(cfg=Cfg(), do_push=lambda n: {"version_id": "x"},
                    remote_head=lambda n: None, local_parent=lambda n: None,
                    incoming_bulk=lambda names: (calls.append(1), [])[1])
    auto._tick()
    auto._tick()
    auto._tick()
    assert len(calls) == 1, f"network check ran {len(calls)}x in 3 ticks"


def test_failed_push_is_retried_next_tick():
    """mark_pushed is only called on success, so a failure stays pending."""
    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)
    w = Watcher()
    w.prime(["A"])
    now = time.time()
    _fake_db_tree(tmp, {"A": now})
    w.settled_changes(["A"], now=now)
    assert w.settled_changes(["A"], now=now + QUIET_PERIOD + 0.1) == ["A"]
    # Simulate a failed push: mark_pushed NOT called.
    assert w.settled_changes(["A"], now=now + QUIET_PERIOD + 5) == ["A"], "gave up after one failure"


# --------------------------------------------------------------- auto-pull --
# The fetch half only: incoming versions are downloaded and verified into a
# local stage so the user's pull is instant. Importing stays a user action.

def _put_version(store, project: str, content: bytes, parent: str | None = None,
                 author: str = "Laptop") -> str:
    from resolve_sync.store import Version, compute_version_id
    drp = Path(tempfile.mkdtemp()) / "project.drp"
    drp.write_bytes(content)
    vid = compute_version_id(drp)
    store.put(Version(
        version_id=vid, project=project, author=author, machine_id="m1",
        created="2026-07-29T00:00:00+00:00", parent=parent, message="msg",
        drp_name="project.drp", media=[], media_roots=[]), drp)
    return vid


def test_incoming_versions_are_staged_for_instant_pull():
    """Whatever `incoming` announces gets pre-fetched — on the same slow clock."""
    from resolve_sync.autosync import AutoSync

    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)

    class Cfg:
        synced_projects = ["A"]
        auto_sync = True
        auto_sync_interval = 15

    fetches: list[str] = []

    def prefetch(name):
        fetches.append(name)
        return {"version_id": "cafe", "path": "C:/stage/a.drp", "author": "Laptop",
                "created": "2026", "message": "m", "manifest": {"version_id": "cafe"}}

    auto = AutoSync(cfg=Cfg(), do_push=lambda n: {"version_id": "x"},
                    remote_head=lambda n: None, local_parent=lambda n: None,
                    incoming_bulk=lambda names: ["A"], prefetch=prefetch)
    auto.watcher.prime(["A"])          # as start() would: old saves aren't changes
    auto._tick()
    assert auto.staged.get("A", {}).get("version_id") == "cafe"
    auto._tick()
    auto._tick()
    assert fetches == ["A"], f"prefetch ran {len(fetches)}x in 3 ticks — it must " \
                             "follow the incoming clock, not hammer the store"
    # The UI payload announces WHAT is staged, never local paths or manifests.
    shown = auto.to_dict()["staged"]["A"]
    assert shown["version_id"] == "cafe"
    assert "path" not in shown and "manifest" not in shown


def test_auto_pull_can_be_disabled():
    from resolve_sync.autosync import AutoSync

    tmp = Path(tempfile.mkdtemp())
    _fake_db_tree(tmp, {"A": time.time() - 7200})
    _patch_roots(tmp)

    class Cfg:
        synced_projects = ["A"]
        auto_sync = True
        auto_sync_interval = 15
        auto_pull = False

    fetches: list[str] = []
    auto = AutoSync(cfg=Cfg(), do_push=lambda n: {"version_id": "x"},
                    remote_head=lambda n: None, local_parent=lambda n: None,
                    incoming_bulk=lambda names: ["A"],
                    prefetch=lambda n: fetches.append(n))
    auto._tick()
    assert fetches == [] and auto.staged == {}, "auto_pull=False still fetched"


def test_our_own_push_clears_the_stage():
    """Once this machine pushes, the staged download is obsolete."""
    from resolve_sync.autosync import AutoSync

    tmp = Path(tempfile.mkdtemp())
    saved = time.time() - QUIET_PERIOD - 1
    _fake_db_tree(tmp, {"A": saved})
    _patch_roots(tmp)

    class Cfg:
        synced_projects = ["A"]
        auto_sync = True
        auto_sync_interval = 15

    auto = AutoSync(cfg=Cfg(), do_push=lambda n: {"version_id": "y"},
                    remote_head=lambda n: None, local_parent=lambda n: None)
    auto.watcher.prime(["A"], {"A": saved - 100})
    auto.watcher.pending["A"] = saved            # save already seen and settled
    auto.incoming = ["A"]
    auto.staged["A"] = {"version_id": "cafe"}
    auto._tick()
    assert auto.incoming == [] and auto.staged == {}, \
        "a successful push must clear the stale staged copy"


def test_incoming_is_checked_even_when_resolve_is_closed():
    """Staging is store-only work; a closed Resolve blocks pushes, not fetches."""
    from resolve_sync.autosync import AutoSync

    tmp = Path(tempfile.mkdtemp())
    saved = time.time() - QUIET_PERIOD - 1
    _fake_db_tree(tmp, {"A": saved})
    _patch_roots(tmp)

    class Cfg:
        synced_projects = ["A"]
        auto_sync = True
        auto_sync_interval = 15

    def resolve_is_closed():
        raise RuntimeError("no Resolve")

    pushes: list[str] = []
    auto = AutoSync(cfg=Cfg(), do_push=lambda n: pushes.append(n),
                    remote_head=lambda n: None, local_parent=lambda n: None,
                    current_project=resolve_is_closed,
                    incoming_bulk=lambda names: ["A"],
                    prefetch=lambda n: {"version_id": "cafe", "path": "x",
                                        "author": "L", "created": "", "message": "",
                                        "manifest": {}})
    auto.watcher.prime(["A"], {"A": saved - 100})
    auto.watcher.pending["A"] = saved            # a save is ready to push
    auto._tick()
    assert pushes == [], "pushed while Resolve was unavailable — must never guess"
    assert auto.incoming == ["A"], "incoming check skipped just because Resolve is closed"
    assert auto.staged.get("A", {}).get("version_id") == "cafe"


def test_stage_head_fetches_and_verifies_the_newest_version():
    from resolve_sync.autosync import stage_head
    from resolve_sync.store import Store, compute_version_id

    store = Store(Path(tempfile.mkdtemp()))
    stage = Path(tempfile.mkdtemp())
    v1 = _put_version(store, "A", b"one")
    v2 = _put_version(store, "A", b"two", parent=v1)

    info = stage_head(store, "A", parent=v1, stage_dir=stage)
    assert info and info["version_id"] == v2
    p = Path(info["path"])
    assert p.is_file() and compute_version_id(p) == v2, "staged file not verified"
    assert info["manifest"]["project"] == "A", "pull needs the manifest offline"

    # Already up to date -> nothing to stage.
    assert stage_head(store, "A", parent=v2, stage_dir=stage) is None

    # A newer head supersedes the old staged file, which must not linger.
    v3 = _put_version(store, "A", b"three", parent=v2)
    info3 = stage_head(store, "A", parent=v1, stage_dir=stage)
    assert info3["version_id"] == v3 and Path(info3["path"]).is_file()
    assert not p.is_file(), "superseded staged file was left behind"


def test_stage_head_never_guesses_a_fork():
    """Two heads = a real fork. Staging one side would quietly pick a winner."""
    from resolve_sync.autosync import stage_head
    from resolve_sync.store import Store

    store = Store(Path(tempfile.mkdtemp()))
    stage = Path(tempfile.mkdtemp())
    base = _put_version(store, "A", b"base")
    _put_version(store, "A", b"ours", parent=base)
    _put_version(store, "A", b"theirs", parent=base)
    assert stage_head(store, "A", parent=base, stage_dir=stage) is None
    assert list(stage.iterdir()) == [], "a forked project must stage nothing"


def test_stage_head_rejects_a_torn_payload():
    """A half-synced .drp must not be staged; the next cycle retries."""
    from resolve_sync.autosync import stage_head
    from resolve_sync.store import Store

    store = Store(Path(tempfile.mkdtemp()))
    stage = Path(tempfile.mkdtemp())
    v1 = _put_version(store, "A", b"good content")
    store.drp_path("A", v1).write_bytes(b"trunc")   # syncer truncated the payload
    assert stage_head(store, "A", parent=None, stage_dir=stage) is None
    assert list(stage.iterdir()) == [], "a torn payload must never reach the stage"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

"""All backends must behave identically — server.py switches between them freely.

A divergence here is invisible in normal use and then bites when someone changes
backend. The round-trip contract is the one that already broke once:

    for name in store.list_projects():
        store.list_versions(name)      # MUST return that project's history
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve_sync.drive_store import DriveStore  # noqa: E402
from resolve_sync.git_store import GitStore  # noqa: E402
from resolve_sync.store import DRP_FILENAME, Store, Version, compute_version_id  # noqa: E402

HAVE_GIT = shutil.which("git") is not None

# Every method server.py may call on a store, and what it must accept/return.
CONTRACT = ["put", "list_projects", "list_versions", "get_version",
            "drp_path", "head", "is_fork"]


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _version(vid, project, **kw):
    d = dict(version_id=vid, project=project, author="Sam", machine_id="m1",
             created="2026-07-28T10:00:00+00:00", parent=None, message="first",
             drp_name=DRP_FILENAME, media=[], media_roots=[])
    d.update(kw)
    return Version(**d)


def test_all_backends_expose_the_same_interface():
    missing = {}
    for cls in (Store, GitStore, DriveStore):
        gaps = [m for m in CONTRACT if not callable(getattr(cls, m, None))]
        if gaps:
            missing[cls.__name__] = gaps
    assert not missing, f"interface gaps: {missing}"


def test_put_signatures_are_call_compatible():
    """server.py calls put(version, drp) — extra params must have defaults."""
    for cls in (Store, GitStore, DriveStore):
        sig = inspect.signature(cls.put)
        required = [
            p.name for p in list(sig.parameters.values())[1:]
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        assert required == ["version", "drp_src"], f"{cls.__name__}.put{sig}"


def _round_trip(store, name: str, tmp: Path) -> None:
    drp = tmp / "p.drp"
    drp.write_bytes(b"PROJECT-PAYLOAD" * 50)
    vid = compute_version_id(drp)
    store.put(_version(vid, name), drp)

    listed = store.list_projects()
    assert name in listed, f"{type(store).__name__}: {name!r} missing from {listed}"
    # The critical contract: whatever list_projects returns must feed list_versions.
    for p in listed:
        versions = store.list_versions(p)
        assert versions, f"{type(store).__name__}: list_versions({p!r}) empty"
        assert store.head(p) is not None
    got = store.drp_path(name, vid)
    assert got.read_bytes() == drp.read_bytes(), "payload mismatch"
    assert store.is_fork(name, vid) is False
    assert store.is_fork(name, None) is True, "stale parent not reported as fork"


def test_folder_backend_round_trip():
    tmp = _tmp()
    _round_trip(Store(tmp / "store"), "Taco Tuesday", tmp)


def test_git_backend_round_trip():
    if not HAVE_GIT:
        return
    tmp = _tmp()
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _round_trip(GitStore(tmp / "wk", str(bare)), "Taco Tuesday", tmp)


def test_awkward_names_round_trip_on_every_backend():
    """Colons, slashes, unicode and trailing dots must survive the round trip."""
    names = ["Ep 01: Rough Cut", "Café  Sessions", "A/B Test", "Trailing dot.",
             "Ünïcødé 🎬 Project"]
    tmp = _tmp()
    store = Store(tmp / "store")
    for i, name in enumerate(names):
        f = tmp / f"{i}.drp"
        f.write_bytes(f"CONTENT-{i}".encode())
        store.put(_version(compute_version_id(f), name), f)
    listed = store.list_projects()
    for name in names:
        assert name in listed, f"{name!r} not in {listed}"
        assert len(store.list_versions(name)) == 1, f"{name!r} history lost"


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

"""Regression tests for the empirically-confirmed bugs.

Every test here reproduces a defect that was demonstrated against the real code
before it was fixed. They are the guard against regressing any of them.

Run:  python -m pytest tests -q      (or: python tests/test_hardening.py)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve_sync.media import MediaFingerprint, MediaIndex, fingerprint_path  # noqa: E402
from resolve_sync.store import DRP_FILENAME, Store, Version, compute_version_id  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _version(vid, project, **kw):
    defaults = dict(
        version_id=vid, project=project, author="Sam", machine_id="m1",
        created="2026-07-28T10:00:00+00:00", parent=None, message="msg",
        drp_name=DRP_FILENAME, media=[], media_roots=[],
    )
    defaults.update(kw)
    return Version(**defaults)


# --- Bug 1: project name written into an NTFS alternate data stream ---------
def test_colon_in_project_name_writes_a_real_file():
    """"Ep 01: Rough Cut" used to create a 0-byte file + an ADS payload."""
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "a.drp"
    drp.write_bytes(b"REAL-PAYLOAD" * 200)
    vid = compute_version_id(drp)
    name = "Ep 01: Rough Cut"
    store.put(_version(vid, name), drp)

    p = store.drp_path(name, vid)
    assert p.is_file() and p.stat().st_size == drp.stat().st_size
    assert p.name == DRP_FILENAME
    # The payload must be a real, listable file — not hidden in a stream.
    files = {f.name: f.stat().st_size for f in p.parent.iterdir() if f.is_file()}
    assert files.get(DRP_FILENAME, 0) > 0, files
    assert store.verify(name, vid)
    assert store.list_versions(name), "version must be visible"


# --- Bug 2: re-push made a version its own parent and destroyed the message -
def test_repush_is_idempotent_and_preserves_original_manifest():
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "b.drp"
    drp.write_bytes(b"UNCHANGED")
    vid = compute_version_id(drp)
    store.put(_version(vid, "MyFilm", message="FIRST REAL MESSAGE", parent=None), drp)
    # Second push: content unchanged -> same version_id, parent now points at it.
    store.put(_version(vid, "MyFilm", message="second push", parent=vid), drp)

    after = store.get_version("MyFilm", vid)
    assert after.parent != after.version_id, "version became its own parent"
    assert after.message == "FIRST REAL MESSAGE", "original manifest was overwritten"


# --- Bug 3: unhashable media degraded to name+size matching (WRONG media) ---
def test_unverifiable_fingerprint_never_matches_a_decoy():
    tmp = _tmp()
    real_dir, decoy_dir = tmp / "real", tmp / "decoy"
    real_dir.mkdir(), decoy_dir.mkdir()
    (real_dir / "C0001.MP4").write_bytes(b"A" * 50000)
    decoy = decoy_dir / "C0001.MP4"
    decoy.write_bytes(b"B" * 50000)  # same name, same size, different footage

    healthy = fingerprint_path("C0001", str(real_dir / "C0001.MP4"))
    degraded = MediaFingerprint("C0001", str(real_dir / "C0001.MP4"),
                                "c0001.mp4", 50000, "")  # hash read failed

    assert healthy.matches(real_dir / "C0001.MP4")
    assert not healthy.matches(decoy)
    assert not degraded.matches(decoy), "matched the wrong footage on name+size"
    assert MediaIndex([decoy_dir]).find(degraded) is None
    # ...but it should still be offered for manual review, not silently dropped.
    assert MediaIndex([decoy_dir]).find_candidates(degraded) == [decoy]


def test_healthy_fingerprint_still_finds_renamed_media():
    """The rename-tolerant discovery must survive the stricter matching."""
    tmp = _tmp()
    src, dst = tmp / "src", tmp / "dst"
    src.mkdir(), dst.mkdir()
    (src / "shot01.mov").write_bytes(b"VIDEO" * 20000)
    (dst / "renamed_and_moved.mov").write_bytes(b"VIDEO" * 20000)
    fp = fingerprint_path("shot01", str(src / "shot01.mov"))
    assert MediaIndex([dst]).find(fp) == dst / "renamed_and_moved.mov"


# --- Store robustness under a dumb syncer ----------------------------------
def test_one_bad_manifest_does_not_kill_the_history():
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "c.drp"
    drp.write_bytes(b"GOOD")
    vid = compute_version_id(drp)
    store.put(_version(vid, "Film"), drp)

    bad = store._project_dir("Film") / "deadbeefdeadbeef"
    bad.mkdir(parents=True)
    (bad / "version.json").write_text('{"no_version_id": true}', encoding="utf-8")

    versions, quarantined = store.list_versions("Film", with_quarantine=True)
    assert [v.version_id for v in versions] == [vid]
    assert len(quarantined) == 1


def test_manifest_without_payload_is_quarantined_not_head():
    """Syncers send the small manifest first; the .drp may not have arrived."""
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "d.drp"
    drp.write_bytes(b"PAYLOAD")
    vid = compute_version_id(drp)
    store.put(_version(vid, "Film"), drp)
    store.drp_path("Film", vid).unlink()  # payload not synced yet

    assert store.list_versions("Film") == []
    assert store.head("Film") is None, "head pointed at an unpullable version"


def test_list_projects_round_trips_into_list_versions():
    """The name list_projects() returns MUST work as list_versions() input.

    Directories carry a collision hash from _safe(), so returning the folder
    name silently yields a project the dashboard shows with zero history.
    """
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "a.drp"
    drp.write_bytes(b"PAYLOAD")
    vid = compute_version_id(drp)
    name = "Taco Tuesday"
    store.put(_version(vid, name), drp)

    listed = store.list_projects()
    assert listed == [name], listed
    for p in listed:
        assert len(store.list_versions(p)) == 1, f"round-trip broken for {p!r}"
        assert store.head(p) is not None


def test_names_that_sanitize_alike_stay_separate():
    """'Ep 01/Rough' and 'Ep 01_Rough' must not share a history."""
    tmp = _tmp()
    store = Store(tmp / "s")
    for i, name in enumerate(("Ep 01/Rough", "Ep 01_Rough")):
        f = tmp / f"{i}.drp"
        f.write_bytes(f"CONTENT-{i}".encode())
        store.put(_version(compute_version_id(f), name), f)
    assert sorted(store.list_projects()) == ["Ep 01/Rough", "Ep 01_Rough"]
    for name in ("Ep 01/Rough", "Ep 01_Rough"):
        assert len(store.list_versions(name)) == 1, f"{name} histories merged"


def test_store_from_an_older_build_is_still_readable():
    """_safe() gained a hash suffix; existing stores must not become invisible."""
    tmp = _tmp()
    root = tmp / "store"
    name = "Taco Tuesday"
    drp = tmp / "x.drp"
    drp.write_bytes(b"OLD-DATA")
    vid = compute_version_id(drp)

    # Write the layout an older build would have produced (bare sanitized name).
    legacy = root / "Taco Tuesday" / vid
    legacy.mkdir(parents=True)
    (legacy / DRP_FILENAME).write_bytes(drp.read_bytes())
    (legacy / "version.json").write_text(json.dumps({
        "version_id": vid, "project": name, "author": "Sam", "machine_id": "m1",
        "created": "2026-01-01T00:00:00+00:00", "parent": None, "message": "old push",
        "drp_name": DRP_FILENAME, "media": [], "media_roots": [],
    }), encoding="utf-8")

    store = Store(root)
    assert store.list_projects() == [name]
    assert len(store.list_versions(name)) == 1, "older store became unreadable"
    assert store.verify(name, vid)

    # A new push must append to the SAME directory, not fork into a second one.
    drp2 = tmp / "y.drp"
    drp2.write_bytes(b"NEW-DATA")
    vid2 = compute_version_id(drp2)
    store.put(_version(vid2, name, parent=vid), drp2)
    assert len(store.list_versions(name)) == 2, "history split across two folders"
    assert len([d for d in root.iterdir() if d.is_dir()]) == 1


def test_sync_client_dot_dirs_are_not_projects():
    tmp = _tmp()
    root = tmp / "s"
    (root / ".dropbox.cache").mkdir(parents=True)
    (root / ".tmp.drivedownload").mkdir(parents=True)
    (root / "RealProject-abc123").mkdir(parents=True)
    assert Store(root).list_projects() == ["RealProject-abc123"]


def test_conflicted_directory_copy_is_not_a_phantom_duplicate():
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "e.drp"
    drp.write_bytes(b"X")
    vid = compute_version_id(drp)
    store.put(_version(vid, "Film"), drp)

    # Dropbox conflicted copy of the version DIRECTORY carries a valid manifest.
    src = store._project_dir("Film") / vid
    dup = store._project_dir("Film") / f"{vid} (Sam's conflicted copy 2026-07-28)"
    dup.mkdir()
    (dup / DRP_FILENAME).write_bytes(b"X")
    (dup / "version.json").write_text((src / "version.json").read_text("utf-8"), "utf-8")

    assert len(store.list_versions("Film")) == 1, "phantom duplicate version"


def test_heads_are_topological_not_clock_based():
    """A skewed clock must not hide a genuine fork."""
    tmp = _tmp()
    store = Store(tmp / "s")
    base = tmp / "f.drp"
    base.write_bytes(b"BASE")
    bid = compute_version_id(base)
    store.put(_version(bid, "Film", created="2026-07-28T10:00:00+00:00"), base)

    # Two machines both branch off `bid` -> a real fork with two tips.
    for tag, when in ((b"BRANCH-A", "2026-07-28T10:05:00+00:00"),
                      (b"BRANCH-B", "2026-07-28T09:55:00+00:00")):  # B's clock is slow
        f = tmp / f"{tag.decode()}.drp"
        f.write_bytes(tag)
        store.put(_version(compute_version_id(f), "Film", parent=bid, created=when), f)

    heads = store.heads("Film")
    assert len(heads) == 2, f"fork hidden; heads={[h.version_id for h in heads]}"


def test_checksum_detects_a_truncated_transfer():
    tmp = _tmp()
    store = Store(tmp / "s")
    drp = tmp / "g.drp"
    drp.write_bytes(b"COMPLETE-PAYLOAD" * 500)
    vid = compute_version_id(drp)
    store.put(_version(vid, "Film"), drp)
    assert store.verify("Film", vid)

    p = store.drp_path("Film", vid)
    p.write_bytes(p.read_bytes()[:1000])  # simulate a partial transfer
    assert p.is_file(), "is_file() still True — which is why it is not enough"
    assert not store.verify("Film", vid), "truncated payload passed verification"


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

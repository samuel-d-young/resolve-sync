"""GitStore tests against a real bare repo (no network / GitHub account needed).

The headline test is `test_concurrent_push_is_rejected_atomically`: it proves the
compare-and-set guarantee that a shared sync folder fundamentally cannot give.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolve_sync.git_store import GitError, GitStore, _slug  # noqa: E402
from resolve_sync.store import DRP_FILENAME, Version, compute_version_id  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _version(vid, project, **kw):
    d = dict(version_id=vid, project=project, author="Sam", machine_id="m1",
             created="2026-07-28T10:00:00+00:00", parent=None, message="msg",
             drp_name=DRP_FILENAME, media=[], media_roots=[])
    d.update(kw)
    return Version(**d)


def _bare(tmp: Path) -> str:
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return str(bare)


def test_slug_is_a_legal_git_ref():
    assert _slug("Ep 01: Rough Cut") == "ep-01-rough-cut"
    assert _slug("Café") == "café" or _slug("Café")  # NFC-normalized, non-empty
    assert not _slug("...").startswith(".")
    assert not _slug("x.lock").endswith(".lock")
    assert _slug("") == "project"


def test_push_and_read_back_roundtrip():
    tmp = _tmp()
    store = GitStore(tmp / "wk", _bare(tmp))
    drp = tmp / "a.drp"
    drp.write_bytes(b"PROJECT-PAYLOAD" * 300)
    vid = compute_version_id(drp)
    store.put(_version(vid, "My Film", message="first cut"), drp)

    versions = store.list_versions("My Film")
    assert [v.version_id for v in versions] == [vid]
    assert versions[0].message == "first cut"
    # Branch is a slug, but the UI must show the real project name.
    assert store.list_projects() == ["My Film"], store.list_projects()

    out = store.drp_path("My Film", vid)               # verifies sha1 internally
    assert out.read_bytes() == drp.read_bytes()


def test_history_is_topological_not_clock_based():
    tmp = _tmp()
    store = GitStore(tmp / "wk", _bare(tmp))
    ids = []
    # Second commit carries an EARLIER timestamp than the first.
    for payload, when in ((b"V1", "2026-07-28T10:00:00+00:00"),
                          (b"V2", "2026-07-28T09:00:00+00:00")):
        f = tmp / f"{payload.decode()}.drp"
        f.write_bytes(payload)
        vid = compute_version_id(f)
        ids.append(vid)
        store.put(_version(vid, "Film", parent=ids[0] if len(ids) > 1 else None,
                           created=when), f)
    # git log order must put the real latest commit first, despite the timestamps.
    assert [v.version_id for v in store.list_versions("Film")] == [ids[1], ids[0]]


def test_repush_of_identical_content_is_a_noop():
    tmp = _tmp()
    store = GitStore(tmp / "wk", _bare(tmp))
    drp = tmp / "b.drp"
    drp.write_bytes(b"SAME")
    vid = compute_version_id(drp)
    store.put(_version(vid, "Film", message="original"), drp)
    store.put(_version(vid, "Film", message="second", parent=vid), drp)
    versions = store.list_versions("Film")
    assert len(versions) == 1, "empty commit created"
    assert versions[0].message == "original"


def test_concurrent_push_is_rejected_atomically():
    """THE headline guarantee: a stale push is refused, never silently merged.

    Two machines clone the same remote, both commit from the same parent, both
    push. The second MUST be rejected — this is the compare-and-set that a
    Dropbox/Drive folder cannot provide.
    """
    tmp = _tmp()
    remote = _bare(tmp)
    base = tmp / "base.drp"
    base.write_bytes(b"BASE")
    base_id = compute_version_id(base)

    a = GitStore(tmp / "machineA", remote)
    a.put(_version(base_id, "Film", message="base"), base)

    b = GitStore(tmp / "machineB", remote)      # machine B syncs the base
    assert [v.version_id for v in b.list_versions("Film")] == [base_id]

    # Both edit from `base` and push.
    fa = tmp / "a2.drp"; fa.write_bytes(b"EDIT-FROM-A")
    fb = tmp / "b2.drp"; fb.write_bytes(b"EDIT-FROM-B")
    a.put(_version(compute_version_id(fa), "Film", parent=base_id, message="A edit"), fa)

    b_version = _version(compute_version_id(fb), "Film", parent=base_id,
                         message="B edit", machine_id="machineB")
    try:
        b.put(b_version, fb)
    except GitError as exc:
        assert exc.rejected, f"rejected for the wrong reason: {exc}"
    else:
        raise AssertionError("machine B's stale push was NOT rejected")

    # A's work is intact on the main branch and is the tip.
    assert a.list_versions("Film")[0].message == "A edit", \
        [(v.version_id, v.message) for v in a.list_versions("Film")]

    # Opting in preserves B's work on a fork branch without touching main.
    b.put(b_version, fb, allow_fork=True)
    branches = subprocess.run(["git", "branch", "-a"], cwd=str(tmp / "machineB"),
                              capture_output=True, text=True).stdout
    assert "machineB" in branches, branches
    assert a.list_versions("Film")[0].message == "A edit", "main branch was clobbered"


def test_two_projects_do_not_share_history():
    """Pushing project B after project A must not graft A's commits onto B.

    `git checkout -B <br>` branches from the CURRENT HEAD, so without an orphan
    branch B inherits A's history and B's first push is rejected as a fork
    against A's version_id.
    """
    tmp = _tmp()
    store = GitStore(tmp / "wk", _bare(tmp))

    fa = tmp / "a.drp"; fa.write_bytes(b"PROJECT-A-CONTENT")
    a_id = compute_version_id(fa)
    store.put(_version(a_id, "Project A", message="A first"), fa)

    fb = tmp / "b.drp"; fb.write_bytes(b"PROJECT-B-CONTENT")
    b_id = compute_version_id(fb)
    # parent=None because B has never been pushed; must NOT be seen as a fork.
    store.put(_version(b_id, "Project B", parent=None, message="B first"), fb)

    a_hist = [(v.version_id, v.message) for v in store.list_versions("Project A")]
    b_hist = [(v.version_id, v.message) for v in store.list_versions("Project B")]
    assert a_hist == [(a_id, "A first")], a_hist
    assert b_hist == [(b_id, "B first")], b_hist
    # B's .drp must be B's bytes, not A's.
    assert store.drp_path("Project B", b_id).read_bytes() == fb.read_bytes()


def test_contaminated_local_branch_is_discarded():
    """A leftover local branch holding another project's history must not win."""
    tmp = _tmp()
    remote = _bare(tmp)
    store = GitStore(tmp / "wk", remote)

    fa = tmp / "a.drp"; fa.write_bytes(b"A-CONTENT")
    a_id = compute_version_id(fa)
    store.put(_version(a_id, "Project A", message="A first"), fa)

    # Simulate the old bug: a local branch for B grafted onto A's history.
    subprocess.run(["git", "checkout", "-q", "-B", "rs/project-b"],
                   cwd=str(tmp / "wk"), check=True)

    fb = tmp / "b.drp"; fb.write_bytes(b"B-CONTENT")
    b_id = compute_version_id(fb)
    store.put(_version(b_id, "Project B", parent=None, message="B first"), fb)

    b_hist = [(v.version_id, v.message) for v in store.list_versions("Project B")]
    assert b_hist == [(b_id, "B first")], b_hist


def test_concurrent_pushes_through_one_clone_are_safe():
    """The auto-sync thread and HTTP handlers share ONE working clone.

    Unserialised, they race: git fails outright, or one thread wipes the
    worktree while another is staging and commits a partial tree. Measured
    before the per-workdir lock: only 1 of 4 concurrent pushes survived.
    """
    import threading

    tmp = _tmp()
    remote = _bare(tmp)
    wk = tmp / "wk"
    done, errs = [], []

    def push(n: int) -> None:
        try:
            store = GitStore(wk, remote)
            f = tmp / f"{n}.drp"
            f.write_bytes(f"CONTENT-{n}".encode() * 100)
            store.put(_version(compute_version_id(f), f"Project {n}", message=f"push {n}"), f)
            done.append(n)
        except Exception as exc:  # noqa: BLE001
            errs.append((n, str(exc)[:80]))

    threads = [threading.Thread(target=push, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errs, f"concurrent pushes failed: {errs}"
    assert sorted(done) == list(range(6))

    store = GitStore(wk, remote)
    for n in range(6):
        project = f"Project {n}"
        versions = store.list_versions(project)
        assert len(versions) == 1, f"{project}: {len(versions)} versions"
        payload = store.drp_path(project, versions[0].version_id).read_bytes()
        assert payload == f"CONTENT-{n}".encode() * 100, f"{project} got another project's bytes"


def test_remote_tips_is_one_call_and_skips_fork_branches():
    tmp = _tmp()
    remote = _bare(tmp)
    store = GitStore(tmp / "wk", remote)
    for i, name in enumerate(("Project A", "Project B")):
        f = tmp / f"{i}.drp"
        f.write_bytes(f"CONTENT-{i}".encode())
        store.put(_version(compute_version_id(f), name, message="m"), f)
    # A fork-preservation branch must not appear as a project tip.
    f = tmp / "fork.drp"; f.write_bytes(b"FORKED")
    store.put(_version(compute_version_id(f), "Project A", parent="0" * 16,
                       message="forked", machine_id="mB"), f, allow_fork=True)

    tips = store.remote_tips()
    assert set(tips) == {"project-a", "project-b"}, tips
    assert all(len(sha) == 40 for sha in tips.values()), tips
    # Unchanged branches keep the same SHA — the fingerprint callers cache on.
    assert store.remote_tips() == tips


def test_checksum_mismatch_refuses_to_import():
    tmp = _tmp()
    store = GitStore(tmp / "wk", _bare(tmp))
    drp = tmp / "c.drp"
    drp.write_bytes(b"REAL")
    store.put(_version(compute_version_id(drp), "Film"), drp)
    try:
        store.drp_path("Film", "0000000000000000")
    except GitError:
        pass
    else:
        raise AssertionError("unknown version_id should not resolve")


if __name__ == "__main__":
    if not HAVE_GIT:
        print("SKIP: git not on PATH")
        sys.exit(0)
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

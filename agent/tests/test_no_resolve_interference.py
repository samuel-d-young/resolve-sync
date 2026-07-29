"""Polling must never navigate Resolve's Project Manager.

Resolve's folder cursor is GLOBAL: the same cursor drives the Project Manager
window the user is looking at. When /api/overview walked the folder tree on its
12-second poll, the view shifted under the user's cursor while they browsed —
a click could land on a different project, and a project they never chose would
start loading.

The rule this file enforces: answer from the filesystem, never by navigating.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVER = (Path(__file__).resolve().parent.parent / "resolve_sync" / "server.py").read_text("utf-8")


def _poll_endpoint_source(name: str) -> str:
    """Executable source of one endpoint, with comments and docstrings removed.

    Comments are stripped deliberately: the explanations in server.py name the
    banned calls, and a test that trips over prose instead of code is worse than
    no test at all.
    """
    start = SERVER.index(f"def {name}(")
    rest = SERVER[start:]
    for marker in ("\n@app.", "\n# ---"):
        idx = rest.find(marker, 1)
        if idx != -1:
            rest = rest[:idx]

    triple_d = '"' * 3
    triple_s = "'" * 3
    lines, in_doc = [], False
    for line in rest.splitlines():
        stripped = line.strip()
        if in_doc:
            if triple_d in stripped or triple_s in stripped:
                in_doc = False
            continue
        if stripped.startswith(triple_d) or stripped.startswith(triple_s):
            marker_count = stripped.count(triple_d) + stripped.count(triple_s)
            if marker_count == 1:
                in_doc = True
            continue
        code = line.split("#", 1)[0]
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


def test_overview_never_navigates_resolve():
    src = _poll_endpoint_source("overview")
    for banned in ("walk_projects", "GotoRootFolder", "OpenFolder", "goto_folder"):
        assert banned not in src, (
            f"/api/overview calls {banned}() — that moves Resolve's global folder "
            "cursor on every poll and disturbs the Project Manager window."
        )


def test_projects_endpoint_never_navigates_resolve():
    src = _poll_endpoint_source("get_projects")
    for banned in ("walk_projects", "GotoRootFolder", "OpenFolder"):
        assert banned not in src, f"/api/projects calls {banned}()"


def test_overview_reads_folders_from_disk():
    src = _poll_endpoint_source("overview")
    assert "folders_on_disk" in src, "overview must derive folders from the filesystem"


def test_folders_on_disk_matches_the_api_shape():
    """Disk-derived folders must look like what walk_projects would return."""
    from resolve_sync import project_meta

    folders = project_meta.folders_on_disk()
    assert isinstance(folders, dict)
    for name, folder in folders.items():
        assert isinstance(name, str) and name
        assert isinstance(folder, str)          # "" means top level
        assert not folder.startswith("/"), folder


def test_navigation_helpers_still_exist_for_user_initiated_work():
    """Export/import genuinely need to navigate — that must still be possible."""
    from resolve_sync import projects

    assert callable(projects.walk_projects)
    assert callable(projects.goto_folder)
    # ...and they must hold the session lock when they do.
    src = (Path(__file__).resolve().parent.parent / "resolve_sync" / "projects.py").read_text("utf-8")
    assert "with RESOLVE_LOCK:" in src


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

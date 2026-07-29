"""The local agent: a small HTTP API + dashboard, bound to localhost.

The browser can't touch your disk or drive Resolve, so this agent is the bridge.
The web dashboard (served from ./static) calls these endpoints; the agent does
the real work — export/import `.drp`, fingerprint media, relink — and reports
back.

Endpoints
---------
GET  /                      dashboard
GET  /api/status            resolve connection + config summary
POST /api/config            update store path / media roots / author
GET  /api/projects          projects in Resolve and in the store
GET  /api/versions?project= version history for a project
POST /api/push              snapshot current/named project -> store
POST /api/pull              import a stored version -> Resolve, auto-relink media
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import detect, project_meta, projects, thumbs
from .autosync import AutoSync, stage_head
from . import branding, google_auth
from .drive_store import DriveError, DriveStore
from .google_auth import GoogleAuthError, GoogleClient
from .config import CONFIG_DIR, Config, SyncState
from .git_store import GitError, GitStore
from .media import MediaIndex, relink_project
from .resolve_conn import ResolveUnavailable, is_available
from .store import DRP_FILENAME, Store, Version, compute_version_id

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Resolve Sync Agent")
cfg = Config.load()


def _store_id() -> str:
    """Identity of the configured store, so parent pointers stay scoped to it."""
    if cfg.backend == "git":
        return f"git:{cfg.git_remote}"
    if cfg.backend == "drive":
        return "drive"
    path = (cfg.store_path or "").rstrip("/\\").lower()
    return f"folder:{path}"


state = SyncState(_store_id())


# ---------------------------------------------------------------------------
# Local-only request guard
# ---------------------------------------------------------------------------
# Binding to 127.0.0.1 stops remote packets but NOT DNS rebinding: a page on the
# public internet can resolve its own hostname to 127.0.0.1 and then talk to this
# agent from the user's browser. Verified before this guard existed:
#   curl -H 'Host: evil.example.com' http://127.0.0.1:7788/api/status  ->  200
# So we require a loopback Host, and reject any cross-origin Origin.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_ok(host: str) -> bool:
    return host.rsplit(":", 1)[0].strip("[]").lower() in {
        h.strip("[]").lower() for h in _ALLOWED_HOSTS
    }


@app.middleware("http")
async def _local_only(request, call_next):
    host = request.headers.get("host", "")
    if host and not _host_ok(host):
        return JSONResponse({"detail": "Forbidden: non-local Host header."}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if not _host_ok(parsed.netloc or parsed.path):
            return JSONResponse(
                {"detail": "Forbidden: cross-origin request."}, status_code=403)
    return await call_next(request)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store():
    """The configured version store backend (git or plain folder)."""
    if cfg.backend != "drive" and not cfg.store:
        raise HTTPException(400, "No store configured. Set a store location first.")
    if cfg.backend == "git":
        if not cfg.git_remote:
            raise HTTPException(400, "Git backend selected but no git remote is set.")
        return GitStore(cfg.store, cfg.git_remote)
    if cfg.backend == "drive":
        if not google_auth.signed_in():
            raise HTTPException(401, "Not signed in to Google.")
        return DriveStore(_google_client())
    return Store(cfg.store)


# ---------------------------------------------------------------------------
# Status & config
# ---------------------------------------------------------------------------
@app.get("/api/status")
def status() -> dict:
    connected = is_available()
    current = None
    if connected:
        try:
            current = projects.current_project_name()
        except ResolveUnavailable:
            connected = False
    return {
        "resolve_connected": connected,
        "current_project": current,
        "git_available": shutil.which("git") is not None,
        "config": {
            "backend": cfg.backend,
            "store_path": cfg.store_path,
            "git_remote": cfg.git_remote,
            "media_roots": cfg.media_roots,
            "synced_projects": cfg.synced_projects,
            "author": cfg.author,
            "machine_id": cfg.machine_id,
        },
    }


class ConfigPatch(BaseModel):
    backend: str | None = None
    store_path: str | None = None
    git_remote: str | None = None
    media_roots: list[str] | None = None
    synced_projects: list[str] | None = None
    author: str | None = None


# Providers whose synced folders must never hold media: fingerprinting reads both
# ends of every file, which forces the provider to hydrate the whole library.
_CLOUD_HINTS = ("dropbox", "onedrive", "google drive", "googledrive", "icloud")


def _cloud_media_warnings(roots: list[str]) -> list[str]:
    out = []
    for r in roots:
        low = r.replace("\\", "/").lower()
        if any(h in low for h in _CLOUD_HINTS):
            out.append(
                f"'{r}' looks like a cloud-synced folder. Media roots should be "
                "local or on a NAS — scanning a synced folder can force a full "
                "download of every file."
            )
    return out


@app.post("/api/config")
def update_config(patch: ConfigPatch) -> dict:
    if patch.backend is not None and patch.backend in ("git", "folder", "drive"):
        cfg.backend = patch.backend
    if patch.store_path is not None:
        cfg.store_path = patch.store_path
    if patch.git_remote is not None:
        cfg.git_remote = patch.git_remote.strip()
    if patch.media_roots is not None:
        cfg.media_roots = [r for r in patch.media_roots if r.strip()]
    if patch.synced_projects is not None:
        cfg.synced_projects = [p for p in patch.synced_projects if p.strip()]
    if patch.author is not None and patch.author.strip():
        cfg.author = patch.author.strip()
    cfg.save()
    state.use_store(_store_id())
    return {
        "ok": True,
        "warnings": _cloud_media_warnings(cfg.media_roots),
        "config": {
            "backend": cfg.backend,
            "store_path": cfg.store_path,
            "git_remote": cfg.git_remote,
            "media_roots": cfg.media_roots,
            "synced_projects": cfg.synced_projects,
            "author": cfg.author,
        },
    }


@app.get("/api/detect")
def detect_environment() -> dict:
    """Everything the first-run wizard needs to fill itself in.

    Deliberately does NOT require Resolve to be running: media roots come from
    Resolve's own project databases on disk, so setup works before the user has
    Resolve open.
    """
    resolve_ok = is_available()
    current = None
    if resolve_ok:
        try:
            current = projects.current_project_name()
        except ResolveUnavailable:
            resolve_ok = False
    return {
        "resolve_connected": resolve_ok,
        "current_project": current,
        "git_available": shutil.which("git") is not None,
        "providers": [p.to_dict() for p in detect.find_providers()],
        "media_suggestions": [m.to_dict() for m in detect.suggest_media_roots()],
        "configured": bool(cfg.store_path)
        or (cfg.backend == "drive" and google_auth.signed_in()),
    }


# ---------------------------------------------------------------------------
# Projects & versions
# ---------------------------------------------------------------------------
@app.get("/api/projects")
def get_projects() -> dict:
    # Disk-derived, so this endpoint never navigates Resolve either.
    resolve_projects: list[str] = list(project_meta.folders_on_disk())
    if not resolve_projects and is_available():
        try:
            resolve_projects = projects.list_projects()
        except ResolveUnavailable:
            pass
    try:
        store_projects = _store().list_projects()
    except (HTTPException, GitError, DriveError, GoogleAuthError):
        store_projects = []
    return {"resolve": resolve_projects, "store": store_projects}


@app.get("/api/versions")
def get_versions(project: str) -> dict:
    store = _store()
    try:
        if isinstance(store, Store):
            versions, quarantined = store.list_versions(project, with_quarantine=True)
            heads = [h.version_id for h in store.heads(project)]
        else:
            versions, quarantined = store.list_versions(project), []
            # Compute heads topologically here too. Taking versions[0] would
            # yield a single head for EVERY backend, so `forked` could never be
            # True for git or Drive and a real divergence would go unreported.
            if hasattr(store, "heads"):
                heads = [h.version_id for h in store.heads(project)]
            else:
                parents = {v.parent for v in versions
                           if v.parent and v.parent != v.version_id}
                tips = [v.version_id for v in versions if v.version_id not in parents]
                heads = tips or ([versions[0].version_id] if versions else [])
    except (DriveError, GoogleAuthError) as exc:
        raise HTTPException(502, f"Project Library error: {exc}") from exc
    except GitError as exc:
        raise HTTPException(502, f"Project Library error: {exc}") from exc
    return {
        "project": project,
        "parent": state.parent_of(project),
        "head": heads[0] if heads else None,
        "heads": heads,
        "forked": len(heads) > 1,
        "quarantined": quarantined,
        "versions": [v.to_manifest() for v in versions],
    }


# Operations currently in flight, so the UI can show a live "Backing up…" /
# "Restoring…" state instead of pretending nothing is happening. GIL-safe dict.
_active_ops: dict[str, str] = {}


def _git_web_url(remote: str, ssh_config_text: str | None = None) -> str | None:
    """Best-effort browser URL for a git remote.

    Handles https remotes, scp-style ssh remotes, and — important here — SSH
    HOST ALIASES: this machine's remote is git@github-resolvesync:user/repo,
    where 'github-resolvesync' only exists in ~/.ssh/config. The alias is
    resolved to its real HostName so the link actually works.
    """
    r = (remote or "").strip()
    if not r:
        return None
    if r.startswith(("http://", "https://")):
        return re.sub(r"\.git$", "", r)
    m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/](.+?)(?:\.git)?/?$", r)
    if not m:
        return None
    host, path = m.group(1), m.group(2)
    if ssh_config_text is None:
        try:
            ssh_config_text = (Path.home() / ".ssh" / "config").read_text("utf-8")
        except OSError:
            ssh_config_text = ""
    aliases: list[str] = []
    for line in ssh_config_text.splitlines():
        token = line.strip()
        if token.lower().startswith("host "):
            aliases = token.split()[1:]
        elif token.lower().startswith("hostname") and host in aliases:
            host = token.split()[1]
            break
    if "github" in host and not host.endswith("github.com"):
        host = "github.com"          # unresolved alias — a safe, useful guess
    return f"https://{host}/{path}"


def _store_label() -> str:
    if cfg.backend == "git":
        url = _git_web_url(cfg.git_remote)
        return url.removeprefix("https://") if url else cfg.git_remote
    if cfg.backend == "drive":
        return "drive.google.com › ResolveSync"
    return cfg.store_path or "not set"


_drive_root_cache: dict[str, str] = {}


# The dashboard polls /api/overview every ~12s; for the git backend a store
# listing is a network fetch, so serve a short-lived cache and invalidate it on
# every push/pull (the only local events that change the answer).
_listing_cache = {"at": 0.0, "id": "", "names": []}
_LISTING_TTL = 30.0


def _store_projects_cached(force: bool = False) -> list[str]:
    sid = _store_id()
    now = time.time()
    if (not force and _listing_cache["id"] == sid
            and now - _listing_cache["at"] < _LISTING_TTL):
        return _listing_cache["names"]
    try:
        names = _store().list_projects()
    except (HTTPException, GitError, DriveError, GoogleAuthError):
        return _listing_cache["names"] if _listing_cache["id"] == sid else []
    _listing_cache.update(at=now, id=sid, names=names)
    return names


@app.get("/api/overview")
def overview() -> dict:
    """Everything the Project Manager view needs, in one call.

    Combines Resolve's own project metadata (resolution, fps, timeline count,
    last modified — read from its metadata cache, no Resolve required) with our
    sync state, so each row can show a plain-English status.
    """
    connected = is_available()
    current = None
    resolve_projects: list[str] = []
    # Folder tree comes from DISK, never from the scripting API. walk_projects()
    # moves Resolve's GLOBAL folder cursor, which is the same cursor driving the
    # Project Manager window — polling it every 12s shifted the view under the
    # user's cursor while they were clicking, and could load the wrong project.
    folder_of = project_meta.folders_on_disk()
    # The project LIST also comes from disk. projects.list_projects() is built on
    # walk_projects() and would navigate too. GetCurrentProject() is safe: it
    # reads the open project without moving the cursor.
    resolve_projects = list(folder_of)
    if connected:
        try:
            current = projects.current_project_name()
            if not resolve_projects:      # non-disk database (e.g. PostgreSQL)
                resolve_projects = projects.list_projects()
                folder_of = {n: "" for n in resolve_projects}
        except ResolveUnavailable:
            connected = False

    store_projects = _store_projects_cached()

    meta = project_meta.all_projects()
    auto_state = auto.to_dict()
    incoming = set(auto_state.get("incoming") or [])
    staged = auto_state.get("staged") or {}
    deferred = set(auto_state.get("deferred") or [])
    synced_set = set(cfg.synced_projects)

    names = sorted(set(resolve_projects) | set(store_projects), key=str.lower)
    rows = []
    for name in names:
        m = meta.get(name)
        in_store = name in store_projects
        parent = state.parent_of(name)
        if name in _active_ops:
            status, label = "active", _active_ops[name]
        elif name in incoming:
            # Staged = auto-pull already downloaded and verified it locally.
            status = "incoming"
            label = "Ready to import" if name in staged else "New version available"
        elif name in deferred:
            status, label = "syncing", "Waiting — open it to back up"
        elif not in_store:
            status, label = "none", "Not backed up"
        elif parent is None:
            status, label = "attn", "Not synced on this computer"
        else:
            status, label = "synced", "Backed up"
        rows.append({
            "name": name,
            "in_resolve": name in resolve_projects,
            "in_store": in_store,
            "is_open": name == current,
            "watched": name in synced_set,
            "status": status,
            "status_label": label,
            "staged_version": (staged.get(name) or {}).get("version_id"),
            "folder": folder_of.get(name),
            "meta": m.to_dict() if m else None,
        })

    folder_counts: dict[str, int] = {}
    for r in rows:
        if r["folder"] is not None:
            folder_counts[r["folder"]] = folder_counts.get(r["folder"], 0) + 1

    return {
        "resolve_connected": connected,
        "current_project": current,
        "backend": cfg.backend,
        "author": cfg.author,
        "configured": bool(cfg.store_path) or cfg.backend == "drive",
        "auto_sync": auto_state,
        "store_info": {"kind": cfg.backend, "label": _store_label()},
        "thumbs_available": thumbs.have_ffmpeg(),
        "folders": [{"path": k, "count": v} for k, v in sorted(folder_counts.items())],
        "busy": sorted(_active_ops),
        "counts": {
            "total": len(rows),
            "backed_up": sum(1 for r in rows if r["in_store"]),
            "incoming": len(incoming),
            "watched": len(synced_set),
        },
        "projects": rows,
    }


@app.post("/api/store/open")
def open_store_location() -> dict:
    """Open the backup location: Explorer for a folder, the browser for a URL.

    Safe to expose because the DNS-rebinding middleware already rejects any
    request whose Host/Origin isn't loopback — a hostile web page can't reach
    this. Worst case is opening the user's own backup folder on their screen.
    """
    import os
    import webbrowser

    if cfg.backend == "folder":
        path = cfg.store_path
        if not path or not Path(path).is_dir():
            raise HTTPException(400, "The backup folder doesn't exist yet — back "
                                     "up a project first.")
        os.startfile(path)  # noqa: S606 - opening the user's own folder locally
        return {"ok": True, "target": path}

    if cfg.backend == "git":
        url = _git_web_url(cfg.git_remote)
        if not url:
            raise HTTPException(400, "Couldn't work out the repository's web address.")
        webbrowser.open(url)
        return {"ok": True, "target": url}

    # drive — the folder id costs one API call; cache it per store.
    sid = _store_id()
    folder_id = _drive_root_cache.get(sid)
    if not folder_id:
        try:
            folder_id = _store()._root()
        except (HTTPException, DriveError, GoogleAuthError) as exc:
            raise HTTPException(502, f"Couldn't reach Google Drive: {exc}") from exc
        _drive_root_cache[sid] = folder_id
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    webbrowser.open(url)
    return {"ok": True, "target": url}


@app.get("/api/thumb")
def thumbnail(project: str):
    """A real frame from a clip the project uses, cached on disk.

    Generated lazily so opening the grid never blocks on 164 ffmpeg runs; the
    browser requests only what it can see.
    """
    path = thumbs.get(project)
    if path is None:
        raise HTTPException(404, "No thumbnail available.")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/thumb/refresh")
def thumbnail_refresh(project: str | None = None) -> dict:
    thumbs.clear(project)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------
class PushRequest(BaseModel):
    project: str | None = None          # single project (back-compat)
    projects: list[str] | None = None   # or many at once
    message: str = ""
    # Git backend: keep this snapshot on a fork branch when the remote has moved on.
    allow_fork: bool = False


@app.post("/api/push")
def push(req: PushRequest) -> dict:
    """Snapshot one or many projects.

    Multi-project pushes never abort the batch on a single failure: each project
    is reported independently, so one unsaved or conflicted project can't stop
    the rest from being backed up.
    """
    if not is_available():
        raise HTTPException(409, "Resolve is not connected.")

    names = [n for n in (req.projects or []) if n.strip()]
    if not names:
        single = req.project or projects.current_project_name()
        if not single:
            raise HTTPException(400, "No project specified and none is open in Resolve.")
        names = [single]

    if len(names) == 1:
        # Single push keeps raising, so the UI's fork prompt still gets its 409.
        return _push_one(names[0], req.message, req.allow_fork)

    results = []
    for name in names:
        try:
            results.append({"project": name, **_push_one(name, req.message, req.allow_fork)})
        except HTTPException as exc:
            results.append({"project": name, "ok": False, "error": exc.detail,
                            "conflict": exc.status_code == 409})
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok > 0, "pushed": ok, "total": len(results), "results": results}


def _push_one(name: str, message: str, allow_fork: bool) -> dict:
    store = _store()
    _active_ops[name] = "Backing up…"
    try:
        # All Resolve API work happens under one lock, so a concurrent dashboard
        # poll cannot move the folder cursor between navigate and export (which
        # would silently export a different project). The lock is released before
        # any store call — see the LOCK ORDER note in projects.py.
        with projects.RESOLVE_LOCK, tempfile.TemporaryDirectory() as tmp:
            proj = _load_or_current(name)
            fingerprints = projects.collect_fingerprints(proj)
            # Constant filename here too: a project named "Ep 01: Rough Cut"
            # would otherwise write into an NTFS alternate data stream.
            drp = projects.export_project(name, Path(tmp) / DRP_FILENAME)
            version_id = compute_version_id(drp)
            parent = state.parent_of(name)
            forked = store.is_fork(name, parent)

            version = Version(
                version_id=version_id,
                project=name,
                author=cfg.author,
                machine_id=cfg.machine_id,
                created=_now(),
                parent=parent,
                message=message,
                # Never derive a filename from the project name — see DRP_FILENAME.
                drp_name=DRP_FILENAME,
                media=fingerprints,
                media_roots=projects.media_roots_from_fingerprints(fingerprints),
            )
            if isinstance(store, GitStore):
                store.put(version, drp, allow_fork=allow_fork)
            else:
                store.put(version, drp)
                # Publish confirmation: a local write succeeding does NOT mean a
                # peer can see it. Re-read what landed and check it against the
                # content hash. (The git backend gets this from the push itself.)
                if not store.verify(name, version_id):
                    raise HTTPException(
                        500,
                        "Push wrote the version but it failed verification. Check "
                        "that the store folder is writable and syncing.",
                    )
    except ResolveUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except GitError as exc:
        if exc.rejected:
            # This is the compare-and-set doing its job: someone else pushed
            # first. Never auto-force — that would destroy their version.
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(502, f"Project Library error: {exc}") from exc
    except (DriveError, GoogleAuthError) as exc:
        raise HTTPException(502, f"Project Library error: {exc}") from exc
    finally:
        _active_ops.pop(name, None)

    state.set_parent(name, version_id)
    _listing_cache["at"] = 0.0
    return {
        "ok": True,
        "version_id": version_id,
        "forked": forked,
        "media_count": len(fingerprints),
    }


# ---------------------------------------------------------------------------
# Pull (import + auto-relink)
# ---------------------------------------------------------------------------
class PullRequest(BaseModel):
    project: str
    version_id: str
    import_as: str | None = None   # defaults to "<project> (synced)"


@app.post("/api/pull")
def pull(req: PullRequest) -> dict:
    if not is_available():
        raise HTTPException(409, "Resolve is not connected.")
    store = _store()

    _active_ops[req.project] = "Restoring…"
    try:
        return _pull_inner(req, store)
    finally:
        _active_ops.pop(req.project, None)


def _staged_for(project: str, version_id: str):
    """The pre-fetched (Version, drp path) when auto-pull staged this exact
    version, else (None, None). Re-verified by checksum so an evicted or
    altered stage file falls back to a normal store read, never a bad import."""
    info = auto.staged.get(project)
    if not info or info.get("version_id") != version_id or not info.get("path"):
        return None, None
    p = Path(info["path"])
    if not p.is_file() or compute_version_id(p) != version_id:
        return None, None
    try:
        return Version.from_manifest(info["manifest"]), p
    except (KeyError, TypeError, ValueError):
        return None, None


def _pull_inner(req: PullRequest, store) -> dict:
    # Auto-pull may have already fetched and verified this version — then the
    # pull needs no store round-trip at all (~20s saved on the Drive backend).
    version, drp = _staged_for(req.project, req.version_id)
    if version is None:
        version = store.get_version(req.project, req.version_id)
        if not version:
            raise HTTPException(404, "Version not found in store.")
        # version_id IS sha1(.drp)[:16]. Verifying costs ~2ms and turns a partial,
        # dehydrated, or corrupted transfer into a clean retryable error instead of
        # a broken import — .is_file() alone returns True for a half-synced file.
        # GitStore.drp_path verifies as it materializes; the folder store checks here.
        try:
            drp = store.drp_path(req.project, req.version_id)
        except (GitError, DriveError, GoogleAuthError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if not drp.is_file():
            raise HTTPException(404, "Stored .drp is missing (not synced yet?).")
        if isinstance(store, Store) and not store.verify(req.project, req.version_id):
            raise HTTPException(
                409,
                "The stored .drp failed its checksum — it is still transferring or is "
                "corrupt. Wait for sync to finish and try again.",
            )

    import_as = req.import_as or f"{req.project} (synced)"
    try:
        proj = projects.import_project(drp, import_as)
        index = MediaIndex(cfg.roots)
        report = relink_project(
            media_pool=proj.GetMediaPool(),
            fingerprints=version.media,
            sender_roots=version.media_roots,
            local_roots=cfg.roots,
            index=index,
        )
    except ResolveUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc

    state.set_parent(req.project, req.version_id)
    _listing_cache["at"] = 0.0
    auto.incoming = [n for n in auto.incoming if n != req.project]
    staged = auto.staged.pop(req.project, None)
    if staged and staged.get("path"):
        Path(staged["path"]).unlink(missing_ok=True)
    return {"ok": True, "imported_as": import_as, "relink": report.to_dict()}


def _load_or_current(name: str):
    """Return project `name` as a loaded project, loading it if needed."""
    pm = projects._pm()  # internal but fine within the package
    current = pm.GetCurrentProject()
    if current and current.GetName() == name:
        return current
    pm.LoadProject(name)
    proj = pm.GetCurrentProject()
    if not proj or proj.GetName() != name:
        raise ResolveUnavailable(f"Could not load project '{name}'.")
    return proj


# ---------------------------------------------------------------------------
# Sign in with Google
# ---------------------------------------------------------------------------
def _google_client() -> GoogleClient:
    """Per-machine override if set, else the credentials baked into the build.

    Distributed builds ship a client, so people you share the app with never
    touch Google Cloud Console — they only click "Sign in with Google".
    """
    cid, secret = cfg.google_client_id, cfg.google_client_secret
    if not (cid and secret):
        cid, secret = branding.bundled_google_client()
    return GoogleClient(
        client_id=cid,
        client_secret=secret,
        redirect_uri=f"http://{cfg.host}:{cfg.port}/oauth/google/callback",
    )


@app.get("/api/google/status")
def google_status() -> dict:
    client = _google_client()
    return {
        "configured": client.configured,
        "bundled": branding.has_bundled_client(),
        "signed_in": google_auth.signed_in(),
        "email": google_auth.account_email(client) if google_auth.signed_in() else None,
        "redirect_uri": client.redirect_uri,
    }


class GoogleSetup(BaseModel):
    client_id: str
    client_secret: str


@app.post("/api/google/setup")
def google_setup(body: GoogleSetup) -> dict:
    """One-time: store the APP's Google client credentials (not the user's)."""
    cfg.google_client_id = body.client_id.strip()
    cfg.google_client_secret = body.client_secret.strip()
    cfg.save()
    return google_status()


@app.post("/api/google/signin")
def google_signin() -> dict:
    """Return the consent URL for the dashboard to open."""
    try:
        url, _state = google_auth.build_auth_url(_google_client())
    except GoogleAuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": url}


@app.get("/oauth/google/callback")
def google_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Google redirects the user's browser here after they approve."""
    if error:
        return HTMLResponse(_oauth_page("Sign-in cancelled", error, False))
    try:
        google_auth.exchange_code(_google_client(), code, state)
    except GoogleAuthError as exc:
        return HTMLResponse(_oauth_page("Sign-in failed", str(exc), False))
    return HTMLResponse(_oauth_page(
        "You're signed in", "You can close this tab and go back to Resolve Sync.", True))


@app.post("/api/google/signout")
def google_signout() -> dict:
    google_auth.forget()
    return google_status()


def _oauth_page(title: str, msg: str, ok: bool) -> str:
    colour = "#3fb950" if ok else "#f85149"
    return f"""<!doctype html><meta charset=utf-8>
<title>{title}</title>
<body style="font-family:system-ui;background:#0e1116;color:#e6edf3;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center;max-width:420px;padding:32px">
<div style="font-size:48px;color:{colour}">{'&#10003;' if ok else '&#10007;'}</div>
<h1 style="font-size:20px;margin:12px 0 8px">{title}</h1>
<p style="color:#8b98a5;font-size:14px">{msg}</p>
</div></body>"""


# ---------------------------------------------------------------------------
# Auto-sync
# ---------------------------------------------------------------------------
def _remote_head(name: str) -> str | None:
    try:
        head = _store().head(name)
    except (HTTPException, GitError):
        return None
    return head.version_id if head else None


# slug -> (remote SHA, head version_id). The SHA is a change fingerprint from
# one ls-remote call; the expensive head/manifest read only happens for branches
# whose SHA actually moved since we last looked.
_tip_cache: dict[str, tuple[str, str | None]] = {}


def _incoming_bulk(names: list[str]) -> list[str]:
    """Which of `names` have a newer version in the store than we've synced?

    Git backend: one ls-remote round-trip for the whole library instead of a
    fetch+checkout+log per project (measured ~2.9s each). Other backends fall
    back to per-name head reads — cheap locally, and already throttled to the
    INCOMING_INTERVAL clock by the caller.
    """
    try:
        store = _store()
    except HTTPException:
        return []
    ahead: list[str] = []
    if isinstance(store, GitStore):
        from .git_store import _slug
        tips = store.remote_tips()
        for name in names:
            slug = _slug(name)
            sha = tips.get(slug)
            if not sha:
                continue                      # never pushed — nothing incoming
            cached = _tip_cache.get(slug)
            if cached and cached[0] == sha:
                head_vid = cached[1]
            else:
                head = store.head(name)       # branch moved: read it once
                head_vid = head.version_id if head else None
                _tip_cache[slug] = (sha, head_vid)
            if head_vid and head_vid != state.parent_of(name):
                ahead.append(name)
        return ahead
    for name in names:
        try:
            head = store.head(name)
        except (GitError, DriveError, GoogleAuthError):
            continue
        if head and head.version_id != state.parent_of(name):
            ahead.append(name)
    return ahead


# Where auto-pull stages pre-fetched .drps. Lives under our own config dir —
# never inside the store, so a stage file can't be mistaken for a version.
_STAGED_DIR = CONFIG_DIR / "staged"


def _prefetch_head(name: str) -> dict | None:
    """Fetch half of auto-pull. Store-only work — never touches Resolve."""
    try:
        store = _store()
    except HTTPException:
        return None       # store unconfigured or signed out — nothing to stage
    return stage_head(store, name, state.parent_of(name), _STAGED_DIR)


auto = AutoSync(
    cfg=cfg,
    do_push=lambda name: _push_one(name, "Auto-sync", allow_fork=False),
    remote_head=_remote_head,
    local_parent=state.parent_of,
    current_project=projects.current_project_name_locked,
    incoming_bulk=_incoming_bulk,
    prefetch=_prefetch_head,
)


@app.get("/api/autosync")
def autosync_status() -> dict:
    return auto.to_dict()


class AutoSyncPatch(BaseModel):
    enabled: bool | None = None
    interval: int | None = None


@app.post("/api/autosync")
def autosync_config(patch: AutoSyncPatch) -> dict:
    if patch.interval is not None:
        cfg.auto_sync_interval = max(15, int(patch.interval))
    if patch.enabled is not None:
        cfg.auto_sync = bool(patch.enabled)
    cfg.save()
    if cfg.auto_sync:
        auto.start()
    return auto.to_dict()


@app.on_event("startup")
def _start_autosync() -> None:
    if cfg.auto_sync:
        auto.start()


# Dashboard (mounted last so /api/* wins).
@app.get("/")
def index() -> HTMLResponse:
    """Serve the dashboard with cache-busted asset URLs.

    The UI lives at a fixed localhost URL, so after an app update a browser
    would happily keep running yesterday's JavaScript against today's API —
    which looks like a broken app, not a stale cache. Stamping each asset with
    the build's newest mtime makes an update always load cleanly, while still
    allowing caching between updates.
    """
    html = (STATIC_DIR / "index.html").read_text("utf-8")
    try:
        stamp = int(max(f.stat().st_mtime for f in STATIC_DIR.glob("*.*")))
    except (OSError, ValueError):
        stamp = 0
    html = re.sub(r'(src|href)="/([^"]+\.(?:js|css))"',
                  lambda m: f'{m.group(1)}="/{m.group(2)}?v={stamp}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")

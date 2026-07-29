"""Google Drive version store — the same Store interface, backed by Drive.

Layout inside Drive (all inside one app-created folder, so the ``drive.file``
scope is enough and we can never see the user's other files):

    ResolveSync/                       <- appFolder
      <project-slug>/                  <- one folder per project
        <version_id>.drp
        <version_id>.json

Flat and dumb on purpose: Drive has no atomic compare-and-set, so this backend
DETECTS forks (two heads) rather than preventing them, exactly like the plain
folder backend. Use the GitHub backend when two people edit the same project.
"""

from __future__ import annotations

import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from .google_auth import GoogleAuthError, access_token
from .git_store import _slug
from .store import DRP_FILENAME, Version, compute_version_id

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    pass


class DriveStore:
    """Version store in Google Drive. Mirrors the Store interface."""

    def __init__(self, client, root_name: str = "ResolveSync"):
        self.client = client
        self.root_name = root_name
        self._ids: dict[str, str] = {}   # cache: path-ish key -> file id

    # --- HTTP ------------------------------------------------------------
    def _req(self, method: str, url: str, *, params=None, data=None,
             headers=None, raw=False):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        hdrs = {"Authorization": f"Bearer {access_token(self.client)}"}
        hdrs.update(headers or {})
        body = data
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # Surface Google's human-readable message, not a JSON blob.
            try:
                msg = json.loads(body).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                msg = ""
            raise DriveError(
                f"Google Drive error {exc.code}: {msg or body[:200]}"
            ) from exc
        except OSError as exc:
            raise DriveError(f"Could not reach Google Drive: {exc}") from exc
        if raw:
            return payload
        return json.loads(payload) if payload else {}

    def _list_all(self, params: dict) -> list[dict]:
        """files.list, following nextPageToken to completion.

        Without this, a project silently stops at pageSize entries: head()
        then reports an old version as the tip, every push looks like a fork,
        and the UI can offer to pull an OLDER version over newer work.
        """
        out: list[dict] = []
        page = dict(params)
        page.setdefault("pageSize", 1000)
        fields = page.get("fields", "files(id,name)")
        if "nextPageToken" not in fields:
            page["fields"] = f"nextPageToken,{fields}"
        while True:
            res = self._req("GET", f"{API}/files", params=page)
            out.extend(res.get("files") or [])
            token = res.get("nextPageToken")
            if not token:
                return out
            page["pageToken"] = token

    # --- folders ---------------------------------------------------------
    def _find_child(self, name: str, parent: str) -> str | None:
        q = (f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and "
             f"'{parent}' in parents and trashed = false")
        files = self._list_all({"q": q, "fields": "files(id,name)"})
        return files[0]["id"] if files else None

    def _ensure_folder(self, name: str, parent: str) -> str:
        key = f"{parent}/{name}"
        if key in self._ids:
            return self._ids[key]
        found = self._find_child(name, parent)
        if not found:
            created = self._req("POST", f"{API}/files",
                                data={"name": name, "mimeType": FOLDER_MIME,
                                      "parents": [parent]},
                                params={"fields": "id"})
            found = created["id"]
        self._ids[key] = found
        return found

    def _root(self) -> str:
        return self._ensure_folder(self.root_name, "root")

    def _project_folder(self, project: str, create: bool = True) -> str | None:
        root = self._root()
        slug = _slug(project)
        if create:
            return self._ensure_folder(slug, root)
        return self._find_child(slug, root)

    # --- writing ---------------------------------------------------------
    def _upload(self, name: str, parent: str, content: bytes, mime: str) -> str:
        """Multipart upload of a small file (a .drp is KB to low-MB)."""
        boundary = "-------resolvesync-boundary"
        meta = json.dumps({"name": name, "parents": [parent]}).encode()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + meta
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        res = self._req("POST", f"{UPLOAD}/files",
                        params={"uploadType": "multipart", "fields": "id"},
                        data=body,
                        headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return res["id"]

    def put(self, version: Version, drp_src: Path) -> Version:
        folder = self._project_folder(version.project)
        # Content-addressed: if this version already exists, do nothing.
        if self._find_child(f"{version.version_id}.json", folder):
            return version
        self._upload(f"{version.version_id}.drp", folder,
                     drp_src.read_bytes(), "application/octet-stream")
        manifest = dict(version.to_manifest())
        manifest["drp_name"] = DRP_FILENAME
        self._upload(f"{version.version_id}.json", folder,
                     json.dumps(manifest, indent=2).encode(), "application/json")
        return version

    # --- reading ---------------------------------------------------------
    def list_projects(self) -> list[str]:
        root = self._root()
        files = self._list_all({
            "q": f"'{root}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
            "fields": "files(id,name)"})
        names: list[str] = []
        for f in files:
            # Prefer the real project name from a manifest over the slug.
            display = self._display_name(f["id"]) or f["name"]
            if display not in names:
                names.append(display)
        return sorted(names)

    def _display_name(self, folder_id: str) -> str | None:
        files = self._list_all({
            "q": f"'{folder_id}' in parents and name contains '.json' and trashed = false",
            "fields": "files(id,name)"})[:1]
        if not files:
            return None
        try:
            blob = self._req("GET", f"{API}/files/{files[0]['id']}",
                             params={"alt": "media"}, raw=True)
            return json.loads(blob).get("project")
        except (DriveError, json.JSONDecodeError):
            return None

    def list_versions(self, project: str) -> list[Version]:
        folder = self._project_folder(project, create=False)
        if not folder:
            return []
        files = {f["name"]: f["id"] for f in self._list_all({
            "q": f"'{folder}' in parents and trashed = false",
            "fields": "files(id,name)"})}
        versions: list[Version] = []
        for name, fid in files.items():
            if not name.endswith(".json"):
                continue
            vid = name[:-5]
            if f"{vid}.drp" not in files:
                continue                       # payload missing — skip, don't crash
            try:
                blob = self._req("GET", f"{API}/files/{fid}", params={"alt": "media"}, raw=True)
                versions.append(Version.from_manifest(json.loads(blob)))
            except (DriveError, json.JSONDecodeError, KeyError, TypeError):
                continue
        versions.sort(key=lambda v: (v.created, v.version_id), reverse=True)
        return versions

    def get_version(self, project: str, version_id: str) -> Version | None:
        for v in self.list_versions(project):
            if v.version_id == version_id:
                return v
        return None

    def drp_path(self, project: str, version_id: str) -> Path:
        folder = self._project_folder(project, create=False)
        if not folder:
            raise DriveError(f"No such project in Drive: {project}")
        fid = self._find_child(f"{version_id}.drp", folder)
        if not fid:
            raise DriveError(f"Version {version_id} has no project file in Drive.")
        data = self._req("GET", f"{API}/files/{fid}", params={"alt": "media"}, raw=True)
        out = Path(tempfile.mkdtemp(prefix="resolve-sync-")) / f"{_slug(project)}-{version_id[:8]}.drp"
        out.write_bytes(data)
        # version_id IS sha1(.drp)[:16] — verify before Resolve ever sees it.
        if compute_version_id(out) != version_id:
            out.unlink(missing_ok=True)
            raise DriveError("The downloaded project file failed its checksum.")
        return out

    def heads(self, project: str) -> list[Version]:
        versions = self.list_versions(project)
        parents = {v.parent for v in versions if v.parent and v.parent != v.version_id}
        tips = [v for v in versions if v.version_id not in parents]
        return tips or versions[:1]

    def head(self, project: str) -> Version | None:
        tips = self.heads(project)
        return tips[0] if tips else None

    def is_fork(self, project: str, parent: str | None) -> bool:
        head = self.head(project)
        return bool(head and head.version_id != parent)

    def verify(self, project: str, version_id: str) -> bool:
        try:
            self.drp_path(project, version_id)
            return True
        except (DriveError, GoogleAuthError):
            return False

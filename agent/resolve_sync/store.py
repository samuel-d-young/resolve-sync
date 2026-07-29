"""The version store — the shared "repository" that machines sync through.

MVP implementation is a plain folder. Point `store_path` at a NAS share, a
synced Dropbox/Drive folder, or a mounted bucket and you have multi-machine
sync with zero extra infrastructure. The interface is deliberately tiny so a
cloud/S3 backend can drop in later without touching the rest of the agent.

Layout on disk::

    <store>/
      <ProjectName>/
        <version_id>/
          project.drp
          version.json      # manifest: author, parent, media fingerprints
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .media import MediaFingerprint

# The .drp is ALWAYS written under this exact name. Deriving it from the Resolve
# project name is unsafe: "Ep 01: Rough Cut" writes into an NTFS alternate data
# stream on Windows (peers receive 0 bytes while local checks still pass), and
# names containing \ : < > " | ? * are silently never propagated by Dropbox.
DRP_FILENAME = "project.drp"

# Artifacts a sync client may drop next to real data.
_CONFLICT_RE = re.compile(
    r"(conflicted copy|\.sync-conflict-|\(case conflict\)|\(\d+\)$|\.part$)",
    re.IGNORECASE,
)


def _is_conflict_artifact(name: str) -> bool:
    return bool(_CONFLICT_RE.search(name))


@dataclass
class Version:
    version_id: str
    project: str
    author: str
    machine_id: str
    created: str            # ISO8601, supplied by caller (agent stamps it)
    parent: str | None
    message: str
    drp_name: str
    media: list[MediaFingerprint]
    media_roots: list[str]

    def to_manifest(self) -> dict:
        return {
            "version_id": self.version_id,
            "project": self.project,
            "author": self.author,
            "machine_id": self.machine_id,
            "created": self.created,
            "parent": self.parent,
            "message": self.message,
            "drp_name": self.drp_name,
            "media": [m.to_dict() for m in self.media],
            "media_roots": self.media_roots,
        }

    @classmethod
    def from_manifest(cls, d: dict) -> "Version":
        return cls(
            version_id=d["version_id"],
            project=d["project"],
            author=d.get("author", ""),
            machine_id=d.get("machine_id", ""),
            created=d.get("created", ""),
            parent=d.get("parent"),
            message=d.get("message", ""),
            drp_name=d.get("drp_name", "project.drp"),
            media=[MediaFingerprint.from_dict(m) for m in d.get("media", [])],
            media_roots=d.get("media_roots", []),
        )


def compute_version_id(drp: Path) -> str:
    """Content-address a version by the sha1 of its .drp (first 16 hex)."""
    h = hashlib.sha1()
    with drp.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


class Store:
    def __init__(self, root: Path):
        self.root = root

    def _project_dir(self, project: str) -> Path:
        """Directory for a project, tolerating stores written by older builds.

        _safe() gained a collision-hash suffix, so a store created before that
        change uses the bare sanitized name. Falling back to it keeps existing
        history readable — and keeps writing to the same directory, so a
        project's history never splits across two folders.
        """
        current = self.root / _safe(project)
        if current.exists():
            return current
        legacy = self.root / _safe_legacy(project)
        if legacy.exists():
            return legacy
        return current

    # --- writing ---------------------------------------------------------
    def put(self, version: Version, drp_src: Path) -> Version:
        """Write a version. Idempotent: re-pushing identical content is a no-op.

        Without the idempotence guard, a second push of unchanged content
        rewrites the manifest with parent == version_id — the version becomes
        its own parent and the original commit message is destroyed.
        """
        vdir = self._project_dir(version.project) / version.version_id
        manifest_path = vdir / "version.json"
        if manifest_path.is_file():
            existing = self.get_version(version.project, version.version_id)
            if existing:
                return existing

        vdir.mkdir(parents=True, exist_ok=True)
        try:
            # Constant filename: a Resolve project named "Ep 01: Rough Cut" would
            # otherwise write into an NTFS alternate data stream — a 0-byte file
            # to every peer, while the local size/checksum checks still pass.
            dest = vdir / DRP_FILENAME
            if not dest.is_file():  # content-addressed, so an existing file is identical
                shutil.copy2(drp_src, dest)
            manifest = dict(version.to_manifest())
            manifest["drp_name"] = DRP_FILENAME
            # Write-then-rename so a reader never observes a half-written manifest.
            tmp = vdir / ".version.json.part"
            tmp.write_text(json.dumps(manifest, indent=2), "utf-8")
            os.replace(tmp, manifest_path)
        except OSError:
            # Don't leave an empty version directory behind to replicate to peers.
            if not manifest_path.is_file():
                shutil.rmtree(vdir, ignore_errors=True)
            raise
        return version

    # --- reading ---------------------------------------------------------
    def list_projects(self) -> list[str]:
        """Real PROJECT NAMES, not directory names.

        Directories are named by _safe(), which appends a collision hash — so
        returning the directory name would break the round-trip every caller
        makes: list_projects() -> list_versions(name). The true name lives in
        each manifest, so read it and fall back to the directory only if no
        manifest is readable.

        Also skips the dot-dirs and conflict copies sync clients leave behind
        (.dropbox.cache, .tmp.drivedownload, .stfolder, "... (1)").
        """
        if not self.root.exists():
            return []
        names: list[str] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or _is_conflict_artifact(d.name):
                continue
            name = self._name_from_manifest(d) or d.name
            if name not in names:
                names.append(name)
        return sorted(names)

    def _name_from_manifest(self, project_dir: Path) -> str | None:
        for vdir in project_dir.iterdir():
            manifest = vdir / "version.json"
            if not manifest.is_file():
                continue
            try:
                return json.loads(manifest.read_text("utf-8")).get("project")
            except (OSError, ValueError):
                continue
        return None

    def list_versions(self, project: str, with_quarantine: bool = False):
        """Versions that are complete and self-consistent, newest first.

        Skips (rather than raises on) anything malformed: one unreadable or
        half-synced manifest must never take down an entire project's history.
        A version is admitted only if its .drp is actually present and non-empty
        — syncers transfer files independently and schedule small files first, so
        "manifest arrived, payload didn't" is the common receive state.
        """
        pdir = self._project_dir(project)
        if not pdir.exists():
            return ([], []) if with_quarantine else []
        versions: list[Version] = []
        quarantined: list[dict] = []
        for vdir in pdir.iterdir():
            if not vdir.is_dir() or vdir.name.startswith(".") or _is_conflict_artifact(vdir.name):
                continue
            manifest = vdir / "version.json"
            if not manifest.is_file():
                continue
            try:
                v = Version.from_manifest(json.loads(manifest.read_text("utf-8")))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                quarantined.append({"dir": vdir.name, "reason": f"unreadable manifest: {exc}"})
                continue
            if v.version_id != vdir.name:
                # A conflicted copy of a DIRECTORY carries a valid manifest and
                # would otherwise appear as a phantom duplicate of a real version.
                quarantined.append({"dir": vdir.name, "reason": "version_id/dir mismatch"})
                continue
            drp = vdir / (v.drp_name or DRP_FILENAME)
            try:
                if not drp.is_file() or drp.stat().st_size == 0:
                    quarantined.append({"dir": vdir.name, "reason": "payload not synced yet"})
                    continue
            except OSError as exc:
                quarantined.append({"dir": vdir.name, "reason": f"payload unreadable: {exc}"})
                continue
            versions.append(v)
        versions.sort(key=lambda v: (v.created, v.version_id), reverse=True)
        return (versions, quarantined) if with_quarantine else versions

    def heads(self, project: str) -> list[Version]:
        """All topological heads: versions that are nobody else's parent.

        Derived from the recorded parent pointers, NOT from client wall-clock
        timestamps — with a skewed clock the timestamp sort reports a single
        head and hides a genuine fork. Two or more heads means a real fork.
        """
        versions = self.list_versions(project)
        parents = {v.parent for v in versions if v.parent and v.parent != v.version_id}
        tips = [v for v in versions if v.version_id not in parents]
        return tips or versions[:1]

    def get_version(self, project: str, version_id: str) -> Version | None:
        manifest = self._project_dir(project) / version_id / "version.json"
        if manifest.is_file():
            return Version.from_manifest(json.loads(manifest.read_text("utf-8")))
        return None

    def drp_path(self, project: str, version_id: str) -> Path:
        v = self.get_version(project, version_id)
        name = (v.drp_name if v else None) or DRP_FILENAME
        return self._project_dir(project) / version_id / name

    def verify(self, project: str, version_id: str) -> bool:
        """version_id IS sha1(.drp)[:16], so integrity checking is one cheap hash."""
        p = self.drp_path(project, version_id)
        return p.is_file() and compute_version_id(p) == version_id

    def head(self, project: str) -> Version | None:
        """The current tip, chosen topologically rather than by client clock.

        Sorting by the client-supplied `created` field would make is_fork()
        clock-dependent: a machine with a fast clock could report a stale
        version as the tip.
        """
        tips = self.heads(project)
        return tips[0] if tips else None

    def is_fork(self, project: str, parent: str | None) -> bool:
        """A push forks history if the store's head isn't the push's parent."""
        head = self.head(project)
        if head is None:
            return False
        return head.version_id != parent


def _safe_legacy(name: str) -> str:
    """The pre-collision-hash naming. Read-only compatibility — never write new
    directories with this."""
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip() or "project"


def _safe(name: str) -> str:
    """Filesystem-safe directory name for a project.

    NFC-normalized so macOS (NFD) and Windows (NFC) agree on names like 'Café',
    and suffixed with a short hash of the original so that distinct names which
    sanitize identically ('Ep 01/Rough' vs 'Ep 01_Rough') don't interleave their
    histories in one directory.
    """
    name = unicodedata.normalize("NFC", name)
    cleaned = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip(" .")
    if not cleaned:
        cleaned = "project"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{cleaned}-{digest}"

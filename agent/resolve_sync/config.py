"""Configuration + local state for the agent.

Everything the agent needs to know about *this* machine lives here:
  - where the shared version store (the "repository") is
  - which folders on this machine hold media (the media roots)
  - a stable identity for this workstation / user
  - per-project sync bookkeeping (which version we last synced from)

Config is a small JSON file so it is easy to inspect and edit by hand.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _default_config_dir() -> Path:
    # Keep our config next to Resolve's own support data on Windows,
    # fall back to a dotfolder elsewhere.
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ResolveSync"
    return Path.home() / ".resolve-sync"


CONFIG_DIR = Path(os.environ.get("RESOLVE_SYNC_HOME", _default_config_dir()))
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"


@dataclass
class Config:
    # Which backend the version store uses:
    #   "git"    — a private GitHub/GitLab repo (recommended). Gives atomic
    #              fork rejection and real publish confirmation.
    #   "folder" — a plain directory; point it at a Drive/Dropbox synced folder
    #              or a NAS share. Simpler, but a push cannot confirm it
    #              actually reached anyone.
    backend: str = "folder"

    # Folder backend: where the store lives.
    # Git backend: the local working clone (any scratch dir).
    store_path: str = ""

    # Git backend only: the remote to push to. Use an SSH remote, e.g.
    #   git@github.com:you/resolve-sync-projects.git
    # Never embed a token in the URL (https://<token>@github.com/...): this file
    # is plaintext, and git would copy the credential into .git/config as well.
    git_remote: str = ""

    # Folders on THIS machine that the agent may scan to locate media.
    media_roots: list[str] = field(default_factory=list)

    # Projects the user has chosen to keep synced. Persisted so "push all" means
    # the same set every time rather than whatever happens to be selected.
    synced_projects: list[str] = field(default_factory=list)

    # Background auto-sync: push a synced project shortly after Resolve saves it,
    # and flag when another machine has newer work.
    auto_sync: bool = False
    auto_sync_interval: int = 90   # seconds between checks (minimum 15)

    # Auto-pull, fetch half: when another machine's push is detected, download
    # and verify it in the background so "get the newer version" is instant.
    # Importing into Resolve always stays a user action — see autosync.stage_head.
    auto_pull: bool = True

    # Google sign-in (drive backend). The client id/secret identify the APP, not
    # the user — Google treats desktop-app secrets as non-confidential and
    # expects them to ship inside the application. The user's own token lives in
    # the OS credential store, never here.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Human-friendly identity attached to every version this machine pushes.
    author: str = ""
    machine_id: str = ""

    # localhost bind for the agent's API + dashboard.
    host: str = "127.0.0.1"
    port: int = 7788

    def __post_init__(self) -> None:
        if not self.machine_id:
            self.machine_id = uuid.uuid4().hex[:12]
        if not self.author:
            self.author = os.environ.get("USERNAME") or socket.gethostname()

    # --- persistence -----------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})
        cfg = cls()
        cfg.save()
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), "utf-8")

    @property
    def store(self) -> Path | None:
        return Path(self.store_path) if self.store_path else None

    @property
    def roots(self) -> list[Path]:
        return [Path(r) for r in self.media_roots if r]


class SyncState:
    """Tracks, per store and project, which version this machine last synced from.

    This is what turns a flat list of pushes into a real history: a push records
    its *parent*, so two people editing the same version produce a visible fork
    instead of a silent overwrite.

    Pointers are namespaced BY STORE. A parent id only means something relative
    to the store it came from, so sharing one namespace across backends makes
    every push after a backend switch look like a fork against a version the new
    store has never heard of.
    """

    def __init__(self, store_id: str = "") -> None:
        self.store_id = store_id or "default"
        self._data: dict[str, dict[str, str]] = {}
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            # Migrate the old flat {project: version} layout into the default
            # namespace rather than discarding a user's sync history.
            if raw and all(isinstance(v, str) for v in raw.values()):
                self._data = {"default": raw}
            elif isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}

    def use_store(self, store_id: str) -> None:
        self.store_id = store_id or "default"

    def _ns(self) -> dict[str, str]:
        return self._data.setdefault(self.store_id, {})

    def parent_of(self, project: str) -> str | None:
        return self._ns().get(project)

    def set_parent(self, project: str, version_id: str) -> None:
        self._ns()[project] = version_id
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self._data, indent=2), "utf-8")

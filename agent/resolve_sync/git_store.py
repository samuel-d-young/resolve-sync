"""Git-backed version store — sync through a private GitHub/GitLab repo.

Why git: this project's data model is already a content-addressed DAG with
parent pointers and forks. That *is* git. Using a real git remote buys two
guarantees the folder store fundamentally cannot provide:

  1. ATOMIC COMPARE-AND-SET. A push from a stale parent is rejected by the
     remote (non-fast-forward). Two machines can never silently clobber each
     other — the loser is told, and its work is preserved on a fork branch.
  2. REAL PUBLISH CONFIRMATION. `put()` returning means the remote accepted the
     bytes, not merely that a local disk write succeeded. A paused/quota-exceeded
     cloud-sync folder reports success forever; a failed git push raises.

It also removes whole bug classes for free: history order is topological (no
client-clock skew), and there is no manifest-scanning to be broken by a
half-synced or malformed file.

Layout in the repo — ONE BRANCH PER PROJECT (`rs/<slug>`), and the .drp lives at
a CONSTANT path so git history *is* the versioning:

    project.drp     <- always this exact name (never derived from the project name)
    version.json    <- manifest for this commit

Committing a <version_id>/ directory per version instead would materialize every
version in the working tree forever — measured ~14x the disk for zero benefit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import unicodedata
from pathlib import Path

from .media import MediaFingerprint
from .store import Version, compute_version_id

# In a windowed (--noconsole / pythonw) build every subprocess would otherwise
# flash a black console window on screen. CREATE_NO_WINDOW suppresses it.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# `* -text` disables all CRLF translation. Without it the same commit checks out
# with different bytes on Windows vs macOS, which breaks any byte comparison.
_GITATTRIBUTES = "* -text\n"


class GitError(RuntimeError):
    """A git command failed. `rejected` marks the compare-and-set fork case."""

    def __init__(self, message: str, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


# One working clone is shared by every caller — HTTP handlers and the auto-sync
# thread alike — and every operation checks out a branch and rewrites the tree.
# Without serialising, two concurrent pushes race: at best git fails with a
# confusing error, at worst one thread wipes the worktree while the other is
# staging, committing a partial tree. Locks are per-workdir so unrelated stores
# never block each other. Reentrant, because public methods call each other.
_WORKDIR_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _workdir_lock(workdir: Path) -> threading.RLock:
    key = str(Path(workdir).resolve()).lower()
    with _LOCKS_GUARD:
        lock = _WORKDIR_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WORKDIR_LOCKS[key] = lock
        return lock


def _slug(name: str) -> str:
    """A safe git ref component for a Resolve project name.

    Git refs forbid spaces, ~^:?*[, '..', leading/trailing '.', and '.lock'
    endings; Windows is case-insensitive, so we lowercase to avoid two branches
    that differ only in case. NFC-normalize first so macOS (NFD) and Windows
    (NFC) agree on 'Café'.
    """
    name = unicodedata.normalize("NFC", name)
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.").lower()
    slug = re.sub(r"-{2,}", "-", slug).replace("..", ".")
    if slug.endswith(".lock"):
        slug = slug[:-5] + "-lock"
    return slug or "project"


class GitStore:
    """Version store backed by a git remote. Implements the same interface as Store.

    Requires the `git` binary on PATH and a configured remote the agent can push
    to (SSH deploy key or a fine-grained PAT over HTTPS).
    """

    def __init__(self, workdir: Path, remote: str | None = None):
        self.workdir = Path(workdir)
        self.remote = remote
        self._lock = _workdir_lock(self.workdir)

    # --- plumbing --------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            rejected = "rejected" in err or "non-fast-forward" in err or "fetch first" in err
            raise GitError(f"git {' '.join(args)} failed: {err}", rejected=rejected)
        return proc

    def ensure_repo(self) -> None:
        """Initialize the working repo and its remote.

        .gitattributes is deliberately NOT written here: an untracked file in the
        worktree makes `git checkout -B <br> origin/<br>` abort ("untracked
        working tree files would be overwritten"), which would silently strand
        the agent on an empty history. It is written after checkout instead.
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not (self.workdir / ".git").is_dir():
            self._git("init", "-q")
            if self.remote:
                self._git("remote", "add", "origin", self.remote)

    def _write_gitattributes(self) -> None:
        """Pin `* -text` so checkouts are byte-identical across Windows/macOS."""
        ga = self.workdir / ".gitattributes"
        if not ga.is_file() or ga.read_text("utf-8") != _GITATTRIBUTES:
            ga.write_text(_GITATTRIBUTES, "utf-8")

    def _branch(self, project: str) -> str:
        return f"rs/{_slug(project)}"

    def _fetch(self, project: str) -> bool:
        """Fetch the project's branch. Returns False if it doesn't exist remotely."""
        if not self.remote:
            return False
        br = self._branch(project)
        proc = self._git("fetch", "-q", "origin", f"{br}:refs/remotes/origin/{br}", check=False)
        return proc.returncode == 0

    def _checkout(self, project: str) -> None:
        """Put the working tree on the project's branch, tracking the remote."""
        br = self._branch(project)
        existed = self._fetch(project)
        if existed:
            # Clear untracked files that would block the checkout (see ensure_repo).
            for p in self.workdir.iterdir():
                if p.name != ".git":
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            self._git("checkout", "-q", "-B", br, f"refs/remotes/origin/{br}")
        else:
            # The remote has no branch for this project yet.
            #
            # A genuinely new project must start with NO history. `checkout -B`
            # would branch from the CURRENT HEAD, so pushing project B right
            # after project A silently grafts A's commits onto B, and B's first
            # push is then rejected as a fork against A's version.
            #
            # A local branch may already exist here — either legitimate work that
            # failed to reach the remote, or contamination from that old bug. Keep
            # it only if its history actually belongs to THIS project.
            local = self._git("rev-parse", "--verify", "-q", f"refs/heads/{br}",
                              check=False).returncode == 0
            if local:
                self._clear_worktree()
                self._git("checkout", "-q", br)
                if self._head_project() == project:
                    self._write_gitattributes()
                    return                      # legitimate unpushed history
                self._git("checkout", "-q", "--detach")
                self._git("branch", "-qD", br)  # contaminated — discard it

            self._git("checkout", "-q", "--orphan", br)
            self._git("rm", "-rq", "--cached", ".", check=False)
            self._clear_worktree()
        self._write_gitattributes()

    def _clear_worktree(self) -> None:
        for p in self.workdir.iterdir():
            if p.name != ".git":
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    def _head_project(self) -> str | None:
        """Project name recorded at the current HEAD, or None if unreadable."""
        blob = self._git("show", "HEAD:version.json", check=False)
        if blob.returncode != 0:
            return None
        try:
            return json.loads(blob.stdout).get("project")
        except json.JSONDecodeError:
            return None

    # --- writing ---------------------------------------------------------
    def _head_version_id(self) -> str | None:
        """version_id recorded at the current HEAD, or None on an empty branch."""
        blob = self._git("show", "HEAD:version.json", check=False)
        if blob.returncode != 0:
            return None
        try:
            return json.loads(blob.stdout).get("version_id")
        except json.JSONDecodeError:
            return None

    def _locked_put(self, version: Version, drp_src: Path, allow_fork: bool = False) -> Version:
        """Commit and PUSH a version. Raises GitError(rejected=True) on a fork.

        COMPARE-AND-SET: the write is refused unless the remote tip is still the
        parent this version was derived from. Checking out the remote tip first
        would silently fast-forward the caller's work onto someone else's — which
        is exactly the silent-clobber this store exists to prevent — so the check
        happens against the *declared* parent, before anything is committed.
        The remote's own non-fast-forward rejection remains as a race backstop.
        """
        self.ensure_repo()
        self._checkout(version.project)

        remote_head = self._head_version_id()
        if remote_head == version.version_id:
            return version  # already published; idempotent re-push

        forked = remote_head is not None and remote_head != version.parent
        if forked and not allow_fork:
            raise GitError(
                f"Fork: the remote is at {remote_head} but your snapshot was taken "
                f"from {version.parent or 'an empty history'}. Pull that version "
                f"first, or re-push with allow_fork to keep this work on a branch.",
                rejected=True,
            )
        # A fork is preserved on its own branch — the main branch is never
        # clobbered, and no work is ever lost.
        target_branch = (
            f"rs/fork/{_slug(version.project)}/{version.machine_id}"
            if forked else self._branch(version.project)
        )

        # Constant filename — never derived from the Resolve project name, which
        # can contain ':' and would silently land in an NTFS alternate data stream.
        shutil.copy2(drp_src, self.workdir / "project.drp")
        manifest = dict(version.to_manifest())
        manifest["drp_name"] = "project.drp"
        (self.workdir / "version.json").write_text(
            json.dumps(manifest, indent=2), "utf-8"
        )

        self._git("add", "-A")
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return version  # nothing changed — idempotent re-push, no empty commit

        msg = version.message or f"Update {version.project}"
        self._git(
            "-c", "user.name=Resolve Sync",
            "-c", f"user.email={version.author or 'agent'}@resolve-sync.local",
            "commit", "-q", "-m", f"{msg}\n\nversion_id: {version.version_id}",
        )
        if self.remote:
            # No --force, ever. The remote's rejection is the backstop that makes
            # this safe against a race between the CAS check and the push.
            self._git("push", "-q", "origin", f"HEAD:{target_branch}")
        return version

    # --- reading ---------------------------------------------------------
    def _locked_list_projects(self) -> list[str]:
        """Real project names, read from each branch's manifest.

        The branch name is a slug ('demo-film'); the display name lives in
        version.json ('Demo Film'), so read it rather than showing the slug.
        """
        self.ensure_repo()
        if self.remote:
            self._git("fetch", "-q", "origin", check=False)
        proc = self._git("branch", "-a", "--format=%(refname)", check=False)
        names: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            ref = line.strip()
            # Project branches only — never the rs/fork/... preservation branches.
            m = re.search(r"refs/(?:heads|remotes/origin)/(rs/[^/]+)$", ref)
            if not m:
                continue
            slug = m.group(1)
            blob = self._git("show", f"{ref}:version.json", check=False)
            display = None
            if blob.returncode == 0:
                try:
                    display = json.loads(blob.stdout).get("project")
                except json.JSONDecodeError:
                    display = None
            names.add(display or slug.split("/", 1)[1])
        return sorted(names)

    def _locked_list_versions(self, project: str) -> list[Version]:
        """History in true topological order (newest first) — no client clocks."""
        self.ensure_repo()
        try:
            self._checkout(project)
        except GitError:
            return []
        proc = self._git("log", "--format=%H", check=False)
        versions: list[Version] = []
        for sha in (proc.stdout or "").split():
            blob = self._git("show", f"{sha}:version.json", check=False)
            if blob.returncode != 0:
                continue
            try:
                versions.append(Version.from_manifest(json.loads(blob.stdout)))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # never let one bad manifest kill the whole history
        return versions

    def remote_tips(self) -> dict[str, str]:
        """Project slug -> remote commit SHA for every project branch.

        ONE network round-trip for the whole library: ls-remote asks the server
        for ref names and SHAs without fetching any objects. The SHA is a
        change fingerprint — if it hasn't moved since last time, the branch
        head hasn't either, so the expensive head/manifest read can be skipped.
        """
        with self._lock:
            self.ensure_repo()
            if not self.remote:
                return {}
            proc = self._git("ls-remote", "origin", "refs/heads/rs/*", check=False)
            tips: dict[str, str] = {}
            for line in (proc.stdout or "").splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                sha, ref = parts
                slug = ref[len("refs/heads/rs/"):] if ref.startswith("refs/heads/rs/") else ""
                # rs/fork/... preservation branches are not project tips.
                if slug and "/" not in slug:
                    tips[slug] = sha
            return tips

    # --- public API: every entry point serialises on the shared working clone ---
    def put(self, version: Version, drp_src: Path, allow_fork: bool = False) -> Version:
        with self._lock:
            return self._locked_put(version, drp_src, allow_fork)

    def list_projects(self) -> list[str]:
        with self._lock:
            return self._locked_list_projects()

    def list_versions(self, project: str) -> list[Version]:
        with self._lock:
            return self._locked_list_versions(project)

    def drp_path(self, project: str, version_id: str) -> Path:
        with self._lock:
            return self._locked_drp_path(project, version_id)

    def get_version(self, project: str, version_id: str) -> Version | None:
        for v in self.list_versions(project):
            if v.version_id == version_id:
                return v
        return None

    def _locked_drp_path(self, project: str, version_id: str) -> Path:
        """Materialize that version's .drp to a temp file, verified by hash."""
        self.ensure_repo()
        self._checkout(project)
        proc = self._git("log", "--format=%H", check=False)
        for sha in (proc.stdout or "").split():
            blob = self._git("show", f"{sha}:version.json", check=False)
            if blob.returncode != 0:
                continue
            try:
                if json.loads(blob.stdout).get("version_id") != version_id:
                    continue
            except json.JSONDecodeError:
                continue
            # Materialize OUTSIDE the working tree. Writing into self.workdir
            # is unsafe: _checkout() clears untracked files, so any concurrent
            # read (the dashboard polls history) would delete this file between
            # here and the caller's import. Also give it a real, human filename
            # rather than a dot-prefixed one, which Windows treats as hidden.
            out_dir = Path(tempfile.mkdtemp(prefix="resolve-sync-"))
            out = out_dir / f"{_slug(project)}-{version_id[:8]}.drp"
            with out.open("wb") as fh:
                got = subprocess.run(
                    ["git", "show", f"{sha}:project.drp"],
                    cwd=str(self.workdir), stdout=fh, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                )
            if got.returncode != 0:
                raise GitError(f"could not extract .drp for {version_id}")
            # version_id IS sha1(.drp)[:16] — verification is free and catches
            # any truncated or corrupted transfer before Resolve ever sees it.
            if compute_version_id(out) != version_id:
                out.unlink(missing_ok=True)
                raise GitError(f"checksum mismatch for {version_id}; refusing to import")
            return out
        raise GitError(f"version {version_id} not found in {project}")

    def heads(self, project: str) -> list[Version]:
        """Topological tips: versions that are nobody else's parent.

        Git's own history is linear per branch, but a fork branch or a rebased
        peer can still produce two tips, and the UI must be able to say so.
        """
        versions = self.list_versions(project)
        parents = {v.parent for v in versions if v.parent and v.parent != v.version_id}
        tips = [v for v in versions if v.version_id not in parents]
        return tips or versions[:1]

    def head(self, project: str) -> Version | None:
        tips = self.heads(project)
        return tips[0] if tips else None

    def is_fork(self, project: str, parent: str | None) -> bool:
        """Advisory only — the real guarantee is the remote rejecting the push."""
        head = self.head(project)
        if head is None:
            return False
        return head.version_id != parent

"""Media fingerprinting and automatic relinking.

This is the piece that makes the tool worth using. A `.drp` references media by
absolute path; on another machine those paths are wrong. We fix that in up to
three passes (see `relink_project`):

  Level 1 — Path remap:   swap a known media-root prefix. Deterministic, instant.
  Level 2 — Fingerprint:  scan this machine's media roots and match by
                          name + size + partial hash, so footage is found even
                          if it was renamed or moved.
  Level 3 — Report:       whatever can't be found is surfaced as missing.

The Resolve API gives us exactly the two calls we need:
  MediaPoolItem.GetClipProperty("File Path")  -> where a clip thinks it lives
  MediaPool.RelinkClips([items], folder)      -> point clips at a new folder
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

# How much of each end of a file to hash. Cheap, but enough to distinguish
# distinct media while tolerating nothing (bytes must match exactly).
_HASH_CHUNK = 1024 * 1024  # 1 MiB


# ----------------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------------
@dataclass
class MediaFingerprint:
    """Identity of one media file, independent of where it lives."""

    clip_name: str          # Resolve clip name (for reporting)
    original_path: str      # path as stored in the pushed project
    name: str               # basename, lowercased for matching
    size: int               # bytes
    partial_hash: str       # sha1 of head+tail+size, or "" if unreadable

    def to_dict(self) -> dict:
        return {
            "clip_name": self.clip_name,
            "original_path": self.original_path,
            "name": self.name,
            "size": self.size,
            "partial_hash": self.partial_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MediaFingerprint":
        return cls(
            clip_name=d.get("clip_name", ""),
            original_path=d.get("original_path", ""),
            name=d.get("name", ""),
            size=int(d.get("size", 0)),
            partial_hash=d.get("partial_hash", ""),
        )

    @property
    def verifiable(self) -> bool:
        """False when we never managed to hash the source file.

        An unverifiable fingerprint can only ever be a *candidate* — matching it
        on name+size alone is how you relink to the wrong footage (camera cards
        reuse names like C0001.MP4, and same-size duplicates are common).
        """
        return bool(self.partial_hash)

    def matches(self, path: Path) -> bool:
        """Is the file at `path` definitely the media this fingerprint describes?

        Requires a hash match. Size alone is NOT sufficient evidence: if the
        source hash could not be read (locked file, unplugged drive, cloud
        placeholder), we report "unverified" rather than silently accepting a
        same-name/same-size impostor.
        """
        if not self.verifiable:
            return False
        try:
            if path.stat().st_size != self.size:
                return False
        except OSError:
            return False
        return partial_hash(path) == self.partial_hash

    def matches_unverified(self, path: Path) -> bool:
        """Weak name+size candidacy, for clips we could not hash. Never auto-linked."""
        try:
            return path.stat().st_size == self.size
        except OSError:
            return False


def partial_hash(path: Path) -> str:
    """sha1 over the first and last `_HASH_CHUNK` bytes plus the size."""
    try:
        size = path.stat().st_size
        h = hashlib.sha1()
        h.update(str(size).encode())
        with path.open("rb") as f:
            h.update(f.read(_HASH_CHUNK))
            if size > _HASH_CHUNK:
                f.seek(max(0, size - _HASH_CHUNK))
                h.update(f.read(_HASH_CHUNK))
        return h.hexdigest()
    except OSError:
        return ""


def fingerprint_path(clip_name: str, file_path: str) -> MediaFingerprint | None:
    """Build a fingerprint from a clip's on-disk file. None if the file is gone."""
    p = Path(file_path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
    except OSError:
        return None
    return MediaFingerprint(
        clip_name=clip_name,
        original_path=file_path,
        name=p.name.lower(),
        size=size,
        partial_hash=partial_hash(p),
    )


# ----------------------------------------------------------------------------
# Local media index (Level 2 discovery)
# ----------------------------------------------------------------------------
class MediaIndex:
    """An index of media found under this machine's roots, keyed by name AND size.

    Built lazily and only over the configured roots so scans stay bounded. The
    dual index is what makes discovery robust: name lookup handles the common
    case cheaply, and a size+hash fallback finds files that were *renamed*.
    """

    def __init__(self, roots: list[Path]):
        self.roots = [r for r in roots if r.exists()]
        self._by_name: dict[str, list[Path]] = {}
        self._by_size: dict[int, list[Path]] = {}
        self._built = False

    def build(self) -> None:
        self._by_name.clear()
        self._by_size.clear()
        for root in self.roots:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    path = Path(dirpath) / fn
                    self._by_name.setdefault(fn.lower(), []).append(path)
                    try:
                        self._by_size.setdefault(path.stat().st_size, []).append(path)
                    except OSError:
                        pass
        self._built = True

    def find(self, fp: MediaFingerprint) -> Path | None:
        """Locate the file for a fingerprint, hash-confirmed.

        1. by name, confirmed by size + hash — fast, the common case.
        2. by size, confirmed by hash — catches renamed files.

        Returns None for an unverifiable fingerprint; use `find_candidates`
        to offer the user a manual choice instead of guessing.
        """
        if not self._built:
            self.build()
        if not fp.verifiable:
            return None
        for candidate in self._by_name.get(fp.name, []):
            if fp.matches(candidate):
                return candidate
        for candidate in self._by_size.get(fp.size, []):  # rename-tolerant
            if partial_hash(candidate) == fp.partial_hash:
                return candidate
        return None

    def find_candidates(self, fp: MediaFingerprint, limit: int = 5) -> list[Path]:
        """Weak name/size matches for a clip we could not hash — for manual review."""
        if not self._built:
            self.build()
        seen: list[Path] = []
        for candidate in self._by_name.get(fp.name, []) + self._by_size.get(fp.size, []):
            if candidate not in seen and fp.matches_unverified(candidate):
                seen.append(candidate)
            if len(seen) >= limit:
                break
        return seen


# ----------------------------------------------------------------------------
# Relink orchestration
# ----------------------------------------------------------------------------
@dataclass
class RelinkReport:
    remapped: list[str]     # clip names fixed by Level 1 (path remap)
    discovered: list[str]   # clip names fixed by Level 2 (hash-confirmed discovery)
    missing: list[str]      # clip names we could not locate at all
    # Clips whose source could not be hashed. We refuse to auto-link these —
    # name+size alone is not proof — so they are surfaced for manual confirmation.
    unverified: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "remapped": self.remapped,
            "discovered": self.discovered,
            "missing": self.missing,
            "unverified": self.unverified,
            "found": len(self.remapped) + len(self.discovered),
            "missing_count": len(self.missing),
            "unverified_count": len(self.unverified),
        }


def _remap(original: str, sender_roots: list[str], local_roots: list[Path]) -> Path | None:
    """Level 1: replace a sender root prefix with each local root and test."""
    norm = original.replace("\\", "/")
    for sroot in sorted(sender_roots, key=len, reverse=True):
        sn = sroot.replace("\\", "/").rstrip("/")
        if norm.lower().startswith(sn.lower()):
            rel = norm[len(sn):].lstrip("/")
            for lroot in local_roots:
                candidate = lroot / rel
                if candidate.is_file():
                    return candidate
    return None


def relink_project(media_pool, fingerprints: list[MediaFingerprint],
                   sender_roots: list[str], local_roots: list[Path],
                   index: MediaIndex) -> RelinkReport:
    """Relink every clip in `media_pool` to media present on this machine.

    `media_pool` is the live Resolve MediaPool for the just-imported project.
    Returns a report of what was remapped, discovered, or left missing.
    """
    items_by_path = _index_pool_items(media_pool)
    fp_by_path = {fp.original_path.replace("\\", "/"): fp for fp in fingerprints}

    # Group relink targets by destination folder — RelinkClips takes a folder.
    relink_groups: dict[str, list] = {}
    report = RelinkReport(remapped=[], discovered=[], missing=[], unverified=[])

    for original, item in items_by_path.items():
        fp = fp_by_path.get(original)
        clip_name = fp.clip_name if fp else Path(original).name

        # Level 1 — deterministic path remap; safe without a hash because the
        # relative path under a known root is itself the evidence.
        found = _remap(original, sender_roots, local_roots)
        level = "remapped"
        # Level 2 — hash-confirmed discovery.
        if found is None and fp is not None:
            found = index.find(fp)
            level = "discovered"

        if found is None:
            # Never guess from name+size. Offer candidates for manual review.
            if fp is not None and not fp.verifiable:
                report.unverified.append({
                    "clip": clip_name,
                    "reason": "source file could not be hashed when pushed",
                    "candidates": [str(c) for c in index.find_candidates(fp)],
                })
            else:
                report.missing.append(clip_name)
            continue

        relink_groups.setdefault(str(found.parent), []).append(item)
        (report.remapped if level == "remapped" else report.discovered).append(clip_name)

    # Apply relinks, one call per destination folder.
    for folder, items in relink_groups.items():
        try:
            media_pool.RelinkClips(items, folder)
        except Exception:  # noqa: BLE001 - Resolve raises opaque errors
            for it in items:
                name = _safe_clip_name(it)
                for bucket in (report.remapped, report.discovered):
                    if name in bucket:
                        bucket.remove(name)
                report.missing.append(name)

    return report


def _index_pool_items(media_pool) -> dict[str, object]:
    """Walk every folder in the media pool -> {normalized file path: item}."""
    result: dict[str, object] = {}
    root = media_pool.GetRootFolder()

    def walk(folder):
        for clip in folder.GetClipList() or []:
            path = clip.GetClipProperty("File Path")
            if path:
                result[path.replace("\\", "/")] = clip
        for sub in folder.GetSubFolderList() or []:
            walk(sub)

    if root:
        walk(root)
    return result


def _safe_clip_name(item) -> str:
    try:
        return Path(item.GetClipProperty("File Path")).name
    except Exception:  # noqa: BLE001
        return "unknown"

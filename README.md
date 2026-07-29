# Resolve Sync

Self-hosted DaVinci Resolve project sync — **Git for your edit, Dropbox for your
media** — with **automatic media relinking**. A free alternative to Blackmagic
Cloud's sync for solo editors and small teams.

It does *not* upload your footage. It versions the tiny `.drp` project snapshot,
records a fingerprint of every media file, and on the other machine
**automatically finds and relinks** that media — even if it was renamed or moved.

## How it works

```
   Machine A                    Version Store                    Machine B
 ┌───────────┐               (NAS / Dropbox /                 ┌───────────┐
 │  Resolve  │                 mounted bucket)                │  Resolve  │
 └─────┬─────┘             ┌──────────────────┐               └─────┬─────┘
       │  ExportProject    │  ProjectName/     │   ImportProject +   │
   ┌───┴────┐   .drp +     │    <version>/     │   auto-relink   ┌───┴────┐
   │ Agent  ├─ fingerprints┤      project.drp  ├───────────────► │ Agent  │
   │(Python)│  ──push──►   │      version.json │   ◄──pull──     │(Python)│
   └───┬────┘              └──────────────────┘                 └───┬────┘
       │  localhost API + dashboard                                 │
   ┌───┴────┐                                                   ┌───┴────┐
   │ Browser│  ← you drive it here                              │ Browser│
   └────────┘                                                   └────────┘
```

The browser can't touch your disk or drive Resolve, so a small **local agent**
(this Python service) does the real work and serves a dashboard on
`http://127.0.0.1:7788`.

### Automatic media find (three levels)
1. **Path remap** — swap a known media-root prefix (`Z:\Footage` → `/Volumes/NAS/Footage`). Instant.
2. **Fingerprint discovery** — scan your media roots and match by name + size + partial hash, so footage is found even if renamed/moved.
3. **Report** — anything genuinely absent is surfaced as "missing" (a future proxy layer will fill these).

## Requirements
- **DaVinci Resolve _Studio_**, installed and **running**.
  > The external scripting API this tool depends on is **Studio-only**. Blackmagic's own
  > `Support/Developer/Scripting/README.txt` describes it as "the Scripting API for DaVinci
  > Resolve Studio", and since Resolve 19.1 the free edition's bridge no longer accepts
  > connections. On free Resolve this tool cannot connect — there is no workaround.
- In Resolve: **Preferences → System → General → External scripting using → Local**.
- Python 3.9+ (you can use your own; the agent adds Resolve's scripting module to the path automatically).

## Run

```bash
cd agent
python -m venv .venv
.venv\Scripts\activate       # Windows  (use: source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
python -m resolve_sync
```

Then open <http://127.0.0.1:7788> and:
1. **Setup** — set the shared **version store** folder (a NAS/Dropbox/Drive path both machines can reach), your **media roots**, and your name.
2. **Push** a snapshot of a project.
3. On the other machine, point its agent at the same store, then **Pull** — media relinks automatically.

## Backends: where versions are stored

Set `backend` in the config. Both implement the same interface, so you can switch
without touching anything else.

### `git` (recommended) — a private GitHub/GitLab repo, free
Your data model *is* a content-addressed DAG with parents and forks, which is
exactly what git is. Two guarantees a synced folder cannot give:

- **Atomic compare-and-set.** A push from a stale parent is *rejected*, so two
  machines can never silently clobber each other. Opt in with `allow_fork` and the
  losing snapshot is preserved on its own branch — nothing is lost.
- **Real publish confirmation.** `put()` returning means the remote accepted the
  bytes. A paused or quota-exceeded sync folder reports success forever.

It also removes bug classes for free: history is in true topological order (no
client-clock skew), and there are no manifests to be broken by a half-synced file.

```jsonc
{
  "backend": "git",
  "store_path": "C:/Users/you/AppData/Local/ResolveSync/repo",   // local working clone
  "git_remote": "git@github.com:you/resolve-sync-projects.git"   // private repo
}
```

Setup: create an **empty private repo**, then add a write-enabled **deploy key**
(one per machine) or a fine-grained PAT scoped to that repo. Needs `git` on PATH.

Storage: `.drp` is a ZIP, so deltas are weak — budget ~0.55x file size per version
(~950 versions of a 2 MB project inside GitHub's ~1 GB guidance). GitLab gives a
documented 10 GiB per project if you want more headroom. Git LFS is *not* needed.

### `folder` — a Drive / Dropbox / NAS folder
Point `store_path` at a synced folder and it just works. Simpler, and genuinely
free, but a push cannot confirm anything actually reached a peer.

> If you use **Google Drive**, set My Drive to **"Mirror files"**, not the default
> "Stream files". Streamed placeholders report `is_file() == True` and a full size
> while zero bytes are local.

**Rule for either backend: never put your media roots inside a cloud-synced
folder.** Fingerprinting reads both ends of every file, which would force the
provider to hydrate the entire library.

## Status
Implemented: export/import via the official scripting API, content-addressed
versioning, media fingerprinting with Level 1 + 2 auto-relink, the git backend with
atomic fork rejection, and a hardened folder backend. 16 regression tests, each
reproducing a defect that was demonstrated against the real code.

Roadmap: proxy generation & sync (remote/offline media), live PostgreSQL
collaboration mode over a tunnel, hosted dashboard.

## Tests

```bash
cd agent && .venv\Scripts\python.exe tests\test_hardening.py && .venv\Scripts\python.exe tests\test_git_store.py
```

## Layout
```
agent/
  resolve_sync/
    resolve_conn.py   # connect to Resolve's scripting API (cross-platform bootstrap)
    projects.py       # export/import .drp, collect media fingerprints
    media.py          # fingerprinting + Level 1/2 auto-relink
    store.py          # version store (folder-based; pluggable for S3)
    config.py         # machine config + per-project sync state
    server.py         # FastAPI: localhost API + dashboard
  static/             # dashboard (HTML/CSS/JS)
```

"""App-level credentials baked into the build.

The Google client ID/secret identify the APPLICATION, not the person using it.
Whoever builds and distributes Resolve Sync creates them ONCE; everybody who
receives the build just clicks "Sign in with Google" and never sees Google Cloud
Console.

Google treats the client secret of an installed/desktop app as non-confidential
and expects it to ship inside the application ("OAuth 2.0 for Mobile & Desktop
Apps"), so embedding it here is the documented pattern — it grants nothing on
its own. Every user's own token is separate and lives in their OS credential
store.

To set them, create ``google_client.json`` next to this file:

    {"client_id": "....apps.googleusercontent.com", "client_secret": "GOCSPX-..."}

It is picked up automatically by the build (see build-exe.bat) and is the
fallback whenever a machine has no client configured locally.
"""

from __future__ import annotations

import json
from pathlib import Path

_CLIENT_FILE = Path(__file__).resolve().parent / "google_client.json"


def bundled_google_client() -> tuple[str, str]:
    """(client_id, client_secret) shipped with this build, or ("", "")."""
    try:
        data = json.loads(_CLIENT_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(data.get("client_id", "")).strip(), str(data.get("client_secret", "")).strip()


def has_bundled_client() -> bool:
    cid, secret = bundled_google_client()
    return bool(cid and secret)

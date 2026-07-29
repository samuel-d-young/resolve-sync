"""Sign in with Google — OAuth 2.0 for a desktop app, no terminal required.

The agent already runs an HTTP server on localhost, so it can receive Google's
redirect itself: click a button, approve in the browser, done. No copy-pasting
codes, no keys, no config files.

Scope is deliberately ``drive.file`` — the app can only see files IT created.
It cannot read the user's existing Drive contents, which also keeps the app out
of Google's restricted-scope verification process.

Tokens are kept in the OS credential store (Windows Credential Manager via
keyring), never in the plaintext config.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"

_SERVICE = "ResolveSync"
_ACCOUNT = "google-refresh-token"


class GoogleAuthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Secret storage
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def save_refresh_token(token: str) -> None:
    kr = _keyring()
    if kr is None:
        raise GoogleAuthError(
            "Cannot store the Google sign-in securely: the 'keyring' package is "
            "missing. Refusing to write a token to a plaintext file."
        )
    kr.set_password(_SERVICE, _ACCOUNT, token)


def load_refresh_token() -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(_SERVICE, _ACCOUNT)
    except Exception:  # noqa: BLE001 - backend may be unavailable
        return None


def forget() -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(_SERVICE, _ACCOUNT)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
@dataclass
class GoogleClient:
    """Google OAuth client credentials.

    For an installed/desktop app Google treats the client secret as NOT
    confidential — it is expected to ship inside the application. See
    "OAuth 2.0 for Mobile & Desktop Apps".
    """

    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


_pending: dict[str, float] = {}   # state -> created_at, to reject stale callbacks


def build_auth_url(client: GoogleClient) -> tuple[str, str]:
    """Return (url, state). Send the user to `url`."""
    if not client.configured:
        raise GoogleAuthError("No Google client ID configured.")
    state = secrets.token_urlsafe(24)
    _pending[state] = time.time()
    params = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",     # we need a refresh token
        "prompt": "consent",          # ensure a refresh token is issued
        "state": state,
    }
    return f"{AUTH_URI}?{urllib.parse.urlencode(params)}", state


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise GoogleAuthError(f"Google rejected the request: {detail}") from exc
    except OSError as exc:
        raise GoogleAuthError(f"Could not reach Google: {exc}") from exc


def exchange_code(client: GoogleClient, code: str, state: str) -> str:
    """Swap the callback code for a refresh token, and store it."""
    created = _pending.pop(state, None)
    if created is None:
        raise GoogleAuthError("This sign-in link has expired. Please try again.")
    tokens = _post_form(TOKEN_URI, {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": client.redirect_uri,
    })
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise GoogleAuthError(
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and sign in again."
        )
    save_refresh_token(refresh)
    return refresh


_access_cache: dict[str, tuple[str, float]] = {}


def access_token(client: GoogleClient) -> str:
    """A valid access token, refreshed as needed."""
    cached = _access_cache.get("t")
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    refresh = load_refresh_token()
    if not refresh:
        raise GoogleAuthError("Not signed in to Google.")
    tokens = _post_form(TOKEN_URI, {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    })
    tok = tokens.get("access_token")
    if not tok:
        raise GoogleAuthError("Google did not return an access token.")
    _access_cache["t"] = (tok, time.time() + int(tokens.get("expires_in", 3600)))
    return tok


def signed_in() -> bool:
    return bool(load_refresh_token())


def account_email(client: GoogleClient) -> str | None:
    """Whose account is signed in — shown in the UI so it's never ambiguous."""
    try:
        tok = access_token(client)
    except GoogleAuthError:
        return None
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {tok}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("email")
    except Exception:  # noqa: BLE001 - userinfo needs a profile scope we may lack
        return None

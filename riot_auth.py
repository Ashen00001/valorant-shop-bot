"""
Riot Games web auth — no Valorant client or lockfile needed.
Uses OAuth2 PKCE browser flow (riot-client).
User logs in once via browser; refresh_token persists ~1 year.
"""
import re, json, secrets, hashlib, base64, os, urllib.parse, requests
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
AUTH_BASE     = "https://auth.riotgames.com"
ENTITLE_BASE  = "https://entitlements.auth.riotgames.com"

_HEADERS = {
    "Content-Type":    "application/json",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent":      "RiotClient/68.0.0.4940199.4789131 rso-auth (Windows;10;;Professional, x64)",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _pkce():
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge

def _exchange_code(code: str, verifier: str):
    """Exchange authorization code → (access_token, refresh_token)."""
    r = requests.post(
        f"{AUTH_BASE}/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  "http://localhost/redirect",
            "code_verifier": verifier,
        },
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        auth=("riot-client", ""),
        timeout=15,
    )
    if not r.ok:
        raise ValueError(f"Token exchange failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "access_token" not in data:
        raise ValueError(f"No access_token in token response: {data}")
    return data["access_token"], data.get("refresh_token", "")

def _entitlements(at: str) -> str:
    r = requests.post(
        f"{ENTITLE_BASE}/api/token/v1", json={},
        headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["entitlements_token"]

def _puuid(at: str) -> str:
    r = requests.get(
        f"{AUTH_BASE}/userinfo",
        headers={"Authorization": f"Bearer {at}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["sub"]


# ── public API ────────────────────────────────────────────────────────────────

def load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return {}

def save_accounts(accounts: dict):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))

def get_browser_login_url() -> tuple:
    """
    Returns (auth_url, verifier).
    Open auth_url in a browser — user logs in — browser redirects to
    http://localhost/redirect?code=... (will show an error page, that's fine).
    Pass the full redirect URL + verifier to complete_browser_login().
    """
    verifier, challenge = _pkce()
    nonce = secrets.token_hex(16)
    params = {
        "client_id":             "riot-client",
        "response_type":         "code",
        "redirect_uri":          "http://localhost/redirect",
        "scope":                 "openid link ban lol_region account",
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "nonce":                 nonce,
    }
    url = f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)
    return url, verifier

def complete_browser_login(redirect_url: str, verifier: str, region: str = "na") -> dict:
    """
    Finish login after user pastes the redirect URL.
    redirect_url should look like: http://localhost/redirect?code=...
    """
    m = re.search(r"[?&]code=([^&]+)", redirect_url)
    if not m:
        raise ValueError(
            "No 'code' found in that URL.\n"
            "Make sure you copied the full URL from the address bar."
        )
    code = m.group(1)
    at, rt = _exchange_code(code, verifier)
    return {
        "region":             region,
        "puuid":              _puuid(at),
        "access_token":       at,
        "entitlements_token": _entitlements(at),
        "refresh_token":      rt,
    }

def refresh(account: dict) -> dict:
    """
    Silently get a fresh access_token using the stored refresh_token.
    Raises ValueError if expired (~1 year). Re-run setup_account.py to fix.
    """
    rt = account.get("refresh_token", "")
    if not rt:
        raise ValueError("No refresh_token — run setup_account.py again.")

    r = requests.post(
        f"{AUTH_BASE}/token",
        data={"grant_type": "refresh_token", "refresh_token": rt},
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        auth=("riot-client", ""),
        timeout=15,
    )
    if not r.ok:
        raise ValueError(
            f"Token refresh failed {r.status_code} — "
            "session expired. Run setup_account.py again."
        )
    data = r.json()
    if "access_token" not in data:
        raise ValueError(f"No access_token in refresh response: {data}")

    at     = data["access_token"]
    new_rt = data.get("refresh_token", rt)
    return {
        **account,
        "access_token":       at,
        "entitlements_token": _entitlements(at),
        "refresh_token":      new_rt,
    }

def get_tokens(account: dict) -> tuple:
    """Returns (access_token, entitlements_token, puuid, updated_account)."""
    updated = refresh(account)
    return updated["access_token"], updated["entitlements_token"], updated["puuid"], updated

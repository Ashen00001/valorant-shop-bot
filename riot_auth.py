"""
Riot Games web auth — no Valorant client or lockfile needed.
Uses PKCE + authorization code flow (riot-client).
Stores refresh_token after first login; refreshes silently from then on.
Refresh tokens last ~1 year before re-login is required.
"""
import re, json, secrets, hashlib, base64, os, requests
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
AUTH_BASE     = "https://auth.riotgames.com"
ENTITLE_BASE  = "https://entitlements.auth.riotgames.com"

_HEADERS = {
    "Content-Type":    "application/json",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent":      "RiotClient/68.0.0.4940199.4789131 rso-auth (Windows;10;;Professional, x64)",
    "X-Riot-ClientPlatform": (
        "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0"
        "Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0"
        "IjogIlVua25vd24iDQp9"
    ),
    "X-Riot-ClientVersion": "release-08.07-shipping-9-2444158",
}

# MFA: store (session, region, code_verifier) between login() and login_mfa()
_mfa_sessions = {}


# ── helpers ───────────────────────────────────────────────────────────────────

def _pkce():
    """Return (verifier, challenge) for PKCE S256."""
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge

def _exchange_code(sess: requests.Session, code: str, verifier: str) -> str:
    """Exchange authorization code for access token."""
    r = sess.post(
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

def _init_session(sess: requests.Session, nonce: str, challenge: str):
    """POST to start PKCE auth session."""
    r = sess.post(f"{AUTH_BASE}/api/v1/authorization", json={
        "acr_values":           "urn:riot:bronze",
        "claims":               "",
        "client_id":            "riot-client",
        "code_challenge":       challenge,
        "code_challenge_method":"S256",
        "nonce":                nonce,
        "redirect_uri":         "http://localhost/redirect",
        "response_type":        "code",
        "scope":                "openid link ban lol_region account",
    }, headers=_HEADERS, timeout=15)
    if not r.ok:
        raise ValueError(f"Auth init failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("type") == "error":
        raise ValueError(f"Auth init error: {data.get('error')} — {data.get('error_description','')}")


# ── public API ────────────────────────────────────────────────────────────────

def load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return {}

def save_accounts(accounts: dict):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))

def login(username: str, password: str, region: str = "na") -> dict:
    """
    Login with credentials. Password is never stored — only refresh_token.
    Raises ValueError("MFA_REQUIRED") if 2FA enabled; call login_mfa() next.
    """
    sess     = requests.Session()
    nonce    = secrets.token_hex(16)
    verifier, challenge = _pkce()

    _init_session(sess, nonce, challenge)

    r = sess.put(f"{AUTH_BASE}/api/v1/authorization", json={
        "type":     "auth",
        "username": username,
        "password": password,
        "remember": True,
        "language": "en_US",
    }, headers=_HEADERS, timeout=15)
    if not r.ok:
        raise ValueError(f"Credential submit failed {r.status_code}: {r.text[:300]}")
    data = r.json()

    if data.get("type") == "multifactor":
        _mfa_sessions[username] = (sess, region, verifier)
        raise ValueError("MFA_REQUIRED")

    if data.get("type") == "error":
        raise ValueError(f"Login failed: {data.get('error')} — {data.get('error_description','')}")

    if data.get("type") != "response":
        raise ValueError(f"Unexpected response type: {data}")

    uri  = data["response"]["parameters"]["uri"]
    m    = re.search(r"code=([^&]+)", uri)
    if not m:
        raise ValueError(f"No code in redirect URI: {uri[:200]}")

    at, rt = _exchange_code(sess, m.group(1), verifier)
    return {
        "region":             region,
        "puuid":              _puuid(at),
        "access_token":       at,
        "entitlements_token": _entitlements(at),
        "refresh_token":      rt,
    }

def login_mfa(username: str, code: str) -> dict:
    """Submit 2FA code after login() raised MFA_REQUIRED."""
    if username not in _mfa_sessions:
        raise ValueError("No pending MFA session — call login() first")
    sess, region, verifier = _mfa_sessions.pop(username)

    r = sess.put(f"{AUTH_BASE}/api/v1/authorization", json={
        "type": "multifactor", "code": code, "rememberDevice": True,
    }, headers=_HEADERS, timeout=15)
    if not r.ok:
        raise ValueError(f"MFA submit failed {r.status_code}: {r.text[:300]}")
    data = r.json()

    if data.get("type") != "response":
        raise ValueError(f"MFA failed: {data}")

    uri = data["response"]["parameters"]["uri"]
    m   = re.search(r"code=([^&]+)", uri)
    if not m:
        raise ValueError(f"No code in MFA redirect: {uri[:200]}")

    at, rt = _exchange_code(sess, m.group(1), verifier)
    return {
        "region":             region,
        "puuid":              _puuid(at),
        "access_token":       at,
        "entitlements_token": _entitlements(at),
        "refresh_token":      rt,
    }

def refresh(account: dict) -> dict:
    """
    Get a new access token using stored refresh_token — no password.
    Raises ValueError if refresh_token expired (~1 yr). Re-run setup_account.py.
    """
    rt = account.get("refresh_token", "")
    if not rt:
        raise ValueError("No refresh_token stored — run setup_account.py again.")

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
            "session may be expired. Run setup_account.py again."
        )
    data = r.json()
    if "access_token" not in data:
        raise ValueError(f"No access_token in refresh response: {data}")

    at      = data["access_token"]
    new_rt  = data.get("refresh_token", rt)  # rotate if server sends a new one
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

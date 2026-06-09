"""
Riot Games web auth — no Valorant client or lockfile needed.
Stores cookies after first login; refreshes silently from then on.
Cookies last ~30 days before re-login is required.
"""
import re, json, secrets, requests
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
AUTH_BASE     = "https://auth.riotgames.com"
ENTITLE_BASE  = "https://entitlements.auth.riotgames.com"

# Riot blocks generic user agents — needs to look like the actual client
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

_mfa_sessions = {}  # username → (session, region) — alive during MFA flow


# ── internal helpers ──────────────────────────────────────────────────────────

def _parse_token(uri: str) -> str:
    m = re.search(r"access_token=([^&]+)", uri)
    if not m:
        raise ValueError(f"No access_token in redirect: {uri[:200]}")
    return m.group(1)

def _entitlements(at: str) -> str:
    r = requests.post(
        f"{ENTITLE_BASE}/api/token/v1", json={},
        headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["entitlements_token"]

def _puuid(at: str) -> str:
    r = requests.get(f"{AUTH_BASE}/userinfo",
                     headers={"Authorization": f"Bearer {at}"}, timeout=10)
    r.raise_for_status()
    return r.json()["sub"]


# ── public API ────────────────────────────────────────────────────────────────

def load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return {}

def save_accounts(accounts: dict):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))

def login(username: str, password: str, region: str = "na") -> dict:
    """
    Login with credentials. Never stores the password — only cookies.
    Raises ValueError("MFA_REQUIRED") if 2FA is on; call login_mfa() next.
    """
    sess = requests.Session()
    nonce = secrets.token_hex(16)

    _r0 = sess.post(f"{AUTH_BASE}/api/v1/authorization", json={
        "acr_values":           "",
        "claims":               "",
        "client_id":            "play-valorant-web-prod",
        "code_challenge":       "",
        "code_challenge_method":"",
        "nonce":                nonce,
        "redirect_uri":         "https://playvalorant.com/opt_auth",
        "response_type":        "token id_token",
        "scope":                "account openid",
    }, headers=_HEADERS, timeout=15)
    if not _r0.ok:
        raise ValueError(f"Auth init failed {_r0.status_code}: {_r0.text[:500]}")
    print(f"[debug] POST cookies: {list(sess.cookies.keys())}", flush=True)
    print(f"[debug] POST body: {_r0.text[:200]}", flush=True)

    r = sess.put(f"{AUTH_BASE}/api/v1/authorization", json={
        "type": "auth", "username": username, "password": password, "remember": True,
    }, headers=_HEADERS, timeout=15)
    if not r.ok:
        raise ValueError(f"Credential PUT failed {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    data = r.json()

    if data.get("type") == "multifactor":
        _mfa_sessions[username] = (sess, region)
        raise ValueError("MFA_REQUIRED")

    if data.get("type") != "response":
        raise ValueError(f"Login failed: {data.get('error', data)}")

    at = _parse_token(data["response"]["parameters"]["uri"])
    return {"region": region, "puuid": _puuid(at), "access_token": at,
            "entitlements_token": _entitlements(at),
            "cookies": {c.name: c.value for c in sess.cookies}}

def login_mfa(username: str, code: str) -> dict:
    """Submit 2FA code after login() raised MFA_REQUIRED."""
    if username not in _mfa_sessions:
        raise ValueError("No pending MFA session — call login() first")
    sess, region = _mfa_sessions.pop(username)

    r = sess.put(f"{AUTH_BASE}/api/v1/authorization", json={
        "type": "multifactor", "code": code, "rememberDevice": True,
    }, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("type") != "response":
        raise ValueError(f"MFA failed: {data}")

    at = _parse_token(data["response"]["parameters"]["uri"])
    return {"region": region, "puuid": _puuid(at), "access_token": at,
            "entitlements_token": _entitlements(at),
            "cookies": {c.name: c.value for c in sess.cookies}}

def refresh(account: dict) -> dict:
    """
    Get a new access token using stored cookies — no password.
    Raises ValueError if cookies expired (~30 days). User must re-run setup_account.py.
    """
    cookie_str = "; ".join(f"{k}={v}" for k, v in account["cookies"].items())
    r = requests.get(f"{AUTH_BASE}/authorize", params={
        "redirect_uri": "https://playvalorant.com/opt_auth",
        "client_id":    "play-valorant-web-prod",
        "response_type": "token id_token", "nonce": "1", "scope": "account openid",
    }, headers={**_HEADERS, "Cookie": cookie_str}, allow_redirects=False, timeout=15)

    if r.status_code != 303:
        raise ValueError(
            f"Cookie refresh returned HTTP {r.status_code} — "
            "session expired. Run setup_account.py again."
        )

    at = _parse_token(r.headers.get("Location", ""))
    new_cookies = {**account["cookies"], **{c.name: c.value for c in r.cookies}}
    return {**account, "access_token": at,
            "entitlements_token": _entitlements(at), "cookies": new_cookies}

def get_tokens(account: dict) -> tuple:
    """Returns (access_token, entitlements_token, puuid, updated_account)."""
    updated = refresh(account)
    return updated["access_token"], updated["entitlements_token"], updated["puuid"], updated

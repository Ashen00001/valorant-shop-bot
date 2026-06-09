#!/usr/bin/env python3
"""
Valorant Shop Bot — vacation edition
Posts daily shop to Discord. 30-min window to respond, then PC sleeps.
Wakes every 3h to re-check. Buy with: buy <name or #> → confirm
"""

import sys, os, re, time, json, subprocess, requests, urllib3
from datetime import datetime, timedelta, timezone
from pathlib import Path

urllib3.disable_warnings()

# Force UTF-8 output so emoji in log lines don't crash on Windows cp1252 consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ──────────────────────────────────────────────────────────────────────
LOCKFILE        = os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile")
RIOT_CLIENT     = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
REGION          = os.environ.get("RIOT_REGION", "na")
DISCORD_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL = os.environ.get("DISCORD_CHANNEL_ID")
WAIT_MINUTES    = int(os.environ.get("WAIT_MINUTES", "30"))
RECHECK_HOURS   = int(os.environ.get("RECHECK_HOURS", "3"))
SCRIPT_DIR      = Path(__file__).parent
STATE_FILE      = SCRIPT_DIR / "state.json"

VP_CURRENCY  = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
ITEM_TYPE_ID = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
DISCORD_API  = "https://discord.com/api/v10"
CLIENT_PLATFORM = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0K"
    "CSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxh"
    "dGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
)

# Rarity colors lifted directly from SkinPeek (embed.js) — contentTierUuid → color
RARITY_COLORS = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 0x5a9fe1,  # Select   — blue
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 0x009984,  # Deluxe   — teal
    "60bca009-4182-7998-dee7-b8a2558dc369": 0xd1538c,  # Premium  — pink
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 0xf9d563,  # Ultra    — gold
    "e046854e-406c-37f4-6607-19a9ba8426fc": 0xf99358,  # Exclusive— orange
}

_client_version = None  # cached after first fetch
_skins_cache    = None  # levelUUID → {name, icon, color}

# ── Logging ──────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Discord ─────────────────────────────────────────────────────────────────────
def d_headers():
    return {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}

def d_send(content="", embeds=None, embed=None):
    """Send a message. Pass embeds=[] for multiple, or embed={} for one."""
    if embed and not embeds:
        embeds = [embed]
    payload = {"embeds": embeds} if embeds else {"content": content}
    r = requests.post(f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages",
                      headers=d_headers(), json=payload)
    r.raise_for_status()
    return r.json()["id"]

def d_messages(after_id, limit=50):
    r = requests.get(f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages",
                     headers=d_headers(), params={"after": after_id, "limit": limit})
    if r.status_code == 200:
        data = r.json()
        return data if isinstance(data, list) else []
    return []

def d_bot_id():
    return requests.get(f"{DISCORD_API}/users/@me", headers=d_headers()).json()["id"]

# ── Valorant ────────────────────────────────────────────────────────────────────
def get_client_version():
    global _client_version
    if not _client_version:
        _client_version = (
            requests.get("https://valorant-api.com/v1/version", timeout=10)
            .json()["data"]["riotClientVersion"]
        )
        log(f"Client version: {_client_version}")
    return _client_version

def build_skins_cache():
    """Fetch all skins once from valorant-api.com and build a lookup by level UUID.
    This lets us get skin name, icon, and rarity color without one call per skin.
    """
    global _skins_cache
    if _skins_cache is not None:
        return _skins_cache
    log("Building skins cache from valorant-api.com...")
    r = requests.get("https://valorant-api.com/v1/weapons/skins?language=en-US", timeout=20)
    r.raise_for_status()
    _skins_cache = {}
    for skin in r.json()["data"]:
        name  = skin["displayName"]
        tier  = skin.get("contentTierUuid", "")
        color = RARITY_COLORS.get(tier, 0xFF4655)
        for level in skin.get("levels", []):
            icon = level.get("displayIcon") or (skin["levels"][0].get("displayIcon") if skin["levels"] else None)
            _skins_cache[level["uuid"]] = {"name": name, "icon": icon, "color": color}
    log(f"Skins cache built ({len(_skins_cache)} levels indexed)")
    return _skins_cache

def get_tokens():
    try:
        with open(LOCKFILE) as f:
            parts = f.read().strip().split(":")
        port, password = parts[2], parts[3]
        r = requests.get(
            f"https://127.0.0.1:{port}/entitlements/v1/token",
            auth=("riot", password), verify=False, timeout=5
        )
        if r.status_code == 200 and "accessToken" in r.json():
            d = r.json()
            return d["accessToken"], d["token"], d["subject"]
    except Exception:
        pass
    return None

def val_process_running():
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq VALORANT.exe"],
        capture_output=True, text=True
    )
    return "VALORANT.exe" in r.stdout

def launch_and_wait(timeout=1800):
    """Launch Valorant and wait up to 30 min for auth tokens.
    Kills stale crashed process first (Vanguard crashes it on sleep/wake).
    """
    killed = subprocess.run(
        ["taskkill", "/F", "/IM", "VALORANT.exe"],
        capture_output=True, text=True
    )
    if "SUCCESS" in killed.stdout:
        log("Killed stale VALORANT.exe (crash from previous sleep cycle)")

    log("Launching Valorant via Riot Client...")
    subprocess.Popen([RIOT_CLIENT, "--launch-product=valorant", "--launch-patchline=live"])

    start      = time.time()
    fired      = set()
    milestones = [
        (120,  "⏳ Still waiting on Valorant to open — may be downloading an update..."),
        (600,  "⏳ 10 min in. Big patch maybe. Still going."),
        (1200, "⏳ 20 min in. Giving up at 30 min if nothing happens."),
    ]

    while time.time() - start < timeout:
        t = get_tokens()
        if t:
            log(f"Valorant auth tokens acquired ({int(time.time() - start)}s elapsed)")
            return t
        elapsed = time.time() - start
        for secs, msg in milestones:
            if elapsed >= secs and secs not in fired:
                fired.add(secs)
                status = "VALORANT.exe is running but not ready" if val_process_running() else "game process not visible yet"
                log(f"Milestone {int(secs/60)} min: {status}")
                d_send(f"{msg}\n`{status}`")
        time.sleep(5)

    log("ERROR: Valorant did not launch within 30 min")
    d_send(f"❌ Valorant didn't launch in 30 min. Going to sleep — retrying in {RECHECK_HOURS}h.")
    schedule_recheck()
    sleep_pc(30)
    return None

def _vh(at, et):
    return {
        "Authorization":           f"Bearer {at}",
        "X-Riot-Entitlements-JWT":  et,
        "X-Riot-ClientPlatform":    CLIENT_PLATFORM,
        "X-Riot-ClientVersion":     get_client_version(),
        "Content-Type":            "application/json",
    }

def get_shop(at, et, puuid):
    """Returns (list of skin dicts, seconds_until_reset).
    Each skin dict has: name, vp, offer_id, icon, color.
    """
    log("Fetching shop from Riot API...")
    r = requests.post(
        f"https://pd.{REGION}.a.pvp.net/store/v3/storefront/{puuid}",
        headers=_vh(at, et), json={}, timeout=15
    )
    r.raise_for_status()
    panel     = r.json()["SkinsPanelLayout"]
    remaining = panel["SingleItemOffersRemainingDurationInSeconds"]

    cache = build_skins_cache()
    skins = []
    for offer in panel["SingleItemStoreOffers"]:
        oid       = offer["OfferID"]
        vp        = offer["Cost"].get(VP_CURRENCY, 0)
        skin_data = cache.get(oid, {})
        name      = skin_data.get("name", oid)
        icon      = skin_data.get("icon")
        color     = skin_data.get("color", 0xFF4655)
        skins.append({"name": name, "vp": vp, "offer_id": oid, "icon": icon, "color": color})
        log(f"  Skin: {name} ({vp} VP)")

    log(f"Shop fetched — resets in {fmt_time(remaining)}")
    return skins, remaining

def get_vp(at, et, puuid):
    r       = requests.get(f"https://pd.{REGION}.a.pvp.net/store/v1/wallet/{puuid}",
                           headers=_vh(at, et), timeout=10)
    balance = r.json()["Balances"].get(VP_CURRENCY, 0)
    log(f"VP balance: {balance}")
    return balance

def buy_skin(at, et, offer_id):
    log(f"Sending purchase request — offer ID: {offer_id}")
    r = requests.post(
        f"https://pd.{REGION}.a.pvp.net/store/v1/order/",
        headers=_vh(at, et),
        json={"OfferId": offer_id, "ItemTypeId": ITEM_TYPE_ID, "Quantity": 1},
        timeout=15
    )
    log(f"Purchase response: HTTP {r.status_code}")
    return r.status_code == 200, r.text

# ── State ────────────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        return {}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def clear_state():
    STATE_FILE.unlink(missing_ok=True)

def shop_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ── Power / scheduling ───────────────────────────────────────────────────────────
def schedule_recheck(hours=None):
    """Register a one-time WakeToRun task via PowerShell.
    schtasks /Create has no WakeToRun flag — must use PowerShell.
    """
    if hours is None:
        hours = RECHECK_HOURS
    wake   = datetime.now() + timedelta(hours=hours)
    script = str(SCRIPT_DIR / "run.ps1").replace("'", "''")
    ps_cmd = (
        f"$a = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"  -Argument '-NonInteractive -ExecutionPolicy Bypass -File \"{script}\"'; "
        f"$t = New-ScheduledTaskTrigger -Once -At '{wake.strftime('%H:%M')}'; "
        f"$s = New-ScheduledTaskSettingsSet -WakeToRun "
        f"  -ExecutionTimeLimit (New-TimeSpan -Hours 2) "
        f"  -MultipleInstances IgnoreNew; "
        f"$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        f"  -LogonType S4U -RunLevel Highest; "
        f"Register-ScheduledTask -TaskName 'ValorantShopBotRecheck' "
        f"  -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null; "
        f"Write-Output 'OK'"
    )
    r = subprocess.run(["powershell", "-NonInteractive", "-Command", ps_cmd],
                       capture_output=True, text=True)
    if "OK" in r.stdout:
        log(f"Recheck task scheduled for {wake.strftime('%H:%M')} (WakeToRun=True)")
    else:
        log(f"WARNING: recheck task may not have registered")
        log(f"  stdout: {r.stdout.strip()}")
        log(f"  stderr: {r.stderr.strip()}")

def delete_recheck_task():
    subprocess.run(
        ["powershell", "-NonInteractive", "-Command",
         "Unregister-ScheduledTask -TaskName 'ValorantShopBotRecheck' "
         "-Confirm:$false -ErrorAction SilentlyContinue"],
        capture_output=True
    )
    log("Deleted stale recheck task (if any)")

def sleep_pc(delay=10):
    """Suspend to RAM (S3). WakeToRun tasks can wake it back up.
    IMPORTANT: do NOT change to 'shutdown /s' — a powered-off PC cannot be
    woken by Task Scheduler, breaking the entire recheck system.
    sys.exit(0) fires after wake so this stale process doesn't clash with
    the new one Task Scheduler starts.
    """
    log(f"Sleeping in {delay}s — recheck scheduled")
    if delay > 0:
        time.sleep(delay)
    subprocess.run(["rundll32", "powrprof.dll,SetSuspendState", "0,1,0"])
    log("Resumed from sleep — exiting stale process")
    sys.exit(0)

# ── Embed builders ───────────────────────────────────────────────────────────────
def fmt_time(secs):
    h, r = divmod(max(0, int(secs)), 3600)
    return f"{h}h {r // 60}m" if h else f"{r // 60}m"

def shop_embeds(skins, remaining, vp, is_recheck):
    """Returns a list of embed dicts: one dark header + one per skin (SkinPeek style).
    Each skin embed gets its rarity color as the left border and the skin icon as thumbnail.
    """
    title = "🔫 Valorant Daily Shop" if not is_recheck else "🔫 Shop Reminder"
    header = {
        "description": (
            f"**{title}**\n"
            f"⏱ Resets in **{fmt_time(remaining)}**  ·  💰 **{vp} VP**\n\n"
            f"`buy <name or #>` to purchase  ·  `no` to skip"
        ),
        "color": 0x202225,
    }
    embeds = [header]
    for i, skin in enumerate(skins):
        e = {
            "title": f"`{i+1}.`  {skin['name']}",
            "description": f"**{skin['vp']} VP**",
            "color": skin["color"],
        }
        if skin.get("icon"):
            e["thumbnail"] = {"url": skin["icon"]}
        embeds.append(e)
    return embeds

def confirm_embed(skin, vp):
    """Confirmation embed with the skin's rarity color and icon."""
    e = {
        "title": "⚠️ Confirm Purchase",
        "description": (
            f"**{skin['name']}** — {skin['vp']} VP\n"
            f"Balance: **{vp} VP**\n\n"
            f"`confirm` to buy  ·  `cancel` to go back  ·  `no` to skip"
        ),
        "color": skin.get("color", 0xFFA500),
    }
    if skin.get("icon"):
        e["thumbnail"] = {"url": skin["icon"]}
    return e

def match_skin(q, skins):
    q = q.strip().lower()
    if q.isdigit():
        idx = int(q) - 1
        return skins[idx] if 0 <= idx < len(skins) else None
    return next((s for s in skins if q in s["name"].lower()), None)

def do_purchase(at, et, skin):
    log(f"Executing purchase: {skin['name']} ({skin['vp']} VP)")
    d_send(content="⏳ Purchasing...")
    ok, resp = buy_skin(at, et, skin["offer_id"])
    if ok:
        log("Purchase successful!")
        d_send(embeds=[{
            "title": "✅ Bought!",
            "description": f"**{skin['name']}** for **{skin['vp']} VP**. Enjoy!\nPC going to sleep.",
            "color": skin.get("color", 0x57F287),
            **({"thumbnail": {"url": skin["icon"]}} if skin.get("icon") else {}),
        }])
        clear_state()
        sleep_pc(30)
        return True
    else:
        log(f"Purchase FAILED — {resp[:300]}")
        d_send(content=f"❌ Purchase failed: `{resp[:200]}`\nTry `buy <skin>` again or `no` to skip.")
        return False

def go_sleep(state, reason=""):
    if reason:
        log(f"Going to sleep — {reason}")
        d_send(content=f"{reason} Checking again in **{RECHECK_HOURS}h** — going to sleep.")
    save_state(state)
    schedule_recheck()
    sleep_pc(10)

# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    log("=" * 55)
    log("Valorant Shop Bot starting up")
    log(f"Region: {REGION}  |  Window: {WAIT_MINUTES} min  |  Recheck: every {RECHECK_HOURS}h")
    log("=" * 55)

    # ── Startup checks ─────────────────────────────────────────────────────────
    if not DISCORD_TOKEN or not DISCORD_CHANNEL:
        log("FATAL: DISCORD_BOT_TOKEN and/or DISCORD_CHANNEL_ID env vars are not set.")
        log("Open run.ps1 and fill in both values.")
        sys.exit(1)

    if not Path(RIOT_CLIENT).exists():
        log(f"FATAL: Riot Client not found at: {RIOT_CLIENT}")
        log("Update the RIOT_CLIENT path in bot.py to match your install location.")
        sys.exit(1)

    try:
        bot_id = d_bot_id()
        log(f"Discord connection OK — bot ID: {bot_id}")
    except Exception as e:
        log(f"FATAL: Could not reach Discord API — {e}")
        sys.exit(1)

    # ── Determine mode ─────────────────────────────────────────────────────────
    state      = load_state()
    today      = shop_day()
    is_recheck = state.get("day") == today
    log(f"Mode: {'RECHECK — same shop day' if is_recheck else 'FRESH START — new shop day'}")

    # ── Valorant auth ──────────────────────────────────────────────────────────
    log("Checking for existing Valorant session...")
    tokens = get_tokens()
    if tokens:
        log("Valorant already running — skipping launch")
    else:
        log("Valorant not running — launching now")
        tokens = launch_and_wait()
    if not tokens:
        return
    at, et, puuid = tokens
    log(f"Authenticated — PUUID: {puuid[:8]}...")

    # ── Fetch shop ─────────────────────────────────────────────────────────────
    try:
        skins, remaining = get_shop(at, et, puuid)
        vp               = get_vp(at, et, puuid)
    except Exception as e:
        log(f"ERROR fetching shop: {e}")
        fresh_state = state if is_recheck else {"day": today}
        d_send(content=f"❌ Failed to fetch shop: `{e}`\nGoing to sleep — retrying in {RECHECK_HOURS}h.")
        go_sleep(fresh_state)
        return

    if not is_recheck:
        delete_recheck_task()
        state = {"day": today}

    # ── Scan Discord for commands sent while PC was sleeping (recheck only) ────
    pending = state.get("pending")
    last_id = state.get("last_msg_id")

    if last_id and is_recheck:
        missed = d_messages(last_id)
        log(f"Scanning {len(missed)} missed message(s)...")
        for msg in reversed(missed):
            if msg["author"]["id"] == bot_id:
                continue
            content = msg["content"].strip().lower()
            state["last_msg_id"] = msg["id"]
            log(f"  Missed: '{content}'")

            if pending:
                if content == "confirm":
                    log("Confirm found in missed messages — purchasing now")
                    if do_purchase(at, et, pending):
                        return
                    pending = None
                    state.pop("pending", None)
                elif content in ("cancel", "no"):
                    pending = None
                    state.pop("pending", None)
                    if content == "no":
                        go_sleep(state, "👋 Got it.")
                        return
            else:
                m = re.match(r"^buy\s+(.+)$", content)
                if m:
                    skin = match_skin(m.group(1), skins)
                    if skin:
                        log(f"Buy request in missed messages: {skin['name']}")
                        pending = skin
                        state["pending"] = skin
                elif content == "no":
                    go_sleep(state, "👋 Got it.")
                    return

        save_state(state)

    # ── Post to Discord ────────────────────────────────────────────────────────
    log("Posting to Discord...")
    if pending:
        vp     = get_vp(at, et, puuid)
        msg_id = d_send(embeds=[confirm_embed(pending, vp)])
        log(f"Confirmation embed posted (skin: {pending['name']})")
    else:
        msg_id = d_send(embeds=shop_embeds(skins, remaining, vp, is_recheck))
        log(f"Shop embeds posted (msg ID: {msg_id})")

    state["last_msg_id"] = msg_id
    save_state(state)

    # ── Poll for response ──────────────────────────────────────────────────────
    deadline = time.time() + WAIT_MINUTES * 60
    wake_at  = datetime.now() + timedelta(minutes=WAIT_MINUTES)
    log(f"Polling every 20s until {wake_at:%H:%M} ({WAIT_MINUTES} min window)...")

    while time.time() < deadline:
        time.sleep(20)
        msgs = d_messages(state["last_msg_id"])
        if not msgs:
            continue

        for msg in reversed(msgs):
            if msg["author"]["id"] == bot_id:
                continue
            content = msg["content"].strip().lower()
            state["last_msg_id"] = msg["id"]
            save_state(state)
            log(f"Incoming: '{content}'")

            if pending:
                if content == "confirm":
                    if do_purchase(at, et, pending):
                        return
                    pending = None
                    state.pop("pending", None)
                    save_state(state)
                elif content == "cancel":
                    log("Purchase cancelled")
                    pending = None
                    state.pop("pending", None)
                    save_state(state)
                    d_send(content="❌ Cancelled. Reply `buy <skin>` to pick something else.")
                elif content == "no":
                    go_sleep(state, "👋 Got it.")
                    return
            else:
                if content == "no":
                    go_sleep(state, "👋 Got it.")
                    return
                m = re.match(r"^buy\s+(.+)$", content)
                if m:
                    skin = match_skin(m.group(1), skins)
                    if skin:
                        log(f"Buy request: {skin['name']} ({skin['vp']} VP)")
                        pending = skin
                        state["pending"] = skin
                        vp  = get_vp(at, et, puuid)
                        cid = d_send(embeds=[confirm_embed(skin, vp)])
                        state["last_msg_id"] = cid
                        save_state(state)
                    else:
                        log(f"No match for '{m.group(1)}'")
                        d_send(content=f"❌ Couldn't find `{m.group(1)}` in today's shop.")

    log(f"No response in {WAIT_MINUTES} min — going to sleep")
    go_sleep(state, f"⏰ No response in {WAIT_MINUTES} min.")


if __name__ == "__main__":
    main()

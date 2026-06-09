#!/usr/bin/env python3
"""
Valorant Shop Bot — VPS / always-on edition
Multi-user Discord bot. Checks daily shop + night market, lets you buy remotely.

Commands (in the configured channel):
  shop / s          — your daily shop
  nm / nightmarket  — night market (if active)
  buy <name or #>   — start purchase (works for both shop and NM)
  confirm           — execute pending purchase
  cancel            — cancel pending purchase

Auto-posts your shop every day at midnight UTC (= 5 PM PDT for NA).
"""

import sys, os, re, time, json, threading, requests
from datetime import datetime, timezone
from pathlib import Path

import riot_auth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL = os.environ.get("DISCORD_CHANNEL_ID")
PREFIX          = os.environ.get("BOT_PREFIX", "!")   # bot ignores messages without this
SCRIPT_DIR      = Path(__file__).parent

VP_CURRENCY  = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
ITEM_TYPE_ID = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
DISCORD_API  = "https://discord.com/api/v10"
CLIENT_PLATFORM = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0K"
    "CSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxh"
    "dGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
)

RARITY_COLORS = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 0x5a9fe1,  # Select    — blue
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 0x009984,  # Deluxe    — teal
    "60bca009-4182-7998-dee7-b8a2558dc369": 0xd1538c,  # Premium   — pink
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 0xf9d563,  # Ultra     — gold
    "e046854e-406c-37f4-6607-19a9ba8426fc": 0xf99358,  # Exclusive — orange
}

_client_version = None
_skins_cache    = None
_accounts       = {}   # discord_user_id → account dict
_pending        = {}   # discord_user_id → skin dict (awaiting confirm)
_last_posted    = {}   # discord_user_id → UTC date string
_session_cache  = {}   # discord_user_id → {"shop": [...], "nm": [...]}


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Discord ───────────────────────────────────────────────────────────────────
def _dh():
    return {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}

def d_send(content="", embeds=None):
    payload = {"embeds": embeds} if embeds else {"content": content}
    r = requests.post(f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages",
                      headers=_dh(), json=payload)
    if r.status_code not in (200, 201):
        log(f"Discord error {r.status_code}: {r.text[:200]}")
        return None
    return r.json().get("id")

def d_messages(after_id=None, limit=50):
    params = {"limit": limit}
    if after_id:
        params["after"] = after_id
    r = requests.get(f"{DISCORD_API}/channels/{DISCORD_CHANNEL}/messages",
                     headers=_dh(), params=params)
    if r.status_code == 200:
        data = r.json()
        return data if isinstance(data, list) else []
    return []

def d_bot_id():
    return requests.get(f"{DISCORD_API}/users/@me", headers=_dh()).json()["id"]


# ── Valorant API ──────────────────────────────────────────────────────────────
def get_client_version():
    global _client_version
    if not _client_version:
        _client_version = (
            requests.get("https://valorant-api.com/v1/version", timeout=10)
            .json()["data"]["riotClientVersion"]
        )
    return _client_version

def build_skins_cache():
    global _skins_cache
    if _skins_cache is not None:
        return _skins_cache
    log("Building skins cache...")
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
    log(f"Skins cache ready ({len(_skins_cache)} levels)")
    return _skins_cache

def _vh(at, et):
    return {
        "Authorization":           f"Bearer {at}",
        "X-Riot-Entitlements-JWT":  et,
        "X-Riot-ClientPlatform":    CLIENT_PLATFORM,
        "X-Riot-ClientVersion":     get_client_version(),
        "Content-Type":            "application/json",
    }

def fetch_storefront(at, et, puuid, region):
    r = requests.post(
        f"https://pd.{region}.a.pvp.net/store/v3/storefront/{puuid}",
        headers=_vh(at, et), json={}, timeout=15,
    )
    r.raise_for_status()
    return r.json()

def parse_shop(storefront) -> tuple:
    """Returns (skins_list, seconds_until_reset)."""
    cache = build_skins_cache()
    panel = storefront["SkinsPanelLayout"]
    skins = []
    for offer in panel["SingleItemStoreOffers"]:
        oid = offer["OfferID"]
        sd  = cache.get(oid, {})
        skins.append({
            "name":     sd.get("name", oid),
            "vp":       offer["Cost"].get(VP_CURRENCY, 0),
            "offer_id": oid,
            "icon":     sd.get("icon"),
            "color":    sd.get("color", 0xFF4655),
            "is_nm":    False,
        })
    return skins, panel["SingleItemOffersRemainingDurationInSeconds"]

def parse_nm(storefront) -> tuple:
    """Returns (skins_list, seconds_until_end) or (None, None) if NM inactive."""
    bs     = storefront.get("BonusStore", {})
    offers = bs.get("BonusStoreOffers")
    if not offers:
        return None, None
    cache = build_skins_cache()
    skins = []
    for offer in offers:
        oid   = offer["Offer"]["OfferID"]   # skin level UUID — for cache lookup only
        b_oid = offer["BonusOfferID"]        # MUST use this for NM purchases (not OfferID)
        sd    = cache.get(oid, {})
        skins.append({
            "name":     sd.get("name", oid),
            "vp":       offer["DiscountCosts"].get(VP_CURRENCY, 0),
            "orig_vp":  offer["Offer"]["Cost"].get(VP_CURRENCY, 0),
            "disc_pct": offer.get("DiscountPercent", 0),
            "offer_id": b_oid,
            "icon":     sd.get("icon"),
            "color":    sd.get("color", 0xFF4655),
            "is_nm":    True,
        })
    return skins, bs.get("BonusStoreRemainingDurationInSeconds", 0)

def get_vp(at, et, puuid, region) -> int:
    r = requests.get(f"https://pd.{region}.a.pvp.net/store/v1/wallet/{puuid}",
                     headers=_vh(at, et), timeout=10)
    return r.json()["Balances"].get(VP_CURRENCY, 0)

def do_order(at, et, puuid, region, offer_id) -> tuple:
    r = requests.post(
        f"https://pd.{region}.a.pvp.net/store/v1/order/",
        headers=_vh(at, et),
        json={"OfferId": offer_id, "ItemTypeId": ITEM_TYPE_ID, "Quantity": 1},
        timeout=15,
    )
    return r.status_code == 200, r.text


# ── Account helpers ───────────────────────────────────────────────────────────
def load_all_accounts():
    global _accounts
    raw = riot_auth.load_accounts()
    for did, acct in raw.items():
        try:
            at, et, puuid, updated = riot_auth.get_tokens(acct)
            _accounts[did] = updated
            log(f"Account ready: Discord {did[:10]}... (puuid {puuid[:8]}...)")
        except Exception as e:
            log(f"WARNING: Could not refresh account {did}: {e}")

def get_tokens_for(discord_id):
    """Returns (at, et, account). Refreshes tokens and saves updated cookies."""
    acct = _accounts.get(discord_id)
    if not acct:
        return None, None, None
    try:
        at, et, puuid, updated = riot_auth.get_tokens(acct)
        _accounts[discord_id] = updated
        all_accts = riot_auth.load_accounts()
        all_accts[discord_id] = updated
        riot_auth.save_accounts(all_accts)
        return at, et, updated
    except ValueError as e:
        log(f"Auth expired for {discord_id}: {e}")
        d_send(content=f"<@{discord_id}> ⚠️ Your auth expired — ask the admin to re-run `setup_account.py`.")
        return None, None, None


# ── Embed builders ────────────────────────────────────────────────────────────
def fmt_time(secs):
    h, r = divmod(max(0, int(secs)), 3600)
    return f"{h}h {r // 60}m" if h else f"{r // 60}m"

def shop_embeds(skins, remaining, vp, mention=None):
    who = f"<@{mention}>'s " if mention else ""
    header = {
        "description": (
            f"**🔫 {who}Daily Shop**\n"
            f"⏱ Resets in **{fmt_time(remaining)}**  ·  💰 **{vp} VP**\n\n"
            f"`{PREFIX}buy <name or #>` to purchase  ·  `{PREFIX}nm` for night market"
        ),
        "color": 0x202225,
    }
    embeds = [header]
    for i, s in enumerate(skins):
        e = {"title": f"`{i+1}.`  {s['name']}", "description": f"**{s['vp']} VP**",
             "color": s["color"]}
        if s.get("icon"):
            e["thumbnail"] = {"url": s["icon"]}
        embeds.append(e)
    return embeds

def nm_embeds(skins, remaining, vp, mention=None):
    who = f"<@{mention}>'s " if mention else ""
    if not skins:
        return [{"description": f"🌙 {who}Night Market isn't active right now.", "color": 0x202225}]
    header = {
        "description": (
            f"**🌙 {who}Night Market**\n"
            f"⏱ Ends in **{fmt_time(remaining)}**  ·  💰 **{vp} VP**\n\n"
            f"`{PREFIX}buy nm1` / `{PREFIX}buy <name>` to purchase at the discounted price"
        ),
        "color": 0x202225,
    }
    embeds = [header]
    for i, s in enumerate(skins):
        e = {
            "title":       f"`NM{i+1}.`  {s['name']}",
            "description": f"~~{s['orig_vp']} VP~~ → **{s['vp']} VP**  (-{s['disc_pct']}%)",
            "color":       s["color"],
        }
        if s.get("icon"):
            e["thumbnail"] = {"url": s["icon"]}
        embeds.append(e)
    return embeds

def confirm_embed(skin, vp):
    if skin.get("is_nm"):
        price_line = f"~~{skin['orig_vp']} VP~~ → **{skin['vp']} VP** (-{skin['disc_pct']}%)"
    else:
        price_line = f"**{skin['vp']} VP**"
    e = {
        "title": "⚠️ Confirm Purchase",
        "description": (
            f"**{skin['name']}**\n"
            f"{price_line}\n"
            f"Balance: **{vp} VP**\n\n"
            f"`{PREFIX}confirm` to buy  ·  `{PREFIX}cancel` to abort"
        ),
        "color": skin.get("color", 0xFFA500),
    }
    if skin.get("icon"):
        e["thumbnail"] = {"url": skin["icon"]}
    return e


# ── Command handlers ──────────────────────────────────────────────────────────
def handle_shop(discord_id, auto=False):
    at, et, acct = get_tokens_for(discord_id)
    if not acct:
        if not auto:
            d_send(content=f"<@{discord_id}> Not set up — ask admin to run `setup_account.py`.")
        return
    try:
        sf               = fetch_storefront(at, et, acct["puuid"], acct["region"])
        skins, remaining = parse_shop(sf)
        nm_skins, nm_rem = parse_nm(sf)
        vp               = get_vp(at, et, acct["puuid"], acct["region"])

        _session_cache[discord_id] = {"shop": skins, "nm": nm_skins}
        _last_posted[discord_id]   = shop_day()

        mention = discord_id if len(_accounts) > 1 else None
        d_send(embeds=shop_embeds(skins, remaining, vp, mention))

        if nm_skins:
            d_send(embeds=nm_embeds(nm_skins, nm_rem, vp, mention))

        log(f"{'Auto-posted' if auto else 'Posted'} shop for {discord_id[:10]}... "
            f"({'NM active' if nm_skins else 'no NM'})")
    except Exception as e:
        log(f"Shop fetch failed for {discord_id}: {e}")
        if not auto:
            d_send(content=f"<@{discord_id}> ❌ Failed: `{e}`")

def handle_nm(discord_id):
    at, et, acct = get_tokens_for(discord_id)
    if not acct:
        d_send(content=f"<@{discord_id}> Not set up.")
        return
    try:
        sf               = fetch_storefront(at, et, acct["puuid"], acct["region"])
        nm_skins, nm_rem = parse_nm(sf)
        vp               = get_vp(at, et, acct["puuid"], acct["region"])

        cached = _session_cache.setdefault(discord_id, {})
        cached["nm"] = nm_skins

        mention = discord_id if len(_accounts) > 1 else None
        d_send(embeds=nm_embeds(nm_skins, nm_rem, vp, mention))
        log(f"NM posted for {discord_id[:10]}...")
    except Exception as e:
        log(f"NM fetch failed for {discord_id}: {e}")
        d_send(content=f"<@{discord_id}> ❌ Night market failed: `{e}`")

def handle_buy(discord_id, query):
    cached     = _session_cache.get(discord_id, {})
    shop_skins = cached.get("shop", [])
    nm_skins   = cached.get("nm") or []

    if not shop_skins and not nm_skins:
        d_send(content="Run `shop` first so I know what's in your store.")
        return

    skin = _match(query, shop_skins) or _match(query, nm_skins)
    if not skin:
        d_send(content=f"❌ No match for `{query}` — use `buy 2`, `buy nm3`, or partial name.")
        return

    at, et, acct = get_tokens_for(discord_id)
    if not at:
        return
    vp = get_vp(at, et, acct["puuid"], acct["region"])
    _pending[discord_id] = skin
    d_send(embeds=[confirm_embed(skin, vp)])
    log(f"Buy pending for {discord_id[:10]}...: {skin['name']} "
        f"({skin['vp']} VP, nm={skin['is_nm']})")

def handle_confirm(discord_id):
    skin = _pending.pop(discord_id, None)
    if not skin:
        d_send(content="Nothing pending — use `buy <name>` first.")
        return
    at, et, acct = get_tokens_for(discord_id)
    if not at:
        return
    d_send(content="⏳ Purchasing...")
    ok, resp = do_order(at, et, acct["puuid"], acct["region"], skin["offer_id"])
    if ok:
        log(f"Purchase OK: {skin['name']} for {discord_id[:10]}...")
        desc = f"**{skin['name']}** for **{skin['vp']} VP**"
        if skin.get("is_nm"):
            desc += f" (-{skin['disc_pct']}%)"
        d_send(embeds=[{
            "title": "✅ Bought!",
            "description": desc + "  enjoy!",
            "color": skin.get("color", 0x57F287),
            **({"thumbnail": {"url": skin["icon"]}} if skin.get("icon") else {}),
        }])
    else:
        log(f"Purchase FAILED: {resp[:200]}")
        d_send(content=f"❌ Purchase failed: `{resp[:200]}`\nTry `buy <skin>` again.")


# ── Misc helpers ──────────────────────────────────────────────────────────────
def shop_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _match(q, skins):
    """Match by number (1-based), nm-number (nm1...), or partial name."""
    q = q.strip().lower()
    idx_str = re.sub(r"^nm", "", q)
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        return skins[idx] if 0 <= idx < len(skins) else None
    return next((s for s in skins if q in s["name"].lower()), None)


# ── Auto-post (midnight UTC = shop reset for all regions) ─────────────────────
def auto_post_loop():
    log("Auto-post scheduler running (fires at 00:00 UTC daily)")
    while True:
        now   = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == 0 and now.minute < 10:
            for discord_id in list(_accounts.keys()):
                if _last_posted.get(discord_id) != today:
                    log(f"Auto-posting for {discord_id[:10]}...")
                    handle_shop(discord_id, auto=True)
                    time.sleep(3)
        time.sleep(60)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log("=" * 55)
    log("Valorant Shop Bot — VPS edition")
    log("=" * 55)

    if not DISCORD_TOKEN or not DISCORD_CHANNEL:
        log("FATAL: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set.")
        sys.exit(1)

    try:
        BOT_ID = d_bot_id()
        log(f"Discord OK — bot ID: {BOT_ID}")
    except Exception as e:
        log(f"FATAL: Discord connection failed: {e}")
        sys.exit(1)

    load_all_accounts()
    if not _accounts:
        log("WARNING: No accounts. Run: python setup_account.py <discord_user_id> [region]")
    else:
        log(f"{len(_accounts)} account(s) loaded")

    build_skins_cache()

    threading.Thread(target=auto_post_loop, daemon=True).start()

    # Start from latest message so we don't replay history on startup
    msgs    = d_messages(limit=1)
    last_id = msgs[0]["id"] if msgs else "0"
    log(f"Polling every 5s (after msg {last_id})")

    while True:
        try:
            msgs = d_messages(after_id=last_id)
        except Exception as e:
            log(f"Poll error: {e}")
            time.sleep(10)
            continue

        for msg in reversed(msgs):
            if msg["author"]["id"] == BOT_ID:
                continue
            last_id = msg["id"]
            author  = msg["author"]["id"]
            text    = msg["content"].strip()

            # Ignore messages that don't start with the prefix
            if not text.startswith(PREFIX):
                continue

            cmd = text[len(PREFIX):].strip().lower()
            log(f"[{msg['author'].get('username', author[:8])}] {PREFIX}{cmd[:80]}")

            if author not in _accounts:
                continue   # ignore users who aren't configured

            if cmd in ("shop", "s", "daily"):
                handle_shop(author)

            elif cmd in ("nm", "nightmarket", "night market", "night_market"):
                handle_nm(author)

            elif re.match(r"^buy\s+\S", cmd):
                handle_buy(author, re.match(r"^buy\s+(.+)$", cmd).group(1))

            elif cmd == "confirm":
                if author in _pending:
                    handle_confirm(author)

            elif cmd == "cancel":
                if _pending.pop(author, None):
                    d_send(content="❌ Purchase cancelled.")

        time.sleep(5)


if __name__ == "__main__":
    main()

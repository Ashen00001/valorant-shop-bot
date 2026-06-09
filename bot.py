#!/usr/bin/env python3
"""
Valorant Shop Bot — discord.py edition
Slash commands, works in DMs and servers, buy confirmation buttons.
"""

import os, sys, re, time, asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
import requests

import riot_auth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

VP_CURRENCY  = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
ITEM_TYPE_ID = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
CLIENT_PLATFORM = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0K"
    "CSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxh"
    "dGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
)
SETUP_TIMEOUT = 600

RARITY_COLORS = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 0x5a9fe1,
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 0x009984,
    "60bca009-4182-7998-dee7-b8a2558dc369": 0xd1538c,
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 0xf9d563,
    "e046854e-406c-37f4-6607-19a9ba8426fc": 0xf99358,
}

# ── State ─────────────────────────────────────────────────────────────────────
_client_version = None
_skins_cache    = None
_accounts       = {}   # user_id str → account dict
_pending_setups = {}   # user_id str → {verifier, region, ts}
_session_cache  = {}   # user_id str → {shop, nm}
_last_posted    = {}   # user_id str → "YYYY-MM-DD"


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # needed to read DM messages (redirect URL)

bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Valorant helpers (sync — called via asyncio.to_thread) ────────────────────
def _get_client_version():
    global _client_version
    if not _client_version:
        _client_version = (
            requests.get("https://valorant-api.com/v1/version", timeout=10)
            .json()["data"]["riotClientVersion"]
        )
    return _client_version

def _build_skins_cache():
    global _skins_cache
    if _skins_cache is not None:
        return _skins_cache
    log("Building skins cache...")
    r = requests.get("https://valorant-api.com/v1/weapons/skins?language=en-US", timeout=20)
    r.raise_for_status()
    _skins_cache = {}
    for skin in r.json()["data"]:
        name  = skin["displayName"]
        color = RARITY_COLORS.get(skin.get("contentTierUuid", ""), 0xFF4655)
        for level in skin.get("levels", []):
            icon = level.get("displayIcon") or (skin["levels"][0].get("displayIcon") if skin["levels"] else None)
            _skins_cache[level["uuid"]] = {"name": name, "icon": icon, "color": color}
    log(f"Skins cache ready ({len(_skins_cache)} levels)")
    return _skins_cache

def _vh(at, et):
    return {
        "Authorization":          f"Bearer {at}",
        "X-Riot-Entitlements-JWT": et,
        "X-Riot-ClientPlatform":   CLIENT_PLATFORM,
        "X-Riot-ClientVersion":    _get_client_version(),
        "Content-Type":           "application/json",
    }

def _fetch_storefront(at, et, puuid, region):
    r = requests.post(
        f"https://pd.{region}.a.pvp.net/store/v3/storefront/{puuid}",
        headers=_vh(at, et), json={}, timeout=15,
    )
    r.raise_for_status()
    return r.json()

def _parse_shop(sf):
    cache = _build_skins_cache()
    panel = sf["SkinsPanelLayout"]
    skins = []
    for offer in panel["SingleItemStoreOffers"]:
        oid = offer["OfferID"]
        sd  = cache.get(oid, {})
        skins.append({
            "name": sd.get("name", oid), "vp": offer["Cost"].get(VP_CURRENCY, 0),
            "offer_id": oid, "icon": sd.get("icon"), "color": sd.get("color", 0xFF4655),
            "is_nm": False,
        })
    return skins, panel["SingleItemOffersRemainingDurationInSeconds"]

def _parse_nm(sf):
    bs     = sf.get("BonusStore", {})
    offers = bs.get("BonusStoreOffers")
    if not offers:
        return None, None
    cache = _build_skins_cache()
    skins = []
    for offer in offers:
        oid   = offer["Offer"]["OfferID"]
        b_oid = offer["BonusOfferID"]
        sd    = cache.get(oid, {})
        skins.append({
            "name": sd.get("name", oid), "vp": offer["DiscountCosts"].get(VP_CURRENCY, 0),
            "orig_vp": offer["Offer"]["Cost"].get(VP_CURRENCY, 0),
            "disc_pct": offer.get("DiscountPercent", 0),
            "offer_id": b_oid, "icon": sd.get("icon"), "color": sd.get("color", 0xFF4655),
            "is_nm": True,
        })
    return skins, bs.get("BonusStoreRemainingDurationInSeconds", 0)

def _get_vp(at, et, puuid, region):
    r = requests.get(f"https://pd.{region}.a.pvp.net/store/v1/wallet/{puuid}",
                     headers=_vh(at, et), timeout=10)
    return r.json()["Balances"].get(VP_CURRENCY, 0)

def _do_order(at, et, puuid, region, offer_id):
    r = requests.post(
        f"https://pd.{region}.a.pvp.net/store/v1/order/",
        headers=_vh(at, et),
        json={"OfferId": offer_id, "ItemTypeId": ITEM_TYPE_ID, "Quantity": 1},
        timeout=15,
    )
    return r.status_code == 200, r.text

def _load_accounts():
    global _accounts
    raw = riot_auth.load_accounts()
    for did, acct in raw.items():
        try:
            at, et, puuid, updated = riot_auth.get_tokens(acct)
            _accounts[did] = updated
            log(f"Account ready: {did[:10]}... (puuid {puuid[:8]}...)")
        except Exception as e:
            log(f"WARNING: Could not load account {did}: {e}")

def _get_tokens_for(uid):
    """Sync. Returns (at, et, account) or (None, None, None)."""
    acct = _accounts.get(uid)
    if not acct:
        return None, None, None
    at, et, puuid, updated = riot_auth.get_tokens(acct)
    _accounts[uid] = updated
    all_accts = riot_auth.load_accounts()
    all_accts[uid] = updated
    riot_auth.save_accounts(all_accts)
    return at, et, updated


# ── Embed builders ────────────────────────────────────────────────────────────
def fmt_time(secs):
    h, r = divmod(max(0, int(secs)), 3600)
    return f"{h}h {r // 60}m" if h else f"{r // 60}m"

def _shop_embeds(skins, remaining, vp):
    header = discord.Embed(
        description=(
            f"**🔫 Daily Shop**\n"
            f"⏱ Resets in **{fmt_time(remaining)}**  ·  💰 **{vp:,} VP**"
        ),
        color=0x202225,
    )
    embeds = [header]
    for i, s in enumerate(skins):
        e = discord.Embed(title=f"`{i+1}.`  {s['name']}",
                          description=f"**{s['vp']:,} VP**", color=s["color"])
        if s.get("icon"):
            e.set_thumbnail(url=s["icon"])
        embeds.append(e)
    return embeds

def _nm_embeds(skins, remaining, vp):
    if not skins:
        return [discord.Embed(description="🌙 Night Market isn't active right now.", color=0x202225)]
    header = discord.Embed(
        description=(
            f"**🌙 Night Market**\n"
            f"⏱ Ends in **{fmt_time(remaining)}**  ·  💰 **{vp:,} VP**"
        ),
        color=0x202225,
    )
    embeds = [header]
    for i, s in enumerate(skins):
        e = discord.Embed(
            title=f"`NM{i+1}.`  {s['name']}",
            description=f"~~{s['orig_vp']:,} VP~~ → **{s['vp']:,} VP**  (-{s['disc_pct']}%)",
            color=s["color"],
        )
        if s.get("icon"):
            e.set_thumbnail(url=s["icon"])
        embeds.append(e)
    return embeds

def _confirm_embed(skin, vp):
    if skin.get("is_nm"):
        price = f"~~{skin['orig_vp']:,} VP~~ → **{skin['vp']:,} VP** (-{skin['disc_pct']}%)"
    else:
        price = f"**{skin['vp']:,} VP**"
    e = discord.Embed(
        title="⚠️ Confirm Purchase",
        description=f"**{skin['name']}**\n{price}\nBalance: **{vp:,} VP**",
        color=skin.get("color", 0xFFA500),
    )
    if skin.get("icon"):
        e.set_thumbnail(url=skin["icon"])
    return e


# ── Buy confirmation buttons ──────────────────────────────────────────────────
class BuyView(discord.ui.View):
    def __init__(self, uid, skin):
        super().__init__(timeout=120)
        self.uid  = uid
        self.skin = skin

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.green, emoji="✅")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("Not your purchase.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            at, et, acct = await asyncio.to_thread(_get_tokens_for, self.uid)
            if not at:
                await interaction.followup.send("❌ Auth error — try `/setup` again.")
                self.stop(); return
            ok, resp = await asyncio.to_thread(
                _do_order, at, et, acct["puuid"], acct["region"], self.skin["offer_id"]
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`")
            self.stop(); return

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        if ok:
            log(f"Purchase OK: {self.skin['name']} for {self.uid[:10]}...")
            desc = f"**{self.skin['name']}** for **{self.skin['vp']:,} VP**"
            if self.skin.get("is_nm"):
                desc += f" (-{self.skin['disc_pct']}%)"
            result = discord.Embed(title="✅ Bought!", description=desc + "  enjoy!",
                                   color=self.skin.get("color", 0x57F287))
            if self.skin.get("icon"):
                result.set_thumbnail(url=self.skin["icon"])
            await interaction.followup.send(embed=result)
        else:
            log(f"Purchase FAILED: {resp[:200]}")
            await interaction.followup.send(f"❌ Purchase failed: `{resp[:200]}`")
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("Not your purchase.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ── Slash commands ────────────────────────────────────────────────────────────
_user_install   = app_commands.allowed_installs(guilds=True, users=True)
_all_contexts   = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)

@tree.command(name="shop", description="See your daily Valorant shop")
@_user_install
@_all_contexts
async def cmd_shop(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in _accounts:
        await interaction.response.send_message(
            "You haven't linked your account yet. Run `/setup` first!", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
        if not at:
            await interaction.followup.send("❌ Auth error — run `/setup` to re-link.")
            return
        sf               = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
        skins, remaining = _parse_shop(sf)
        nm_skins, nm_rem = _parse_nm(sf)
        vp               = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])

        _session_cache[uid] = {"shop": skins, "nm": nm_skins}
        _last_posted[uid]   = _today()

        await interaction.followup.send(embeds=_shop_embeds(skins, remaining, vp))
        if nm_skins:
            await interaction.followup.send(embeds=_nm_embeds(nm_skins, nm_rem, vp))
        log(f"Shop posted for {uid[:10]}...")
    except Exception as e:
        log(f"Shop error for {uid[:10]}...: {e}")
        await interaction.followup.send(f"❌ Failed: `{e}`")


@tree.command(name="nm", description="See your Valorant night market")
@_user_install
@_all_contexts
async def cmd_nm(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in _accounts:
        await interaction.response.send_message("Run `/setup` first!", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
        if not at:
            await interaction.followup.send("❌ Auth error — run `/setup` to re-link.")
            return
        sf               = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
        nm_skins, nm_rem = _parse_nm(sf)
        vp               = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])
        _session_cache.setdefault(uid, {})["nm"] = nm_skins
        await interaction.followup.send(embeds=_nm_embeds(nm_skins, nm_rem, vp))
        log(f"NM posted for {uid[:10]}...")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`")


@tree.command(name="buy", description="Buy a skin from your shop or night market")
@_user_install
@_all_contexts
@app_commands.describe(skin="Skin name or number (e.g. 'phantom', '2', 'nm3')")
async def cmd_buy(interaction: discord.Interaction, skin: str):
    uid    = str(interaction.user.id)
    cached = _session_cache.get(uid, {})
    shop   = cached.get("shop", [])
    nm     = cached.get("nm") or []

    if not shop and not nm:
        await interaction.response.send_message(
            "Run `/shop` first so I know what's in your store.", ephemeral=True)
        return

    match = _match(skin, shop) or _match(skin, nm)
    if not match:
        await interaction.response.send_message(
            f"No match for `{skin}` — use a number like `2`, `nm3`, or a partial name.",
            ephemeral=True)
        return

    await interaction.response.defer()
    try:
        at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
        if not at:
            await interaction.followup.send("❌ Auth error.")
            return
        vp = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")
        return

    log(f"Buy prompt: {match['name']} for {uid[:10]}...")
    await interaction.followup.send(embed=_confirm_embed(match, vp), view=BuyView(uid, match))


@tree.command(name="setup", description="Link your Riot/Valorant account")
@_user_install
@_all_contexts
@app_commands.describe(region="Your Valorant region (default: na)")
@app_commands.choices(region=[
    app_commands.Choice(name="NA", value="na"),
    app_commands.Choice(name="EU", value="eu"),
    app_commands.Choice(name="AP", value="ap"),
    app_commands.Choice(name="KR", value="kr"),
    app_commands.Choice(name="BR", value="br"),
    app_commands.Choice(name="LATAM", value="latam"),
])
async def cmd_setup(interaction: discord.Interaction, region: str = "na"):
    uid      = str(interaction.user.id)
    auth_url, verifier = riot_auth.get_browser_login_url()

    _pending_setups[uid] = {"verifier": verifier, "region": region, "ts": time.time()}

    embed = discord.Embed(
        title="🔗 Link your Valorant account",
        description=(
            f"**Step 1 —** [Click here to log into Riot]({auth_url})\n\n"
            "**Step 2 —** After logging in your browser will show an error page — that's fine.\n\n"
            "**Step 3 —** Copy the **full URL** from the address bar and **DM it to me**.\n\n"
            "It starts with `http://localhost/redirect?code=...`\n\n"
            "⏱ Link expires in 10 minutes."
        ),
        color=0xFF4655,
    )

    # Send ephemeral so the OAuth link stays somewhat private
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log(f"Setup started for {uid[:10]}... (region={region})")


# ── DM listener (catches redirect URL pasted by user after /setup) ────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    uid = str(message.author.id)
    if uid not in _pending_setups:
        return

    content = message.content.strip()
    setup   = _pending_setups[uid]

    if time.time() - setup["ts"] > SETUP_TIMEOUT:
        _pending_setups.pop(uid, None)
        await message.channel.send("⏱ That link expired. Run `/setup` again.")
        return

    if "localhost/redirect" not in content and "code=" not in content:
        await message.channel.send(
            "Paste the **full URL** from your browser address bar.\n"
            "It starts with `http://localhost/redirect?code=...`"
        )
        return

    await message.channel.send("⏳ Linking account...")
    try:
        account = await asyncio.to_thread(
            riot_auth.complete_browser_login, content, setup["verifier"], setup["region"]
        )
    except Exception as e:
        await message.channel.send(f"❌ Failed: `{e}`\nRun `/setup` again.")
        _pending_setups.pop(uid, None)
        return

    _accounts[uid] = account
    all_accts      = riot_auth.load_accounts()
    all_accts[uid] = account
    riot_auth.save_accounts(all_accts)
    _pending_setups.pop(uid, None)

    await message.channel.send(
        "✅ **Account linked!**\nYou can now use `/shop`, `/nm`, and `/buy` anywhere."
    )
    log(f"Account linked: {uid[:10]}... (puuid {account['puuid'][:8]}...)")


# ── Auto-post daily shop at midnight UTC ──────────────────────────────────────
@tasks.loop(minutes=1)
async def auto_post():
    now   = datetime.now(timezone.utc)
    today = _today()
    if now.hour == 0 and now.minute < 10:
        for uid in list(_accounts.keys()):
            if _last_posted.get(uid) == today:
                continue
            try:
                user = await bot.fetch_user(int(uid))
                at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
                if not at:
                    continue
                sf               = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
                skins, remaining = _parse_shop(sf)
                nm_skins, nm_rem = _parse_nm(sf)
                vp               = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])

                _session_cache[uid] = {"shop": skins, "nm": nm_skins}
                _last_posted[uid]   = today

                await user.send(embeds=_shop_embeds(skins, remaining, vp))
                if nm_skins:
                    await user.send(embeds=_nm_embeds(nm_skins, nm_rem, vp))
                log(f"Auto-posted shop to {uid[:10]}...")
                await asyncio.sleep(3)
            except Exception as e:
                log(f"Auto-post failed for {uid[:10]}...: {e}")


# ── Misc ──────────────────────────────────────────────────────────────────────
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _match(q, skins):
    q       = q.strip().lower()
    idx_str = re.sub(r"^nm", "", q)
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        return skins[idx] if 0 <= idx < len(skins) else None
    return next((s for s in skins if q in s["name"].lower()), None)


# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log("=" * 55)
    log(f"Logged in as {bot.user} ({bot.user.id})")
    log("=" * 55)
    await asyncio.to_thread(_load_accounts)
    log(f"{len(_accounts)} account(s) loaded")
    await asyncio.to_thread(_build_skins_cache)
    await tree.sync()
    log("Slash commands synced globally")
    auto_post.start()
    log("Auto-post scheduler started (fires at 00:00 UTC)")


if not DISCORD_TOKEN:
    log("FATAL: DISCORD_BOT_TOKEN not set")
    sys.exit(1)

bot.run(DISCORD_TOKEN)

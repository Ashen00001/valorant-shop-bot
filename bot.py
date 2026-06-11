#!/usr/bin/env python3
"""
Valorant Shop Bot — discord.py edition
Slash commands, works in DMs and servers.
Commands: /shop /nm /bundle /buy /setup /wishlist
"""

import os, sys, re, time, json, asyncio
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
DISCORD_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN")
SCRIPT_DIR     = Path(__file__).parent
WISHLIST_FILE  = SCRIPT_DIR / "wishlist.json"

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
_bundles_cache  = None
_accounts       = {}   # uid → account dict
_pending_setups = {}   # uid → {verifier, region, ts}
_session_cache  = {}   # uid → {shop, nm}
_last_posted    = {}   # uid → "YYYY-MM-DD"
_wishlist       = {}   # uid → [skin_name_lower, ...]
_dismissed      = {}   # uid → {skin_name_lower: date_str} — suppressed until next reset

intents = discord.Intents.default()
intents.message_content = True
bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

_user_install = app_commands.allowed_installs(guilds=True, users=True)
_all_contexts = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Wishlist persistence ───────────────────────────────────────────────────────
def _load_wishlist():
    global _wishlist
    if WISHLIST_FILE.exists():
        _wishlist = json.loads(WISHLIST_FILE.read_text())

def _save_wishlist():
    WISHLIST_FILE.write_text(json.dumps(_wishlist, indent=2))


# ── Valorant helpers ──────────────────────────────────────────────────────────
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

def _build_bundles_cache():
    global _bundles_cache
    if _bundles_cache is not None:
        return _bundles_cache
    r = requests.get("https://valorant-api.com/v1/bundles?language=en-US", timeout=15)
    r.raise_for_status()
    _bundles_cache = {b["uuid"]: b for b in r.json()["data"]}
    return _bundles_cache

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

def _parse_bundles(sf):
    """Returns list of {name, image, items, remaining} — one per active bundle."""
    fb  = sf.get("FeaturedBundle", {})
    raw_bundles = fb.get("Bundles") or ([fb["Bundle"]] if fb.get("Bundle") else [])
    if not raw_bundles:
        return []
    skins_cache   = _build_skins_cache()
    bundles_cache = _build_bundles_cache()
    out = []
    for raw in raw_bundles:
        bundle_uuid = raw.get("DataAssetID", "")
        bundle_meta = bundles_cache.get(bundle_uuid, {})
        name        = bundle_meta.get("displayName", "Featured Bundle")
        image       = bundle_meta.get("displayIcon") or bundle_meta.get("verticalPromoImage")
        items = []
        for item in raw.get("Items", []):
            if item.get("Item", {}).get("ItemTypeID") != ITEM_TYPE_ID:
                continue
            oid = item["Item"]["ItemID"]
            sd  = skins_cache.get(oid, {})
            items.append({
                "name":    sd.get("name", oid),
                "vp":      item.get("DiscountedPrice", item.get("BasePrice", 0)),
                "orig_vp": item.get("BasePrice", 0),
                "icon":    sd.get("icon"),
                "color":   sd.get("color", 0xFF4655),
            })
        remaining = raw.get("DurationRemainingInSeconds",
                            fb.get("BundleRemainingDurationInSeconds", 0))
        out.append({"name": name, "image": image, "items": items, "remaining": remaining})
    return out

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

def _confirm_embed(skin, vp):
    price = (f"~~{skin['orig_vp']:,} VP~~ → **{skin['vp']:,} VP** (-{skin['disc_pct']}%)"
             if skin.get("is_nm") else f"**{skin['vp']:,} VP**")
    e = discord.Embed(
        title="⚠️ Confirm Purchase",
        description=f"**{skin['name']}**\n{price}\nBalance: **{vp:,} VP**",
        color=skin.get("color", 0xFFA500),
    )
    if skin.get("icon"):
        e.set_thumbnail(url=skin["icon"])
    return e


# ── Night Market reveal view ──────────────────────────────────────────────────
class RevealButton(discord.ui.Button):
    def __init__(self, idx, skin, uid):
        super().__init__(label=f"#{idx + 1}", style=discord.ButtonStyle.secondary,
                         emoji="🎴", row=idx // 3)
        self.idx  = idx
        self.skin = skin
        self.uid  = uid

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("Not your night market!", ephemeral=True)
            return
        s = self.skin
        self.label    = s["name"][:22]
        self.style    = discord.ButtonStyle.success
        self.emoji    = None
        self.disabled = True

        e = discord.Embed(
            title=f"🎴  {s['name']}",
            description=(
                f"~~{s['orig_vp']:,} VP~~ → **{s['vp']:,} VP**\n"
                f"**-{s['disc_pct']}% off**"
            ),
            color=s["color"],
        )
        if s.get("icon"):
            e.set_thumbnail(url=s["icon"])

        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(embed=e)


class NMRevealView(discord.ui.View):
    def __init__(self, uid, nm_skins):
        super().__init__(timeout=600)
        for i, skin in enumerate(nm_skins):
            self.add_item(RevealButton(i, skin, uid))


# ── Buy confirmation buttons ──────────────────────────────────────────────────
class BuyView(discord.ui.View):
    def __init__(self, uid, skin):
        super().__init__(timeout=120)
        self.uid  = uid
        self.skin = skin

    async def _finish(self, interaction, ok, resp=None):
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
            await interaction.followup.send(f"❌ Purchase failed: `{resp[:200]}`")
        self.stop()

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.green, emoji="✅")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("Not your purchase.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            at, et, acct = await asyncio.to_thread(_get_tokens_for, self.uid)
            if not at:
                await interaction.followup.send("❌ Auth error — run `/setup`.")
                self.stop(); return
            ok, resp = await asyncio.to_thread(
                _do_order, at, et, acct["puuid"], acct["region"], self.skin["offer_id"]
            )
            await self._finish(interaction, ok, resp)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`")
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


# ── Wishlist notification view ────────────────────────────────────────────────
class WishlistDismissView(discord.ui.View):
    def __init__(self, uid, skin_name):
        super().__init__(timeout=None)
        self.uid       = uid
        self.skin_name = skin_name

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="🔕")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("Not your notification.", ephemeral=True)
            return
        _dismissed.setdefault(self.uid, {})[self.skin_name.lower()] = _today()
        button.disabled = True
        button.label    = "Dismissed"
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            "🔕 Got it — no more pings for this skin until your shop resets.", ephemeral=True
        )
        self.stop()


# ── Slash commands ────────────────────────────────────────────────────────────
@tree.command(name="shop", description="See your daily Valorant shop")
@_user_install
@_all_contexts
async def cmd_shop(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in _accounts:
        await interaction.response.send_message("Run `/setup` to link your account first!", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
        if not at:
            await interaction.followup.send("❌ Auth error — run `/setup` to re-link.")
            return
        sf               = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
        skins, remaining = _parse_shop(sf)
        nm_skins, _      = _parse_nm(sf)
        vp               = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])

        _session_cache[uid] = {"shop": skins, "nm": nm_skins}
        _last_posted[uid]   = _today()

        await interaction.followup.send(embeds=_shop_embeds(skins, remaining, vp))
        log(f"Shop posted for {uid[:10]}...")
    except Exception as e:
        log(f"Shop error for {uid[:10]}...: {e}")
        await interaction.followup.send(f"❌ Failed: `{e}`")


@tree.command(name="nm", description="Reveal your Valorant night market")
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

        if not nm_skins:
            await interaction.followup.send(embed=discord.Embed(
                description="🌙 Night Market isn't active right now.", color=0x202225))
            return

        header = discord.Embed(
            description=(
                f"**🌙 Night Market**\n"
                f"⏱ Ends in **{fmt_time(nm_rem)}**  ·  💰 **{vp:,} VP**\n\n"
                f"Tap a card to reveal it."
            ),
            color=0x202225,
        )
        await interaction.followup.send(embed=header, view=NMRevealView(uid, nm_skins))
        log(f"NM reveal sent to {uid[:10]}...")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`")


@tree.command(name="bundle", description="See the current featured bundle(s)")
@_user_install
@_all_contexts
async def cmd_bundle(interaction: discord.Interaction):
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
        sf      = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
        bundles = await asyncio.to_thread(_parse_bundles, sf)
        if not bundles:
            await interaction.followup.send(embed=discord.Embed(
                description="No featured bundles right now.", color=0x202225))
            return

        # One message per bundle — each with its own name, image, and items
        for b in bundles:
            total = sum(s["vp"] for s in b["items"])
            header = discord.Embed(
                title=f"🎁 {b['name']} Bundle",
                description=(
                    f"⏱ Ends in **{fmt_time(b['remaining'])}**"
                    + (f"  ·  💰 Full bundle ≈ **{total:,} VP**" if total else "")
                ),
                color=0xFF4655,
            )
            if b["image"]:
                header.set_image(url=b["image"])
            embeds = [header]
            for s in b["items"]:
                e = discord.Embed(title=s["name"], color=s["color"])
                if s["orig_vp"] and s["orig_vp"] != s["vp"]:
                    e.description = f"~~{s['orig_vp']:,} VP~~ → **{s['vp']:,} VP**"
                else:
                    e.description = f"**{s['vp']:,} VP**"
                if s.get("icon"):
                    e.set_thumbnail(url=s["icon"])
                embeds.append(e)
            await interaction.followup.send(embeds=embeds[:10])
        log(f"{len(bundles)} bundle(s) posted for {uid[:10]}...")
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
        await interaction.response.send_message("Run `/shop` first.", ephemeral=True)
        return
    match = _match(skin, shop) or _match(skin, nm)
    if not match:
        await interaction.response.send_message(
            f"No match for `{skin}` — use a number like `2`, `nm3`, or a partial name.", ephemeral=True)
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
    await interaction.followup.send(embed=_confirm_embed(match, vp), view=BuyView(uid, match))


# ── Wishlist commands ─────────────────────────────────────────────────────────
wishlist_group = app_commands.Group(name="wishlist", description="Manage your skin wishlist")
tree.add_command(wishlist_group)

@wishlist_group.command(name="add", description="Add a skin to your wishlist")
@_user_install
@_all_contexts
@app_commands.describe(skin="Skin name to watch for (e.g. 'Oni Phantom')")
async def wl_add(interaction: discord.Interaction, skin: str):
    uid  = str(interaction.user.id)
    name = skin.strip().lower()
    wl   = _wishlist.setdefault(uid, [])
    if name in wl:
        await interaction.response.send_message(f"**{skin}** is already on your wishlist.", ephemeral=True)
        return
    wl.append(name)
    _save_wishlist()
    await interaction.response.send_message(
        f"✅ Added **{skin}** to your wishlist. I'll ping you when it shows up in your shop.", ephemeral=True)

@wishlist_group.command(name="remove", description="Remove a skin from your wishlist")
@_user_install
@_all_contexts
@app_commands.describe(skin="Skin name to remove")
async def wl_remove(interaction: discord.Interaction, skin: str):
    uid  = str(interaction.user.id)
    name = skin.strip().lower()
    wl   = _wishlist.get(uid, [])
    if name not in wl:
        await interaction.response.send_message(f"**{skin}** isn't on your wishlist.", ephemeral=True)
        return
    wl.remove(name)
    _save_wishlist()
    await interaction.response.send_message(f"🗑️ Removed **{skin}** from your wishlist.", ephemeral=True)

@wishlist_group.command(name="view", description="See your current wishlist")
@_user_install
@_all_contexts
async def wl_view(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    wl  = _wishlist.get(uid, [])
    if not wl:
        await interaction.response.send_message("Your wishlist is empty. Use `/wishlist add` to add skins.", ephemeral=True)
        return
    lines = "\n".join(f"• {s.title()}" for s in wl)
    await interaction.response.send_message(
        embed=discord.Embed(title="🎯 Your Wishlist", description=lines, color=0xFF4655),
        ephemeral=True)


# ── Setup command ─────────────────────────────────────────────────────────────
@tree.command(name="setup", description="Link your Riot/Valorant account")
@_user_install
@_all_contexts
@app_commands.describe(region="Your Valorant region (default: na)")
@app_commands.choices(region=[
    app_commands.Choice(name="NA",    value="na"),
    app_commands.Choice(name="EU",    value="eu"),
    app_commands.Choice(name="AP",    value="ap"),
    app_commands.Choice(name="KR",    value="kr"),
    app_commands.Choice(name="BR",    value="br"),
    app_commands.Choice(name="LATAM", value="latam"),
])
async def cmd_setup(interaction: discord.Interaction, region: str = "na"):
    uid              = str(interaction.user.id)
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
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log(f"Setup started for {uid[:10]}... (region={region})")


# ── DM listener ───────────────────────────────────────────────────────────────
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
            "Paste the **full URL** from your address bar — starts with `http://localhost/redirect?code=...`")
        return
    await message.channel.send("⏳ Linking account...")
    try:
        account = await asyncio.to_thread(
            riot_auth.complete_browser_login, content, setup["verifier"], setup["region"])
    except Exception as e:
        await message.channel.send(f"❌ Failed: `{e}`\nRun `/setup` again.")
        _pending_setups.pop(uid, None)
        return
    _accounts[uid] = account
    all_accts      = riot_auth.load_accounts()
    all_accts[uid] = account
    riot_auth.save_accounts(all_accts)
    _pending_setups.pop(uid, None)
    await message.channel.send("✅ **Account linked!**\nYou can now use `/shop`, `/nm`, `/bundle`, and `/buy` anywhere.")
    log(f"Account linked: {uid[:10]}... (puuid {account['puuid'][:8]}...)")


# ── Auto-post daily shop ──────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def auto_post():
    now   = datetime.now(timezone.utc)
    today = _today()
    if now.hour == 0 and now.minute < 10:
        # Clear dismissed on new shop day
        for uid in list(_dismissed.keys()):
            _dismissed[uid] = {k: v for k, v in _dismissed[uid].items() if v == today}

        for uid in list(_accounts.keys()):
            if _last_posted.get(uid) == today:
                continue
            try:
                user             = await bot.fetch_user(int(uid))
                at, et, acct     = await asyncio.to_thread(_get_tokens_for, uid)
                if not at:
                    continue
                sf               = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
                skins, remaining = _parse_shop(sf)
                nm_skins, _      = _parse_nm(sf)
                vp               = await asyncio.to_thread(_get_vp, at, et, acct["puuid"], acct["region"])
                _session_cache[uid] = {"shop": skins, "nm": nm_skins}
                _last_posted[uid]   = today
                await user.send(embeds=_shop_embeds(skins, remaining, vp))
                log(f"Auto-posted shop to {uid[:10]}...")
                await asyncio.sleep(3)
            except Exception as e:
                log(f"Auto-post failed for {uid[:10]}...: {e}")


# ── Wishlist check (every 3 hours) ────────────────────────────────────────────
@tasks.loop(hours=3)
async def wishlist_check():
    today = _today()
    for uid, wish_skins in list(_wishlist.items()):
        if not wish_skins or uid not in _accounts:
            continue
        try:
            at, et, acct = await asyncio.to_thread(_get_tokens_for, uid)
            if not at:
                continue
            sf           = await asyncio.to_thread(_fetch_storefront, at, et, acct["puuid"], acct["region"])
            shop, _      = _parse_shop(sf)
            nm, _        = _parse_nm(sf)
            all_skins    = (shop or []) + (nm or [])
            dismissed    = _dismissed.get(uid, {})

            for skin in all_skins:
                name_lower = skin["name"].lower()
                if not any(w in name_lower for w in wish_skins):
                    continue
                matched_wish = next(w for w in wish_skins if w in name_lower)
                if dismissed.get(matched_wish) == today:
                    continue

                user  = await bot.fetch_user(int(uid))
                label = "🌙 Night Market" if skin.get("is_nm") else "🔫 Daily Shop"
                price = (f"~~{skin['orig_vp']:,} VP~~ → **{skin['vp']:,} VP** (-{skin['disc_pct']}%)"
                         if skin.get("is_nm") else f"**{skin['vp']:,} VP**")
                e = discord.Embed(
                    title=f"🎯 Wishlist hit — {skin['name']}",
                    description=f"{label}\n{price}\n\nUse `/buy` to purchase.",
                    color=skin["color"],
                )
                if skin.get("icon"):
                    e.set_thumbnail(url=skin["icon"])
                await user.send(embed=e, view=WishlistDismissView(uid, matched_wish))
                log(f"Wishlist ping: {skin['name']} → {uid[:10]}...")
                await asyncio.sleep(1)
        except Exception as e:
            log(f"Wishlist check failed for {uid[:10]}...: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────
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
    await asyncio.to_thread(_load_wishlist)
    log(f"{len(_accounts)} account(s) loaded")
    await asyncio.to_thread(_build_skins_cache)
    await asyncio.to_thread(_build_bundles_cache)
    await tree.sync()
    log("Slash commands synced globally")
    auto_post.start()
    wishlist_check.start()
    log("Tasks started")


if not DISCORD_TOKEN:
    log("FATAL: DISCORD_BOT_TOKEN not set")
    sys.exit(1)

bot.run(DISCORD_TOKEN)

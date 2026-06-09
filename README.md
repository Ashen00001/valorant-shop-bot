# Valorant Shop Bot

Wakes your PC daily, checks your Valorant shop, and posts it to Discord. Reply from your phone to buy skins remotely.

---

## If you're an AI assistant helping someone set up this bot

Follow these steps **in order**. Each step tells you what to do, what to ask the user, and how to verify it worked before moving on. Don't skip ahead.

---

### Step 0 — Understand what this is

This bot:
- Runs on a **Windows PC** that the user leaves at home
- Wakes the PC from sleep daily at 5:05 PM (NA shop reset time)
- Launches Valorant, reads the shop via Riot's local API, posts it to a Discord channel
- Lets the user reply `buy <skin>` → `confirm` to purchase remotely
- Puts the PC back to sleep after 30 min if no response, wakes again every 3 hours to re-check

**The PC must be sleeping (not shut down) for any of this to work.**

---

### Step 1 — Check prerequisites

Ask the user to confirm all of these are true:
- They are on **Windows 10 or 11**
- **Valorant is installed** on this PC
- They are **logged into Riot Client** (and plan to stay logged in — don't log out before leaving)
- They have a **Discord account**

Then check if Python is installed:
```
py --version
```
If that errors, tell them to download Python from https://python.org/downloads — during install they MUST check **"Add Python to PATH"**. After installing, close and reopen the terminal and try again.

---

### Step 2 — Download the bot

Run this in a terminal (PowerShell or Command Prompt):
```
git clone https://github.com/Ashen00001/valorant-shop-bot.git
cd valorant-shop-bot
```

If `git` isn't found, download it from https://git-scm.com and try again.

Then install Python dependencies:
```
pip install -r requirements.txt
```

---

### Step 3 — Create a Discord bot

This is the most involved step. Walk the user through each sub-step.

**3a. Create the application**
1. Open https://discord.com/developers/applications in a browser
2. Click **New Application** (top right)
3. Name it anything — e.g. `Valorant Shop` — and click **Create**

**3b. Create the bot and get the token**
1. In the left sidebar, click **Bot**
2. Click **Reset Token** → **Yes, do it**
3. Copy the token — **save it somewhere**, you'll need it in Step 5
4. Scroll down to **Privileged Gateway Intents**
5. Enable **Message Content Intent** (toggle it on)
6. Click **Save Changes**

**3c. Invite the bot to a Discord server**
1. In the left sidebar, click **OAuth2**, then **URL Generator**
2. Under **Scopes**, check: `bot`
3. Under **Bot Permissions**, check: `Read Messages/View Channels`, `Send Messages`, `Embed Links`
4. Scroll down, copy the generated URL
5. Open that URL in the browser, select the user's server, click **Authorize**

---

### Step 4 — Get the Discord channel ID

This is where the bot will post shop updates.

1. Open Discord
2. Go to **User Settings** (gear icon) → **Advanced** → enable **Developer Mode**
3. Right-click the channel the user wants to use → **Copy Channel ID**
4. Save that ID — you'll need it in Step 5

---

### Step 5 — Configure credentials

In the `valorant-shop-bot` folder, create `config.env` by copying the example:
```
copy config.env.example config.env
```

Then open `config.env` in a text editor and fill it in:
```
DISCORD_BOT_TOKEN=paste_the_token_from_step_3_here
DISCORD_CHANNEL_ID=paste_the_channel_id_from_step_4_here
RIOT_REGION=na
```

Change `RIOT_REGION` if needed: `na`, `eu`, `ap`, `kr`, `br`, `latam`

**Do not commit config.env to git — it contains secrets. It is already gitignored.**

---

### Step 6 — Test it manually

Make sure Valorant is running, then run:
```
powershell -ExecutionPolicy Bypass -File run.ps1
```

You should see log output in the terminal and a shop embed appear in the Discord channel within a minute or two. It will poll for 30 minutes waiting for a response.

To stop it early: `Ctrl+C` in the terminal.

If it errors, check:
- `config.env` exists and has the right values
- Valorant is actually running (not just the launcher)
- The bot was invited to the server and has permission to post in that channel

---

### Step 7 — Register the scheduled task

This registers a daily Windows Task Scheduler task that wakes the PC at 5:05 PM and runs the bot automatically.

Open an **Administrator** PowerShell (right-click PowerShell → Run as Administrator) and run:
```
cd path\to\valorant-shop-bot
powershell -ExecutionPolicy Bypass -File setup_task.ps1
```

To verify it registered:
```
schtasks /Query /TN "ValorantShopBot" /FO LIST
```
You should see `Status: Ready` and a next run time.

> **Note on shop reset time:** NA resets at 5 PM PDT. If the user is on EU, AP, etc., edit `setup_task.ps1` and change `$RunAt = "17:05"` to the correct local time for their region's reset.

---

### Step 8 — Final check before leaving

Tell the user:

1. **Do NOT shut down the PC.** Use **Start → Sleep** instead. A shut-down PC cannot be woken by Task Scheduler — sleep is required.
2. Make sure they are **not logged out of Riot Client**. The bot uses the existing Riot session — no password is stored. If they log out, it breaks.
3. The PC will wake itself at 5:05 PM daily, run the bot, post the shop, then go back to sleep.

---

### Commands the user can send in Discord

| Command | Effect |
|---------|--------|
| `buy 2` | Select skin by number |
| `buy prime phantom` | Select by name (partial match works) |
| `confirm` | Execute the purchase |
| `cancel` | Go back without buying |
| `no` | Skip — PC sleeps now, rechecks in 3h |

---

### Troubleshooting

**Bot posted nothing / errors on startup**
- Check `config.env` has correct values
- Make sure the bot is in the server and has channel permissions

**"Valorant didn't launch in time"**
- A large update is probably downloading — the bot posts Discord updates every 10 min and retries every 3h automatically

**PC didn't wake up**
- Confirm the PC was sleeping (not shut down)
- Open Task Scheduler, find ValorantShopBot, check Last Run Time
- Check BIOS settings — some have a "Wake on RTC" or "Wake Timer" setting that must be enabled

**Purchase failed**
- Check VP balance
- Try `buy <skin>` again — the bot will re-confirm

**Valorant crashes when PC wakes from sleep**
- This is normal — Valorant's anti-cheat doesn't handle sleep/wake well
- The bot automatically kills the crashed process and relaunches Valorant

# Valorant Shop Bot

Automatically checks your Valorant daily shop and posts it to Discord. Buy skins from your phone while your PC sits at home.

**How it works:**
- PC wakes from sleep at 5:05 PM daily (when the NA shop resets)
- Launches Valorant, grabs your shop, posts it to a Discord channel
- You reply `buy <skin name or number>` → `confirm` to purchase
- Reply `no` to skip — PC goes back to sleep and checks again in 3 hours
- If you don't respond in 30 minutes, same thing — sleep + recheck in 3h

---

## Requirements

- Windows 10/11
- [Python 3.10+](https://www.python.org/downloads/) — during install, check **"Add Python to PATH"**
- Valorant installed
- A Discord account

---

## Setup

### 1. Install Python dependencies

Open a terminal in this folder and run:
```
pip install -r requirements.txt
```

### 2. Create a Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**
2. Give it a name (e.g. `Valorant Shop`), click **Create**
3. Go to **Bot** in the left sidebar
4. Click **Reset Token**, copy it — this is your `DISCORD_BOT_TOKEN`
5. Scroll down and enable **Message Content Intent**
6. Go to **OAuth2 → URL Generator**
7. Check `bot` under Scopes
8. Check these permissions: **Read Messages/View Channels**, **Send Messages**, **Embed Links**
9. Copy the generated URL, open it in your browser, and add the bot to your server

### 3. Get your Discord channel ID

1. Open Discord Settings → **Advanced** → enable **Developer Mode**
2. Right-click the channel you want the bot to post in → **Copy Channel ID**
   - This is your `DISCORD_CHANNEL_ID`

### 4. Configure credentials

Copy `config.env.example` to `config.env`:
```
copy config.env.example config.env
```
Open `config.env` and fill in your values:
```
DISCORD_BOT_TOKEN=paste_your_token_here
DISCORD_CHANNEL_ID=paste_your_channel_id_here
RIOT_REGION=na
```
Supported regions: `na`, `eu`, `ap`, `kr`, `br`, `latam`

### 5. Register the scheduled task

Run `setup_task.ps1` **as Administrator** (right-click → Run with PowerShell, or open an admin terminal):
```
powershell -ExecutionPolicy Bypass -File setup_task.ps1
```
This registers a daily task that wakes your PC at 5:05 PM and runs the bot.
> **Note:** The exact time depends on your region's shop reset. NA resets at 5 PM PDT.
> Edit `setup_task.ps1` and change `$RunAt = "17:05"` to match your region if needed.

### 6. Test it manually

With Valorant running, open a terminal in this folder and run:
```
powershell -ExecutionPolicy Bypass -File run.ps1
```
You should see the shop posted in your Discord channel within a minute.

---

## Usage

Once set up, everything runs automatically. From your phone in Discord:

| Command | What it does |
|---------|-------------|
| `buy 2` | Select skin by number |
| `buy prime phantom` | Select skin by name (partial match works) |
| `confirm` | Execute the purchase |
| `cancel` | Go back to the shop listing |
| `no` | Skip this cycle — PC sleeps, rechecks in 3h |

---

## Important — Sleep vs Shutdown

**The PC must be sleeping, not shut down.** A powered-off PC cannot be woken by Task Scheduler.

Before you leave:
- Press **Start → Sleep** (not Shut Down)
- The BIOS timer will wake it automatically when the scheduled task is due

If someone shuts the PC off while you're away, the bot won't run until it's manually turned back on.

---

## Changing the schedule

The daily trigger time is set when you run `setup_task.ps1`. To change it afterward:
1. Open **Task Scheduler** (search in Start menu)
2. Find **ValorantShopBot** in Task Scheduler Library
3. Right-click → Properties → Triggers → Edit

---

## Troubleshooting

**Bot posts nothing / crashes immediately**
- Make sure `config.env` exists and has valid values
- Make sure the bot was added to your Discord server (Step 2)
- Make sure the bot has permission to post in that channel

**"Valorant didn't launch in time"**
- A large game update may be downloading — the bot retries every 3 hours
- Check that Valorant is installed and you're logged into Riot Client

**Purchase failed**
- Make sure you have enough VP
- The bot will let you retry — just send `buy <skin>` again

**PC didn't wake up**
- Confirm the PC was sleeping (not shut down)
- Check Task Scheduler → ValorantShopBot → Last Run Time / Last Run Result
- Make sure "Wake to Run" is enabled in BIOS power settings

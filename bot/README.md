# Ryno Sender Bot (Membership Gate)

## Setup (Windows PowerShell)

1) Create venv:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2) Install deps:

```powershell
pip install -r requirements.txt
```

3) Create `.env`:

- Copy `.env.example` to `.env`
- Set `BOT_TOKEN`
- Set `REQUIRED_CHANNEL` (e.g. `@mychannel`)
- If your channel is private, set `CHANNEL_JOIN_URL` too
- Optionally set `ADMIN_CONTACT` (e.g. `@your_admin`)

4) Make the bot admin in the channel.

- The bot must be **admin** of the channel you enforce, otherwise Telegram won’t return membership info reliably.

5) Run:

```powershell
python bot.py
```

## Setup (Ubuntu/Debian VPS + systemd)

### 1) Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 2) Create a dedicated user (recommended)

```bash
sudo adduser --disabled-password --gecos "" bot
```

### 3) Upload / clone the bot

Option A (git):

```bash
sudo mkdir -p /opt/ryno-bot
sudo chown -R bot:bot /opt/ryno-bot
sudo -u bot git clone <YOUR_REPO_URL> /opt/ryno-bot
```

Option B (zip/scp): copy your project folder to `/opt/ryno-bot` and `chown` it to user `bot`.

### 4) Create venv + install requirements

```bash
cd /opt/ryno-bot
sudo -u bot python3 -m venv .venv
sudo -u bot ./.venv/bin/pip install -r requirements.txt
```

### 5) Configure `.env`

```bash
cd /opt/ryno-bot
sudo -u bot cp .env.example .env
sudo -u bot nano .env
```

Minimum required values:
- `BOT_TOKEN`
- `REQUIRED_CHANNEL`
- `OWNER_CHAT_ID`

If your channel is private:
- set `CHANNEL_JOIN_URL`

Also make sure the bot is **admin** in the channel.

### 6) Quick sanity check

```bash
cd /opt/ryno-bot
sudo -u bot ./.venv/bin/python -m py_compile bot.py
```

### 7) Install as a systemd service

1) Copy the service template and adjust paths if you used a different folder:

```bash
sudo cp /opt/ryno-bot/deploy/ryno-sender-bot.service.example /etc/systemd/system/ryno-sender-bot.service
sudo nano /etc/systemd/system/ryno-sender-bot.service
```

2) Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ryno-sender-bot
sudo systemctl start ryno-sender-bot
```

3) View logs:

```bash
sudo journalctl -u ryno-sender-bot -f
```

4) Restart after code/config changes:

```bash
sudo systemctl restart ryno-sender-bot
```

## Deploy (Railway)

This bot is a long-running **polling worker** (it does not need an HTTP port).

### Option A (recommended): Deploy from GitHub using Dockerfile

1) Push this project to GitHub.
2) In Railway:
  - New Project → Deploy from GitHub Repo
  - Select this repo
3) In the service settings:
  - Ensure it runs as a **Worker** (no port required)
4) Set environment variables in Railway (Variables tab):
  - `BOT_TOKEN`
  - `REQUIRED_CHANNEL`
  - `OWNER_CHAT_ID`
  - `BOT_ADMIN_IDS` (example: `6041119040`)
  - Optional: `CHANNEL_JOIN_URL` (for private channels)
  - Optional: `TZ_NAME` (default `Asia/Tehran`)

### SQLite persistence (important)

If you use SQLite without a persistent disk, your data can be lost on redeploy.

Recommended:
1) Attach a **Volume** to the Railway service.
2) The code will automatically store the database in the mounted volume if Railway provides `RAILWAY_VOLUME_MOUNT_PATH`.
3) If you want to set it explicitly, set:
  - `DB_PATH=/data/db.sqlite3` (use the mount path you configured)

### Notes

- Make sure the bot is **admin** in the channel used for membership gating.
- After changing Variables, redeploy or restart the service.

## Behavior

- `/start` sends a welcome message + two inline buttons:
  - Join channel
  - Confirm membership
- Confirm checks membership via `getChatMember`.
- After membership is confirmed (or if already a member), a quick reply menu is shown:
  - حساب کاربری
  - رزرو تایم
  - ارتباط با ادمین

## Extra Commands

- User opt-in notifications:
  - `/subscribe` join notifications
  - `/unsubscribe` leave notifications

- Admin/Owner:
  - `/amar` shows professional stats (requires `OWNER_CHAT_ID` or `BOT_ADMIN_IDS`)
  - `/hamgani` starts a broadcast to subscribed users only
  - `/cancel_hamgani` cancels the broadcast step
  - `/takhfif` creates a discount code (wizard)
  - `/cancel_takhfif` cancels the discount wizard

## Reminder Job (30 minutes before)

- The bot runs a repeating JobQueue task that checks booked reservations and sends a reminder message to admins about 30 minutes before.
- Configure via `.env`: `REMINDER_MINUTES_BEFORE`, `REMINDER_INTERVAL_SECONDS`, `REMINDER_WINDOW_SECONDS`.


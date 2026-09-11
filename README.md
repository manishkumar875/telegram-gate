# 🚗 Telegram YouTube Gate

A free Telegram bot that asks people to subscribe to your YouTube channel
**before** it hands them the invite link to your existing Telegram group.

```
Instagram bio link
        ↓
   Telegram bot   ──►  "Subscribe to our YouTube channel"
        ↓
 [▶️ Subscribe on YouTube]   ← opens YouTube, Subscribe pop-up ready
        ↓
   user comes back
        ↓
 [✅ I've Subscribed]
        ↓
 [🚀 JOIN TELEGRAM GROUP]   ← your existing group, untouched
```

**Your existing group is never modified.** No members are removed, no group is
created, no existing invite link is revoked. The bot only ever *hands out* a
link, or *approves* people who ask to join.

---

## ⚠️ Read this first: what is actually verified

This is the single most important thing to understand about this project.

| Mode | What happens | Is the subscription really checked? |
|---|---|---|
| **`honor`** (default, Phase 1) | The bot asks "did you subscribe?" and believes the answer | **NO.** Nothing is checked. |
| **`youtube_oauth`** (Phase 2) | The user signs in with Google; the bot asks the official YouTube API | **YES.** Genuinely verified. |

### The free/default version does NOT verify anything

Telegram cannot see YouTube. YouTube will not tell an anonymous app whether a
particular person is subscribed. There is **no free trick** that changes this.
Anyone who tells you otherwise is either using the Google API (Phase 2, below)
or is not verifying anything.

So in `honor` mode this bot is an **honesty prompt plus a small speed bump**,
not a lock. And it says exactly that to your users, in its own words:

> 🤝 **Quick note before I hand over the link**
>
> This bot works on the **honour system**. It has **not** technically checked
> your YouTube account — it is trusting you that you subscribed.

That wording is deliberate, and there is an automated test
(`test_honor_mode_never_claims_technical_verification`) that fails the build if
anyone ever changes the bot to claim a check it did not perform.

**In practice the honour system still works well**, because most people who
came from your Instagram bio are willing to subscribe — they just need to be
asked. Expect a meaningful lift, not perfect enforcement.

If you want real enforcement, do [Phase 2](#-phase-2-real-youtube-verification).
If you want to stop link-sharing (a different problem), use
[`INVITE_MODE=request`](#choosing-an-invite-mode) — that part *is* fully
enforceable for free.

---

## Table of contents

1. [What you need before you start](#1-what-you-need-before-you-start)
2. [Create your Telegram bot (BotFather)](#2-create-your-telegram-bot-botfather)
3. [Get your Telegram group invite link](#3-get-your-telegram-group-invite-link)
4. [Find your YouTube channel URL](#4-find-your-youtube-channel-url)
5. [Install](#5-install)
6. [Create your `.env` file](#6-create-your-env-file)
7. [Check everything (preflight)](#7-check-everything-preflight)
8. [Run the bot](#8-run-the-bot)
9. [Test it](#9-test-it)
10. [Put the link in your Instagram bio](#10-put-the-link-in-your-instagram-bio)
11. [Choosing an invite mode](#choosing-an-invite-mode)
12. [Adding the bot to your group + permissions](#adding-the-bot-to-your-group--permissions)
13. [Phase 2: real YouTube verification](#-phase-2-real-youtube-verification)
14. [Free hosting](#-free-hosting)
15. [Troubleshooting](#-troubleshooting)
16. [Customising the wording](#-customising-the-wording)
17. [Privacy & security](#-privacy--security)
18. [Project structure](#-project-structure)
19. [Commands](#-commands)

---

## 1. What you need before you start

- A computer with **Python 3.10 or newer**
  (check by opening a terminal and running `python --version`)
- A Telegram account
- Your existing Telegram group, where **you are an admin**
- Your YouTube channel address

You do **not** need a Google account, a credit card, or a server for the
default version.

> **Windows tip:** if `python --version` says "not recognised", install Python
> from <https://www.python.org/downloads/> and tick
> **"Add python.exe to PATH"** on the first screen of the installer.

---

## 2. Create your Telegram bot (BotFather)

BotFather is Telegram's official bot for making bots.

1. Open Telegram and search for **`@BotFather`** (blue tick).
2. Send: `/newbot`
3. It asks for a **name** — this is the display name. Example: `Car Crew Gate`
4. It asks for a **username** — this must be unique and **end in `bot`**.
   Example: `carcrewgate_bot`
5. BotFather replies with something like:

```
Done! Congratulations on your new bot.
Use this token to access the HTTP API:
123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
```

**That long line is your bot token. Copy it.** You will paste it into `.env`
in step 6.

### 🔐 Keep the token secret

Anyone with that token can control your bot completely. Never post it in a
screenshot, a chat, a public GitHub repo, or a support forum.

If it ever leaks: `@BotFather` → `/mybots` → your bot → **API Token** →
**Revoke current token**. Then put the new one in `.env`.

### Where to find the token again later

`@BotFather` → `/mybots` → pick your bot → **API Token**

### Recommended extra settings (optional)

While you are in BotFather:

- `/setdescription` — the text shown before someone taps Start
- `/setabouttext` — the short text on the bot's profile
- `/setuserpic` — a profile picture

---

## 3. Get your Telegram group invite link

**Do not create a new group.** Use the one you already have.

**On mobile:**
1. Open your group.
2. Tap the **group name** at the top.
3. Tap the **pencil / Edit** icon → **Invite Links**.
4. Either copy the existing primary link, or tap **Create a New Link**.

**On desktop:**
1. Open your group.
2. Click the group name → **Manage Group** → **Invite Links**.

**Creating a new, dedicated link is recommended.** That way you can revoke
*just* the bot's link later without affecting anyone who joined by other means.

The link looks like one of these:

```
https://t.me/+AbCdEfGh123456      ← private group
https://t.me/yourgroupname        ← public group
```

Copy it. It goes in `.env` as `TELEGRAM_GROUP_INVITE_LINK`.

> Creating a new invite link **does not** affect existing members or existing
> links. Your 5,000+ members are not touched.

---

## 4. Find your YouTube channel URL

1. Open YouTube and go to your channel.
2. Copy the address from your browser's address bar.

Either of these formats works:

```
https://www.youtube.com/@yourhandle
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
```

On mobile: tap your profile picture → **Your channel** → **⋯ / Share** →
**Copy link**.

The bot automatically appends `?sub_confirmation=1`, which makes YouTube pop
the **Subscribe** dialog open immediately instead of just showing your channel
page. Fewer taps means fewer people dropping out.

---

## 5. Install

Open a terminal **in the project folder** (the one containing `run.py`).

```bash
cd "C:/Users/manis/OneDrive/Documents/telegram bot/telegram-youtube-gate"
```

Create an isolated environment so this project's packages don't clash with
anything else on your machine:

```bash
python -m venv .venv
```

Activate it:

**Windows (PowerShell or CMD):**
```bash
.venv\Scripts\activate
```

**Windows (Git Bash) / macOS / Linux:**
```bash
source .venv/Scripts/activate
```

> On macOS/Linux the path is `.venv/bin/activate` instead.

You will see `(.venv)` appear at the start of your prompt. Now install:

```bash
pip install -r requirements.txt
```

To also run the tests, install the dev extras instead:

```bash
pip install -r requirements-dev.txt
```

---

## 6. Create your `.env` file

`.env` is a plain text file holding your settings and secrets. It is already
listed in `.gitignore`, so it will never be committed to git.

Copy the template:

**Windows:**
```bash
copy .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Now open `.env` in Notepad, TextEdit, or VS Code and fill in the three required
values:

```ini
TELEGRAM_BOT_TOKEN=123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
TELEGRAM_GROUP_INVITE_LINK=https://t.me/+AbCdEfGh123456
YOUTUBE_CHANNEL_URL=https://www.youtube.com/@yourchannel
COMMUNITY_NAME=the Car Crew
```

**Rules:**
- No spaces around `=` → `KEY=value`, not `KEY = value`
- No quotes needed
- Lines starting with `#` are comments

`.env.example` documents every other option with a full explanation.

### 🔐 Never do these

- ❌ Never paste your token into a public repo, Discord, or a screenshot
- ❌ Never commit `.env` (it's git-ignored, keep it that way)
- ❌ Never put the token directly into a `.py` file

---

## 7. Check everything (preflight)

Before running for real, run the checker. It tests your Python version,
dependencies, `.env`, database, bot token, and group permissions — and tells
you in plain English what is wrong.

```bash
python scripts/preflight.py
```

A healthy run ends with:

```
==============================================================
  RESULT: ready to run.
==============================================================

  Start the bot with:   python run.py
```

It also prints the exact link to put in your Instagram bio.

---

## 8. Run the bot

```bash
python run.py
```

You'll see:

```
==============================================================
  Telegram YouTube Gate
==============================================================
  Verification : honor
  Invite mode  : static
  ...
==============================================================
  Press Ctrl+C to stop.

... | INFO | Connected to Telegram as @yourbot_bot (id 123456789)
... | INFO | Bot is running. Open https://t.me/yourbot_bot and send /start.
```

**Leave this window open.** The bot only works while this is running.
Press **Ctrl+C** to stop it cleanly.

---

## 9. Test it

### A. Automated: the full journey, no Telegram account needed

This launches the real bot against a fake Telegram server, plays a scripted
conversation through it, and checks every message and button:

```bash
python scripts/smoke_test.py
```

Expected ending:

```
  RESULT: the complete user journey works on this machine.
```

### B. Automated: the unit and integration test suite

```bash
python -m pytest
```

Expected: `232 passed`.

### C. Manual: test it yourself in Telegram

1. Make sure `python run.py` is running.
2. Open Telegram, search for your bot's username, open it.
3. Tap **Start** (or send `/start`).
4. You should see the welcome message with two buttons.
5. Tap **▶️ Subscribe on YouTube** → your channel opens with the Subscribe
   dialog ready.
6. Come back to Telegram, tap **✅ I've Subscribed**.
7. Read the honesty note, tap **✅ I've Subscribed** again.
8. You get **🚀 JOIN TELEGRAM GROUP**. Tap it — it opens your existing group.

Also try:
- `/help`, `/status`, `/privacy`
- Sending random text (`asdfgh`) — you should get a gentle nudge, not an error
- `/start` again — you should get the link straight back, not the whole funnel
- `/stats` — works only if your Telegram ID is in `ADMIN_USER_IDS`
  (get your ID from `@userinfobot`)

---

## 10. Put the link in your Instagram bio

Your bot's link is:

```
https://t.me/YOUR_BOT_USERNAME
```

(`preflight.py` prints this for you.)

Put **that** in your Instagram bio instead of the group link. Everyone now
goes through the gate.

---

## Choosing an invite mode

`INVITE_MODE` in `.env` controls how people actually get in.

| Mode | Bot must be group admin? | Can the link be shared with non-subscribers? | Best for |
|---|---|---|---|
| `static` *(default)* | No | **Yes** — it's one fixed link | Getting started fast |
| `unique` | Yes | No — single-use, expires in 24h | Stopping casual re-sharing |
| `request` | Yes | **No — the bot approves each person individually** | The strongest gate |

### How `request` mode works (recommended upgrade)

The bot creates an invite link that puts people into Telegram's **pending join
request** queue instead of adding them directly. The bot then approves *only*
people it has seen complete the gate. Even if someone forwards the link to a
hundred friends, none of them get in without going through your bot.

This is fully free and fully enforceable — unlike the YouTube check itself.

To enable it:

```ini
INVITE_MODE=request
TELEGRAM_GROUP_ID=-1001234567890
```

Get the group ID by running, with the bot already in your group:

```bash
python -m src.tools.find_group_id
```

Then send any message in your group. The ID is printed for you.

---

## Adding the bot to your group + permissions

Only needed for `INVITE_MODE=unique` or `INVITE_MODE=request`.
**`static` mode needs none of this.**

### Add the bot

1. Open your group → tap the group name.
2. **Add Members** / **Add Subscribers**.
3. Search your bot's username → add it.

### Make it an admin

1. Group name → **Administrators** → **Add Admin** → pick your bot.
2. Turn **ON** this permission:

   | Permission | Needed? | Why |
   |---|---|---|
   | **Invite Users via Link** | ✅ **REQUIRED** | Create invite links and approve join requests |
   | Delete Messages | ❌ No | |
   | Ban Users | ❌ No | |
   | Pin Messages | ❌ No | |
   | Manage Video Chats | ❌ No | |
   | Change Group Info | ❌ No | |
   | Add New Admins | ❌ No | |

3. Save.

**Give it nothing else.** "Invite Users via Link" is the only permission this
bot needs. It cannot remove members, delete messages, or change your group
with only that permission.

Verify it worked:

```bash
python scripts/preflight.py
```

You should see:

```
  [PASS] The bot is an admin with invite rights - INVITE_MODE=request will work
  [INFO] Group: Your Group Name (id -1001234567890)
  [INFO] Current members: 5088 - none will be touched
```

---

## 🔐 Phase 2: real YouTube verification

This is the version that **genuinely checks** whether someone is subscribed.

### How it works

1. The bot gives the user a one-time link to **Google's own sign-in page**.
2. The user signs in on `accounts.google.com` and grants one read-only
   permission: *see your YouTube subscriptions*.
3. Google sends your app a short-lived code.
4. Your app exchanges it for an access token and asks the official
   **YouTube Data API v3**:
   *"is this account subscribed to channel `UC...`?"*
5. The token is **immediately revoked** and thrown away.
6. Only a yes/no is stored against the Telegram user ID.

The bot never sees the user's Google password — authentication happens entirely
on Google's servers.

### Be honest with yourself about the trade-offs

| | |
|---|---|
| 💰 Cost | Free (Google Cloud free tier) |
| ⏱️ Setup | ~30–45 minutes |
| 🌐 Needs | A public **https://** address |
| 🚧 **Google verification** | `youtube.readonly` is a **sensitive scope**. Until Google approves your OAuth consent screen, your app stays in *Testing* mode: only accounts you list as **Test users** (max 100) can complete the flow. Everyone else hits a warning screen. Full verification means submitting your app for review and can take weeks. |
| 📉 **Conversion drop** | Many people will *not* sign in with Google to join a Telegram group. Expect noticeably fewer completions than the honour system. |
| 📊 Quota | 10,000 units/day by default; each check costs 1 unit → ~10,000 checks/day |
| ⚠️ Limitation | It proves the subscription existed **at the moment of the check**. Someone can unsubscribe afterwards. |

**My honest recommendation:** start with `honor` + `INVITE_MODE=request`. That
combination is free, instant, has no conversion penalty, and genuinely stops
link-sharing. Move to Phase 2 only if you truly need to enforce subscriptions.

### Setup

**Step 0 — Rehearse it first (2 minutes, no Google account needed)**

Before spending 45 minutes in Google Cloud, prove the code on your side works:

```bash
python scripts/oauth_rehearsal.py
```

This starts the real web server, builds a real Google consent URL, stands in
for Google, and makes a real request to `/oauth/callback` — then checks the
success path, the not-subscribed path, and that replayed links, forged state,
expired links, denied consent, and API errors all **fail closed**.

Run it again after filling in `.env` and it validates *your* values, including
printing the exact redirect URI you must register with Google.

**Step 1 — Get your channel ID**

Phase 2 needs the `UC...` form of your channel ID.

```bash
python -m src.tools.resolve_channel
```

This needs a free `YOUTUBE_API_KEY` in `.env`. To get one:
Google Cloud Console → **APIs & Services → Credentials → Create credentials →
API key**, then enable **YouTube Data API v3** for the project.

Or find it manually: open your channel, **View Page Source**, search for
`externalId` — the `UC...` value next to it is your channel ID.

**Step 2 — Create a Google Cloud project**

1. <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.

**Step 3 — Configure the OAuth consent screen**

1. **APIs & Services → OAuth consent screen**
2. User type: **External** → Create
3. Fill in app name, your support email, developer email.
4. **Scopes → Add or remove scopes** → add:
   `https://www.googleapis.com/auth/youtube.readonly`
5. **Test users → Add users** → add your own Google account (and anyone else
   who should be able to test).
6. Save.

**Step 4 — Create OAuth credentials**

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Web application**
3. Under **Authorised redirect URIs**, click **Add URI** and enter *exactly*:

   ```
   https://YOUR-PUBLIC-ADDRESS/oauth/callback
   ```

   This must match `OAUTH_PUBLIC_BASE_URL` + `/oauth/callback` character for
   character — no trailing slash, correct `http`/`https`.

4. Copy the **Client ID** and **Client secret**.

**Step 5 — Get a public https address**

Use a Cloudflare quick tunnel — unlike ngrok it needs **no account and no
credit card**. Install once:

```bash
winget install --id Cloudflare.cloudflared -e
```

Then, in its own terminal:

```bash
python scripts/tunnel.py
```

It opens the tunnel, writes the URL into `.env` as `OAUTH_PUBLIC_BASE_URL`
automatically, and prints the exact redirect URI to paste into Google Cloud.
Keep that window open — closing it kills the tunnel.

> The URL **changes every restart**, and you must update the Authorised
> redirect URI in Google Cloud to match each time. `tunnel.py` prints the new
> one. For anything permanent, deploy to a host with a stable URL
> (see [Free hosting](#-free-hosting)).

(ngrok also works if you prefer it: `ngrok http 8080`, then set
`OAUTH_PUBLIC_BASE_URL` yourself. It requires a free account.)

**Step 6 — Fill in `.env`**

```ini
VERIFICATION_MODE=youtube_oauth
YOUTUBE_CHANNEL_ID=UC_x5XG1OV2P6uZZ5FSM9Ttw
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxx
OAUTH_PUBLIC_BASE_URL=https://a1b2-c3d4.ngrok-free.app
OAUTH_STATE_SECRET=paste-a-long-random-string-here
WEB_PORT=8080
```

Generate the state secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Step 7 — Verify and run**

```bash
python scripts/preflight.py
python run.py
```

### Security of the Phase 2 implementation

- **Authorization Code flow + PKCE (S256)** — the current OAuth standard
- **Signed, single-use, expiring `state`** — HMAC-SHA256 signed, stored
  server-side, deleted on first use, 15-minute lifetime. Blocks CSRF and replay.
- **Minimum scope** — `youtube.readonly` only, nothing writable
- **No refresh tokens** — `access_type=online`, so long-lived access is never
  issued
- **Immediate revocation** — the access token is revoked right after the check
- **Nothing sensitive stored** — no Google tokens, IDs, or emails, ever

---

## ☁️ Free hosting

The bot must run continuously. Options, with **honest** current limitations —
free tiers change often, so **verify on the provider's own pricing page**.

| Option | Truly free? | The catch |
|---|---|---|
| **Your own PC** | ✅ Yes | Only online while your PC is on and awake. **Best for getting started.** |
| **Old laptop / Raspberry Pi** | ✅ Yes | You already own it; leave it running. Genuinely the best free option. |
| **Oracle Cloud Always Free** | ✅ Yes | Real always-free VM, but signup needs a card and ARM capacity is often unavailable in popular regions. |
| **Render (free web service)** | ⚠️ Partly | **Spins down after ~15 min with no HTTP traffic** and takes ~1 min to wake. Your bot is offline while asleep. Disk is wiped on restart (SQLite resets). |
| **Koyeb / Fly.io** | ⚠️ Check | Free allowances have been repeatedly reduced. Confirm current terms before relying on them. |
| **Railway** | ❌ No | Trial credit only, then paid. |
| **Heroku** | ❌ No | Free tier removed in Nov 2022. |
| **PythonAnywhere free** | ❌ **No — won't work** | Free accounts can only reach whitelisted sites. **`api.telegram.org` is not whitelisted**, so the bot cannot connect at all. |
| **Replit free** | ❌ No | No longer keeps projects always-on. |

### Deploying to Render

`render.yaml`, `Procfile`, and `runtime.txt` are already included.

1. Push this project to GitHub. **`.env` is git-ignored — do not force-add it.**
2. <https://dashboard.render.com/> → **New → Blueprint** → connect your repo.
3. Render reads `render.yaml` and prompts for your secrets. Enter them **in
   Render's dashboard**, never in the repo.
4. Deploy.

**About the spin-down:** free Render services sleep without HTTP traffic. The
bot serves `/health` (it binds Render's `$PORT` automatically), so you can
point a free uptime monitor like UptimeRobot at
`https://your-service.onrender.com/health` every 10 minutes to keep it awake.
Be aware this consumes your free instance hours faster.

**About the database:** Render's free disk is ephemeral. Your SQLite file is
wiped on every deploy/restart, so users would need to re-confirm. For this bot
that is usually acceptable — but know it happens.

### Deploying with Docker

A `Dockerfile` is included (runs as a non-root user):

```bash
docker build -t telegram-youtube-gate .
docker run -d --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  --name yt-gate telegram-youtube-gate
```

The `-v` mount keeps your database on the host so it survives restarts.

### Keeping it running on your own PC

**Windows** — create `start-bot.bat`:

```bat
@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python run.py
pause
```

Double-click it to start. To run at login, press `Win+R`, type `shell:startup`,
and put a shortcut to the `.bat` file in that folder.

---

## 🔧 Troubleshooting

### "CONFIGURATION PROBLEM - the bot cannot start yet"

The bot is telling you exactly which `.env` values are wrong. Fix the numbered
items and run again. If you have no `.env` yet, see [step 6](#6-create-your-env-file).

### "TELEGRAM_BOT_TOKEN was rejected by Telegram"

The token is wrong, has a typo, or was revoked.
`@BotFather` → `/mybots` → your bot → **API Token** → copy the whole line again.
Make sure there are no spaces, quotes, or line breaks.

### `ModuleNotFoundError: No module named 'telegram'`

Dependencies aren't installed, or your virtual environment isn't active.

```bash
.venv\Scripts\activate
pip install -r requirements.txt
```

You should see `(.venv)` in your prompt.

### `python` is not recognised

Python isn't on your PATH. Reinstall from python.org and tick
**"Add python.exe to PATH"**. Or try `py` instead of `python`.

### The bot doesn't reply in Telegram

1. Is `python run.py` still running? It only works while that window is open.
2. Any red `ERROR` lines in that window?
3. Did you tap **Start** in the chat?
4. Are you messaging the right bot? Check the username.

### "I couldn't create your invite link just now"

`INVITE_MODE` is `unique` or `request`, but the bot isn't set up as a group
admin. See [permissions](#adding-the-bot-to-your-group--permissions), then run
`python scripts/preflight.py`.

### "The group link isn't set up yet"

`TELEGRAM_GROUP_INVITE_LINK` is empty in `.env`. See [step 3](#3-get-your-telegram-group-invite-link).

### Join requests aren't being approved

- `INVITE_MODE=request` and `TELEGRAM_GROUP_ID` set correctly?
- Bot is an admin **with "Invite Users via Link"**?
- Did the person actually complete the gate first? Unverified requests are
  deliberately left pending.
- Is the bot running? It can only approve while online.

### `find_group_id` never prints anything

Telegram's privacy mode hides normal group messages from bots. Either:
- `@BotFather` → `/mybots` → your bot → **Bot Settings → Group Privacy → Turn off**, or
- Make the bot a group admin (admins always see messages).

Then remove and re-add the bot to the group and try again.

### Phase 2: "redirect_uri_mismatch"

`OAUTH_PUBLIC_BASE_URL` + `/oauth/callback` must match the **Authorised
redirect URI** in Google Cloud *exactly* — including `https://` and with no
trailing slash. Compare them character by character. The exact value the bot
expects is printed in the logs at startup.

### Phase 2: "Google hasn't verified this app"

Expected while your consent screen is in *Testing*. Only accounts added under
**Test users** can proceed. See the [Phase 2 trade-offs](#be-honest-with-yourself-about-the-trade-offs).

### Phase 2: "That verification link expired"

Links are single-use and last 15 minutes, by design. Tap **🔁 Check again** for
a fresh one.

### Ctrl+C doesn't stop the bot

It should — the bot installs proper Windows handlers. If it ever hangs, close
the terminal window, or run `taskkill /F /IM python.exe` (this kills *all*
Python processes).

---

## 🎨 Customising the wording

**Every message the bot sends lives in one file:**
[`src/bot/copy.py`](src/bot/copy.py)

Open it, edit the text between the `"""triple quotes"""`, save, restart the bot.

Keep the `{placeholders}` — they're filled in automatically:
- `{community_name}` — from `COMMUNITY_NAME` in `.env`
- `{channel}` — your YouTube channel handle
- `{seconds}` — a countdown number

You can use `<b>bold</b>`, `<i>italic</i>`, and `<a href="...">links</a>`.

Button labels are at the top of the same file (`BTN_SUBSCRIBE`, etc.).

> Please keep the honour-system disclosure honest if you reword it. There is a
> test that fails if the bot starts claiming verification it didn't do.

---

## 🔒 Privacy & security

### What is stored

Only this, in a local SQLite file:

- Your Telegram **numeric user ID**
- When they first messaged the bot / were last seen
- Whether they reached the confirmation step
- Whether they confirmed, and by which method
- Whether they received the invite link (and which link)
- Whether they joined

### What is **never** stored

❌ Names, usernames, phone numbers, emails
❌ Message contents
❌ Google passwords (the bot never sees them — Google handles sign-in)
❌ Google access tokens or refresh tokens
❌ Any YouTube data beyond a yes/no

There is a test (`test_only_expected_columns_exist`) that fails if anyone adds
a column for personal data.

Users can send **`/forgetme`** to delete everything about themselves.

### Security measures in the code

- Secrets only ever come from environment variables; none are hard-coded
- `.env`, `data/`, and `*.sqlite3` are git-ignored; `.dockerignore` excludes them too
- **All** SQL uses parameterised queries — no string-built SQL anywhere
- Button data is validated against a strict allow-list; unknown payloads are dropped
- Per-user rate limiting on every interaction
- Text interpolated into messages is HTML-escaped
- Admin commands check the user ID against `ADMIN_USER_IDS` (empty = nobody)
- Join requests are only ever approved for the one configured group
- Diagnostics mask secrets (`1234...gh (49 chars)`)

Run the security tests any time:

```bash
python -m pytest tests/test_security.py
```

---

## 📁 Project structure

```
telegram-youtube-gate/
├── README.md                 ← you are here
├── .env.example              ← settings template (copy to .env)
├── .gitignore                ← keeps .env and the database out of git
├── requirements.txt          ← what to install
├── requirements-dev.txt      ← extras for running tests
├── run.py                    ← START HERE: python run.py
├── Dockerfile / Procfile / render.yaml / runtime.txt   ← deployment
│
├── src/
│   ├── config/settings.py    ← reads + validates .env
│   ├── console.py            ← Windows console UTF-8 fix
│   ├── database/db.py        ← SQLite storage
│   ├── bot/
│   │   ├── app.py            ← wiring and lifecycle
│   │   ├── handlers.py       ← what happens on each tap/command
│   │   ├── keyboards.py      ← the buttons
│   │   ├── invites.py        ← works with your EXISTING group
│   │   └── copy.py           ← ✏️ ALL WORDING - edit this to customise
│   ├── verification/
│   │   ├── base.py           ← the swappable interface
│   │   ├── honor.py          ← Phase 1: verifies NOTHING (honest about it)
│   │   └── youtube_oauth.py  ← Phase 2: real Google/YouTube check
│   ├── web/server.py         ← OAuth callback + /health
│   └── tools/                ← find_group_id, resolve_channel
│
├── scripts/
│   ├── setup_wizard.py       ← fill in .env, validating every value live
│   ├── tunnel.py             ← public https URL (no account needed)
│   ├── preflight.py          ← check your setup before running
│   ├── smoke_test.py         ← prove the whole journey works
│   ├── oauth_rehearsal.py    ← rehearse Phase 2 without a Google account
│   └── mock_telegram_api.py  ← fake Telegram, used by the smoke test
│
├── tests/                    ← 232 automated tests
└── data/                     ← your SQLite database (git-ignored)
```

### How Phase 2 slots in without a rewrite

`src/verification/base.py` defines a `Verifier` interface. The bot only ever
asks *"did this user pass?"* — it never talks to YouTube itself. Switching
`VERIFICATION_MODE` swaps the implementation underneath.

The interface carries a `was_technically_verified` flag. `HonorVerifier`
hard-codes it to `False`, which is what makes it impossible for the bot to
accidentally claim a check it never made.

To add another method later (a different platform, a manual allow-list),
implement `Verifier` and add one line to `build_verifier()`.

---

## 📋 Commands

**For everyone:**

| Command | What it does |
|---|---|
| `/start` | Begin — or get the group link again if already verified |
| `/help` | How it works |
| `/status` | Show progress through the steps |
| `/privacy` | What data is stored |
| `/forgetme` | Delete everything stored about you |

**For you (listed in `ADMIN_USER_IDS`):**

| Command | What it does |
|---|---|
| `/stats` | Funnel numbers: started → tapped subscribe → confirmed → got link → joined |

Get your Telegram user ID by messaging `@userinfobot`.

---

## Quick reference

```bash
# one-time setup
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env          # then edit .env

# fill in .env (validates each value against the real service)
python scripts/setup_wizard.py --status          # what is done / still missing
python scripts/setup_wizard.py                   # guided walkthrough
python scripts/setup_wizard.py --set KEY=VALUE   # set one value

# for SECRETS, prefer the clipboard: the value never appears in your shell
# history, and is never printed back to the screen.
#   1. copy the token/secret  2. run:
python scripts/setup_wizard.py --from-clipboard TELEGRAM_BOT_TOKEN
python scripts/setup_wizard.py --from-clipboard GOOGLE_CLIENT_SECRET

# check, test, run
python scripts/preflight.py       # is everything configured?
python scripts/smoke_test.py      # does the whole journey work?
python scripts/oauth_rehearsal.py # Phase 2: rehearse OAuth without Google
python scripts/tunnel.py          # Phase 2: public https URL (own terminal)
python -m pytest                  # run all 232 tests
python run.py                     # start the bot

# helpers
python -m src.tools.find_group_id      # find your group's numeric ID
python -m src.tools.resolve_channel    # find your UC... channel ID
```

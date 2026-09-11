"""EVERY piece of text the bot says lives in this file.

=============================================================================
  HOW TO CHANGE THE BOT'S WORDING
=============================================================================
  1. Edit the text between the triple quotes (\"\"\"...\"\"\") below.
  2. Save the file.
  3. Restart the bot.

  Keep the {curly_brace} placeholders - they get filled in automatically:
      {community_name}  -> COMMUNITY_NAME from your .env
      {channel}         -> your YouTube channel name/handle
      {group_link}      -> your Telegram group invite link
      {seconds}         -> a number of seconds

  The bot sends messages using Telegram's HTML mode, so you may use
  <b>bold</b>, <i>italic</i>, <code>code</code> and <a href="...">links</a>.
  If you want to write a literal < > or & character, write &lt; &gt; &amp;
=============================================================================
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# BUTTON LABELS
# ---------------------------------------------------------------------------

BTN_SUBSCRIBE = "▶️ Subscribe on YouTube"
BTN_CONFIRMED = "✅ I've Subscribed"
BTN_VERIFY_GOOGLE = "🔐 Verify with Google"
BTN_JOIN_GROUP = "🚀 JOIN TELEGRAM GROUP"
BTN_HELP = "❓ Help"
BTN_START_OVER = "🔄 Start over"
BTN_RECHECK = "🔁 Check again"


# ---------------------------------------------------------------------------
# 1. THE FIRST MESSAGE (sent on /start)
# ---------------------------------------------------------------------------

WELCOME = """🚗 <b>Welcome!</b>

To join {community_name} on Telegram:

1️⃣ Subscribe to our YouTube channel
2️⃣ Come back here
3️⃣ Tap ✅ I've Subscribed

👇 Subscribe here:"""


# Shown when someone taps "Subscribe on YouTube" (Telegram opens the link and
# we also update the chat so the next step is obvious).
AFTER_SUBSCRIBE_TAP = """▶️ <b>YouTube is opening…</b>

Hit the <b>SUBSCRIBE</b> button on the channel, then come straight back here
and tap <b>✅ I've Subscribed</b> below."""


# ---------------------------------------------------------------------------
# 2. THE HONOR-SYSTEM CONFIRMATION (Phase 1 - free, nothing is checked)
#
#    IMPORTANT: this text deliberately tells the user the truth - that the
#    bot is trusting them rather than checking. Please keep that honesty if
#    you reword it.
# ---------------------------------------------------------------------------

HONOR_DISCLOSURE = """🤝 <b>Quick note before I hand over the link</b>

This bot works on the <b>honour system</b>. It has <b>not</b> technically
checked your YouTube account — it is trusting you that you subscribed.

If you did subscribe: thank you, that genuinely helps the channel. 🙏
If you didn't yet, tap ▶️ Subscribe on YouTube first — it takes 5 seconds."""


TOO_FAST = """⏳ <b>That was quick!</b>

Please open the YouTube channel and tap <b>SUBSCRIBE</b> first.

Try again in {seconds} second(s)."""


# ---------------------------------------------------------------------------
# 3. SUCCESS - hand over the group link
# ---------------------------------------------------------------------------

SUCCESS = """✅ <b>Thanks!</b>

Here's the link to join our Telegram group:"""

SUCCESS_UNIQUE_LINK = """✅ <b>Thanks!</b>

Here's your <b>personal</b> invite link. It works once and only for you,
so please don't share it:"""

SUCCESS_JOIN_REQUEST = """✅ <b>Thanks!</b>

Tap the button below to request to join the group.
You'll be approved automatically within a few seconds. 🚀"""

ALREADY_VERIFIED = """👋 <b>Welcome back!</b>

You're already through the gate. Here's the group link again:"""

ALREADY_IN_GROUP = """🎉 You're already a member of the group — nothing else to do!

Enjoy, and thanks for supporting the channel. 🙏"""

JOIN_REQUEST_APPROVED = """🎉 <b>You're in!</b>

Your request to join {community_name} has been approved. See you inside!"""

JOIN_REQUEST_PENDING = """👋 I see you asked to join the group.

Before I can let you in, please complete the quick step below —
it takes about 10 seconds."""


# ---------------------------------------------------------------------------
# 4. GOOGLE / YOUTUBE REAL VERIFICATION (Phase 2)
# ---------------------------------------------------------------------------

OAUTH_INTRO = """🔐 <b>Let's verify your subscription</b>

Tap the button below to sign in with Google. We'll ask YouTube one question:
<i>"is this account subscribed to {channel}?"</i>

<b>What we can see:</b> only your list of YouTube subscriptions (read-only).
<b>What we can't do:</b> post, comment, change anything, or see your password.
<b>What we keep:</b> nothing but a yes/no answer — your Google access is
revoked the moment the check finishes.

The link below is personal to you and expires in 15 minutes."""

OAUTH_SUCCESS = """✅ <b>Verified!</b>

YouTube confirmed that your account is subscribed to {channel}. 🎉"""

OAUTH_NOT_SUBSCRIBED = """❌ <b>Not subscribed yet</b>

We checked with YouTube and that Google account isn't subscribed to
{channel} yet.

Please subscribe, then tap <b>🔁 Check again</b>.

<i>Tip: make sure you sign in with the same Google account you use on
YouTube — if you have several accounts, pick the one that subscribed.</i>"""

OAUTH_LINK_EXPIRED = """⌛ <b>That verification link expired</b>

For your security each link only works once and lasts 15 minutes.
Tap the button below to get a fresh one."""

OAUTH_FAILED = """⚠️ <b>Verification didn't complete</b>

Something went wrong while talking to Google — this is usually temporary.
Please tap the button below to try again."""

OAUTH_BROWSER_DONE = """<h2>✅ All done — you can close this tab</h2>
<p>Head back to Telegram to get your group link.</p>"""


# ---------------------------------------------------------------------------
# 5. HELP AND ERRORS
# ---------------------------------------------------------------------------

HELP = """ℹ️ <b>How this works</b>

This bot gives you the invite link to {community_name} after you subscribe
to our YouTube channel.

<b>Commands</b>
/start — begin (or restart) the process
/help — show this message
/status — see where you are in the process
/privacy — what data this bot stores

<b>Stuck?</b>
• Tap /start to begin again.
• Make sure you tapped SUBSCRIBE on YouTube, not just watched a video.
• If the group link doesn't open, copy it and paste it into Telegram's
  search bar.

{support_line}"""

SUPPORT_LINE_DEFAULT = "Still stuck? Reply here and a human will read it."

PRIVACY = """🔒 <b>What this bot stores</b>

• Your numeric Telegram user ID
• The time you first messaged the bot
• Whether you tapped the subscribe button
• Whether you confirmed / were verified
• Whether you were given the group link

That's it. This bot does <b>not</b> store your name, username, phone number,
email, messages, or any Google password. {oauth_privacy_line}

Send /forgetme at any time and everything above is deleted."""

PRIVACY_OAUTH_LINE = (
    "When you verify with Google, the access we receive is read-only, used for "
    "a single subscription check, and revoked immediately afterwards — we never "
    "store Google tokens."
)

PRIVACY_NO_OAUTH_LINE = "This bot never asks for your Google account at all."

FORGOTTEN = """🗑️ Done — everything this bot knew about you has been deleted.

Tap /start any time to begin again."""

UNKNOWN_TEXT = """🤔 I only understand buttons and a few commands.

Tap /start to begin, or /help if you're stuck."""

UNKNOWN_COMMAND = """🤔 I don't know that command.

Try /start or /help."""

RATE_LIMITED = "⏳ Slow down a moment, then try again."

GENERIC_ERROR = """⚠️ Something went wrong on my side — sorry about that.

Please tap /start to try again. If it keeps happening, let us know."""

NO_LINK_CONFIGURED = """⚠️ The group link isn't set up yet.

The bot owner needs to add TELEGRAM_GROUP_INVITE_LINK to the .env file.
(If you're the owner: see step 5 of the README.)"""

INVITE_FAILED = """⚠️ I couldn't create your invite link just now.

This usually means the bot isn't an admin of the group yet, or Telegram is
having a moment. Please try again in a minute — or contact the owner."""


# ---------------------------------------------------------------------------
# 6. STATUS
# ---------------------------------------------------------------------------

STATUS_TEMPLATE = """📊 <b>Your status</b>

{steps}

{next_step}"""

STATUS_NEXT_START = "👉 Tap /start to begin."
STATUS_NEXT_SUBSCRIBE = "👉 Tap ▶️ Subscribe on YouTube, then ✅ I've Subscribed."
STATUS_NEXT_CONFIRM = "👉 Tap ✅ I've Subscribed to get your link."
STATUS_NEXT_DONE = "👉 You're all set — you already have the group link."


# ---------------------------------------------------------------------------
# 7. ADMIN
# ---------------------------------------------------------------------------

ADMIN_ONLY = "🔒 That command is only for the bot owner."

STATS_TEMPLATE = """📈 <b>Funnel stats</b>

👥 Started the bot: <b>{total_users}</b>
▶️ Tapped subscribe: <b>{clicked_subscribe}</b>
✅ Confirmed: <b>{confirmed}</b>
🔗 Got the link: <b>{invites_sent}</b>
🎉 Joined the group: <b>{joined}</b>

<b>Verification breakdown</b>
🤝 Honour system: <b>{honor_confirmed}</b>
🔐 Google-verified: <b>{oauth_verified}</b>

<i>Conversion (started → got link): {conversion}%</i>"""

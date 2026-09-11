"""Typed, validated configuration loaded from environment variables.

Nothing in this file contains secrets. Every value comes from the process
environment (usually populated from a local ``.env`` file that is git-ignored).

Design goal: fail loudly and with a *beginner-friendly* message at startup
rather than crashing halfway through a user conversation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

# Repository root = two levels up from src/config/settings.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when the .env configuration is missing or invalid.

    The message is written for a non-programmer: it says what is wrong and
    exactly which variable to fix.
    """


class VerificationMode(str, Enum):
    """How the bot decides that somebody is subscribed on YouTube.

    HONOR
        Phase 1 (free, no Google account needed). The user *tells* us they
        subscribed. Nothing is technically verified. This is clearly disclosed
        to the user in the bot's own wording.

    YOUTUBE_OAUTH
        Phase 2 (free to run, but needs a Google Cloud project). The user signs
        in with Google and we query the official YouTube Data API v3 to check
        whether their account really is subscribed to the channel.
    """

    HONOR = "honor"
    YOUTUBE_OAUTH = "youtube_oauth"


class InviteMode(str, Enum):
    """How the verified user is let into the EXISTING Telegram group.

    STATIC
        Send the fixed invite link from ``TELEGRAM_GROUP_INVITE_LINK``.
        Simplest. The bot does not need to be an admin of the group.
        Downside: the link can be forwarded to people who never subscribed.

    UNIQUE
        The bot generates a single-use, expiring invite link per verified user
        via ``createChatInviteLink``. Forwarding it is useless once consumed.
        Requires the bot to be a group admin with "Invite Users via Link".

    REQUEST
        The bot uses a "join request" invite link. Anyone can tap it, but
        Telegram holds them in a pending queue and the bot only *approves*
        people it has verified. This is the strongest gate.
        Requires the bot to be a group admin with "Invite Users via Link".
    """

    STATIC = "static"
    UNIQUE = "unique"
    REQUEST = "request"


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off", ""}

# Telegram bot tokens look like: 123456789:AAF-abcdefgh...
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

# YouTube channel IDs always start with UC and are 24 chars long.
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

_PLACEHOLDERS = {
    "",
    "changeme",
    "your_bot_token_here",
    "your-bot-token-here",
    "paste_your_token_here",
    "xxx",
    "todo",
}


def _clean(raw: str | None) -> str:
    """Strip whitespace and the quotes people accidentally paste from tutorials."""
    if raw is None:
        return ""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _env(name: str, default: str = "") -> str:
    return _clean(os.getenv(name)) or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False if raw else default
    raise ConfigError(
        f"{name} must be true or false (you wrote {raw!r}). "
        f"Use {name}=true or {name}=false in your .env file."
    )


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number (you wrote {raw!r})."
        ) from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be {minimum} or greater (you wrote {value}).")
    return value


def _env_id_list(name: str) -> tuple[int, ...]:
    """Parse a comma-separated list of Telegram user IDs."""
    raw = _env(name)
    if not raw:
        return ()
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"{name} must be numeric Telegram user IDs separated by commas "
                f"(for example: 111111111,222222222). {chunk!r} is not a number."
            ) from exc
    return tuple(out)


def _require_https(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            f"{name} must be a full web address starting with https:// "
            f"(you wrote {value!r})."
        )
    return value


# --------------------------------------------------------------------------
# YouTube helpers
# --------------------------------------------------------------------------


def extract_channel_id(youtube_url: str) -> str | None:
    """Return the UC... channel ID if the URL already contains one.

    ``https://www.youtube.com/channel/UCabc...`` -> ``UCabc...``
    ``https://www.youtube.com/@somehandle``      -> ``None`` (needs API lookup)
    """
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", youtube_url)
    return match.group(1) if match else None


def extract_channel_handle(youtube_url: str) -> str | None:
    """Return the ``@handle`` part of a modern YouTube URL, if present."""
    match = re.search(r"youtube\.com/@([A-Za-z0-9_.\-]{3,30})", youtube_url)
    return f"@{match.group(1)}" if match else None


def build_subscribe_url(youtube_url: str, channel_id: str | None) -> str:
    """Build the link used by the "Subscribe on YouTube" button.

    When we know the channel ID we can use YouTube's official
    ``?sub_confirmation=1`` parameter, which pops the Subscribe dialog open
    immediately instead of just showing the channel page. Fewer taps means
    fewer people dropping out of the funnel.
    """
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}?sub_confirmation=1"
    if "sub_confirmation" in youtube_url:
        return youtube_url
    separator = "&" if "?" in youtube_url else "?"
    return f"{youtube_url}{separator}sub_confirmation=1"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """All runtime configuration, already validated."""

    # --- Telegram -------------------------------------------------------
    bot_token: str
    api_base_url: str
    group_invite_link: str
    group_chat_id: int | None
    invite_mode: InviteMode
    admin_user_ids: tuple[int, ...]

    # --- YouTube --------------------------------------------------------
    youtube_channel_url: str
    youtube_channel_id: str | None
    youtube_channel_handle: str | None
    youtube_api_key: str

    # --- Verification ---------------------------------------------------
    verification_mode: VerificationMode
    min_seconds_before_confirm: int

    # --- Google OAuth (Phase 2) ----------------------------------------
    google_client_id: str
    google_client_secret: str
    oauth_public_base_url: str
    oauth_state_secret: str
    web_host: str
    web_port: int

    # --- Storage / ops --------------------------------------------------
    database_path: Path
    log_level: str
    health_check_port: int | None

    # --- Branding -------------------------------------------------------
    community_name: str
    bot_username: str = ""  # filled in at runtime from getMe()

    # Derived, computed in __post_init__
    subscribe_url: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscribe_url",
            build_subscribe_url(self.youtube_channel_url, self.youtube_channel_id),
        )

    # -- convenience -----------------------------------------------------

    @property
    def oauth_enabled(self) -> bool:
        return self.verification_mode is VerificationMode.YOUTUBE_OAUTH

    @property
    def needs_group_admin(self) -> bool:
        return self.invite_mode in {InviteMode.UNIQUE, InviteMode.REQUEST}

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.oauth_public_base_url.rstrip('/')}/oauth/callback"

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def redacted(self) -> dict[str, Any]:
        """A dict safe to print in logs: secrets are masked."""

        def mask(value: str) -> str:
            if not value:
                return "<not set>"
            # Plain ASCII only: the Windows console default code page cannot
            # render characters like the ellipsis and prints mojibake instead.
            return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"

        return {
            "bot_token": mask(self.bot_token),
            "group_invite_link": self.group_invite_link or "<not set>",
            "group_chat_id": self.group_chat_id,
            "invite_mode": self.invite_mode.value,
            "verification_mode": self.verification_mode.value,
            "youtube_channel_url": self.youtube_channel_url,
            "youtube_channel_id": self.youtube_channel_id or "<not set>",
            "youtube_api_key": mask(self.youtube_api_key),
            "google_client_id": mask(self.google_client_id),
            "google_client_secret": mask(self.google_client_secret),
            "oauth_public_base_url": self.oauth_public_base_url or "<not set>",
            "oauth_state_secret": mask(self.oauth_state_secret),
            "database_path": str(self.database_path),
            "admin_user_ids": list(self.admin_user_ids),
            "log_level": self.log_level,
        }


def load_settings(
    env_file: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
) -> Settings:
    """Read ``.env`` + environment and return validated :class:`Settings`.

    Raises :class:`ConfigError` with a human-readable explanation when
    something required is missing or malformed.
    """
    dotenv_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path, override=override)

    errors: list[str] = []

    # ---- Telegram bot token (required, always) -------------------------
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    if bot_token.lower() in _PLACEHOLDERS:
        errors.append(
            "TELEGRAM_BOT_TOKEN is empty. Open Telegram, message @BotFather, "
            "send /newbot, and paste the token it gives you into your .env file."
        )
    elif not _TOKEN_RE.match(bot_token):
        errors.append(
            "TELEGRAM_BOT_TOKEN does not look like a real token. It should look "
            "like 123456789:AAExample-TokenFromBotFather. Copy the whole line "
            "from @BotFather, with no spaces or quotes."
        )

    # ---- Invite mode ---------------------------------------------------
    invite_mode_raw = _env("INVITE_MODE", InviteMode.STATIC.value).lower()
    try:
        invite_mode = InviteMode(invite_mode_raw)
    except ValueError:
        errors.append(
            f"INVITE_MODE must be one of: static, unique, request "
            f"(you wrote {invite_mode_raw!r})."
        )
        invite_mode = InviteMode.STATIC

    # ---- Group invite link / group id ----------------------------------
    group_invite_link = _env("TELEGRAM_GROUP_INVITE_LINK")
    if group_invite_link and not re.match(
        r"^https://t\.me/(\+|joinchat/|[A-Za-z0-9_]{4,})", group_invite_link
    ):
        errors.append(
            "TELEGRAM_GROUP_INVITE_LINK must be a t.me link, for example "
            "https://t.me/+AbCdEf123456 or https://t.me/yourpublicgroup. "
            "Get it from your group: Group name > Invite Links."
        )

    group_chat_id_raw = _env("TELEGRAM_GROUP_ID")
    group_chat_id: int | None = None
    if group_chat_id_raw:
        try:
            group_chat_id = int(group_chat_id_raw)
        except ValueError:
            errors.append(
                "TELEGRAM_GROUP_ID must be a number like -1001234567890 "
                f"(you wrote {group_chat_id_raw!r}). Run "
                "`python -m src.tools.find_group_id` to discover it."
            )

    if invite_mode is InviteMode.STATIC and not group_invite_link:
        errors.append(
            "TELEGRAM_GROUP_INVITE_LINK is empty. With INVITE_MODE=static the "
            "bot needs a ready-made invite link to your existing group."
        )
    if invite_mode in {InviteMode.UNIQUE, InviteMode.REQUEST} and group_chat_id is None:
        errors.append(
            f"INVITE_MODE={invite_mode.value} needs TELEGRAM_GROUP_ID (a number "
            "like -1001234567890) so the bot can manage invites for your group. "
            "Run `python -m src.tools.find_group_id` to discover it."
        )

    # ---- YouTube -------------------------------------------------------
    youtube_channel_url = _env("YOUTUBE_CHANNEL_URL")
    if not youtube_channel_url:
        errors.append(
            "YOUTUBE_CHANNEL_URL is empty. Open your channel in a browser and "
            "copy the address, for example https://www.youtube.com/@yourchannel"
        )
    else:
        try:
            _require_https("YOUTUBE_CHANNEL_URL", youtube_channel_url)
        except ConfigError as exc:
            errors.append(str(exc))

    youtube_channel_id = _env("YOUTUBE_CHANNEL_ID") or extract_channel_id(
        youtube_channel_url
    )
    if youtube_channel_id and not _CHANNEL_ID_RE.match(youtube_channel_id):
        errors.append(
            "YOUTUBE_CHANNEL_ID must start with UC and be 24 characters long, "
            f"for example UC_x5XG1OV2P6uZZ5FSM9Ttw (you wrote {youtube_channel_id!r}). "
            "Leave it blank if you are unsure - it is only required for "
            "VERIFICATION_MODE=youtube_oauth."
        )
        youtube_channel_id = None
    youtube_channel_handle = extract_channel_handle(youtube_channel_url)

    # ---- Verification mode --------------------------------------------
    verification_raw = _env("VERIFICATION_MODE", VerificationMode.HONOR.value).lower()
    try:
        verification_mode = VerificationMode(verification_raw)
    except ValueError:
        errors.append(
            f"VERIFICATION_MODE must be 'honor' or 'youtube_oauth' "
            f"(you wrote {verification_raw!r})."
        )
        verification_mode = VerificationMode.HONOR

    google_client_id = _env("GOOGLE_CLIENT_ID")
    google_client_secret = _env("GOOGLE_CLIENT_SECRET")
    oauth_public_base_url = _env("OAUTH_PUBLIC_BASE_URL").rstrip("/")
    oauth_state_secret = _env("OAUTH_STATE_SECRET")

    if verification_mode is VerificationMode.YOUTUBE_OAUTH:
        if not google_client_id:
            errors.append(
                "VERIFICATION_MODE=youtube_oauth needs GOOGLE_CLIENT_ID. "
                "Create an OAuth client in Google Cloud Console "
                "(see README section 'Phase 2')."
            )
        if not google_client_secret:
            errors.append(
                "VERIFICATION_MODE=youtube_oauth needs GOOGLE_CLIENT_SECRET."
            )
        if not oauth_public_base_url:
            errors.append(
                "VERIFICATION_MODE=youtube_oauth needs OAUTH_PUBLIC_BASE_URL, the "
                "public https:// address where Google can reach this app, for "
                "example https://my-gate.onrender.com"
            )
        else:
            try:
                _require_https("OAUTH_PUBLIC_BASE_URL", oauth_public_base_url)
            except ConfigError as exc:
                errors.append(str(exc))
        if not youtube_channel_id:
            errors.append(
                "VERIFICATION_MODE=youtube_oauth needs YOUTUBE_CHANNEL_ID (the "
                "UC... id of your channel) so it can check the subscription. "
                "Run `python -m src.tools.resolve_channel` to find it."
            )
        if len(oauth_state_secret) < 16:
            errors.append(
                "OAUTH_STATE_SECRET must be a random string of at least 16 "
                "characters. Generate one with: "
                'python -c "import secrets;print(secrets.token_urlsafe(32))"'
            )

    # ---- Numbers & paths ----------------------------------------------
    try:
        min_seconds_before_confirm = _env_int(
            "MIN_SECONDS_BEFORE_CONFIRM", 0, minimum=0
        )
    except ConfigError as exc:
        errors.append(str(exc))
        min_seconds_before_confirm = 0

    # Nearly every free host (Render, Koyeb, Railway, Fly, Heroku-likes) tells
    # the app which port to bind via $PORT. Honour it so deploying needs no
    # extra configuration, while still letting the .env override it.
    platform_port = _env("PORT")

    try:
        web_port = _env_int("WEB_PORT", 0, minimum=0) or 0
    except ConfigError as exc:
        errors.append(str(exc))
        web_port = 0
    if not web_port:
        try:
            web_port = int(platform_port) if platform_port else 8080
        except ValueError:
            errors.append(f"PORT must be a number (the host set PORT={platform_port!r}).")
            web_port = 8080

    health_check_port: int | None
    raw_health = _env("HEALTH_CHECK_PORT") or platform_port
    try:
        health_check_port = int(raw_health) if raw_health else None
    except ValueError:
        errors.append("HEALTH_CHECK_PORT must be a number, for example 8080.")
        health_check_port = None

    try:
        admin_user_ids = _env_id_list("ADMIN_USER_IDS")
    except ConfigError as exc:
        errors.append(str(exc))
        admin_user_ids = ()

    database_path_raw = _env("DATABASE_PATH", "data/gate.sqlite3")
    database_path = Path(database_path_raw)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    log_level = _env("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append(
            f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL "
            f"(you wrote {log_level!r})."
        )
        log_level = "INFO"

    if errors:
        raise ConfigError(_format_errors(errors))

    return Settings(
        bot_token=bot_token,
        api_base_url=_env("TELEGRAM_API_BASE_URL", "https://api.telegram.org/bot"),
        group_invite_link=group_invite_link,
        group_chat_id=group_chat_id,
        invite_mode=invite_mode,
        admin_user_ids=admin_user_ids,
        youtube_channel_url=youtube_channel_url,
        youtube_channel_id=youtube_channel_id,
        youtube_channel_handle=youtube_channel_handle,
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        verification_mode=verification_mode,
        min_seconds_before_confirm=min_seconds_before_confirm,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        oauth_public_base_url=oauth_public_base_url,
        oauth_state_secret=oauth_state_secret,
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=web_port,
        database_path=database_path,
        log_level=log_level,
        health_check_port=health_check_port,
        community_name=_env("COMMUNITY_NAME", "our community"),
    )


def _format_errors(errors: list[str]) -> str:
    lines = [
        "",
        "=" * 68,
        " CONFIGURATION PROBLEM - the bot cannot start yet",
        "=" * 68,
        "",
        f"Found {len(errors)} problem(s) in your .env file:",
        "",
    ]
    for index, message in enumerate(errors, start=1):
        lines.append(f"  {index}. {message}")
        lines.append("")
    lines.extend(
        [
            "-" * 68,
            "Fix the items above in the file called .env (in the project folder),",
            "then run the bot again.",
            "If you do not have a .env file yet, copy .env.example to .env:",
            "",
            "    cp .env.example .env        (macOS / Linux)",
            "    copy .env.example .env      (Windows)",
            "=" * 68,
        ]
    )
    return "\n".join(lines)

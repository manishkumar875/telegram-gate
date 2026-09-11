"""Security-focused tests.

These encode the promises made in the README, so that a future change that
breaks one of them fails the build instead of shipping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.bot.keyboards import Action
from src.config import InviteMode
from tests.conftest import build_gate, make_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    path
    for path in PROJECT_ROOT.rglob("*.py")
    if ".venv" not in path.parts and "__pycache__" not in path.parts
]


# ---------------------------------------------------------------------------
# no secrets in the repository
# ---------------------------------------------------------------------------


#: A credential-shaped string is tolerated only if it advertises itself as a
#: stand-in. Real secrets are random, so they do not contain these words.
FAKE_MARKERS = ("fake", "example", "test", "mock", "smoke", "placeholder", "rehearsal")


def _is_obviously_fake(match: str, path: Path) -> bool:
    haystack = f"{match} {path.name}".lower()
    return any(marker in haystack for marker in FAKE_MARKERS)


def _assert_no_credentials(pattern: re.Pattern[str], label: str) -> None:
    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.findall(text):
            assert _is_obviously_fake(match, path), (
                f"possible real {label} committed in {path}: {match[:12]}..."
            )


def test_no_real_bot_token_is_committed():
    """A live BotFather token must never appear in source."""
    _assert_no_credentials(re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"), "bot token")


def test_no_google_api_key_is_committed():
    _assert_no_credentials(re.compile(r"AIza[A-Za-z0-9_-]{30,}"), "Google API key")


def test_no_google_client_secret_is_committed():
    _assert_no_credentials(re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"), "Google client secret")


def test_the_credential_guard_actually_catches_a_real_looking_secret(tmp_path):
    """Guard the guard: a random-looking secret must NOT pass."""
    real_looking = "GOCSPX-9zQ4vB2xK7mN1pR8sT3wY6aC"
    assert not _is_obviously_fake(real_looking, Path("src/bot/app.py"))
    # ...while the stand-ins used in this repo are correctly tolerated.
    assert _is_obviously_fake("GOCSPX-exampleSecretValue123", Path("tests/x.py"))


def test_env_file_is_git_ignored():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    stripped = {line.strip() for line in ignored}
    assert ".env" in stripped
    assert "!.env.example" in stripped  # the template stays shareable
    assert any(line.strip() in {"data/", "*.sqlite3"} for line in ignored)


def test_env_example_contains_no_filled_in_values():
    """The template must ship empty, so nobody inherits someone else's keys."""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "OAUTH_STATE_SECRET",
        "YOUTUBE_API_KEY",
        "TELEGRAM_GROUP_INVITE_LINK",
    ):
        for line in text.splitlines():
            if line.startswith(f"{key}="):
                assert line.strip() == f"{key}=", f"{key} must be blank in .env.example"


def test_dockerignore_excludes_secrets_and_data():
    text = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in text
    assert "data/" in text


# ---------------------------------------------------------------------------
# secret handling at runtime
# ---------------------------------------------------------------------------


def test_redacted_never_exposes_a_secret(tmp_path):
    settings = make_settings(
        tmp_path,
        google_client_secret="the-real-client-secret",
        google_client_id="the-real-client-id",
        oauth_state_secret="the-real-state-secret-value",
        youtube_api_key="the-real-api-key",
    )
    dumped = str(settings.redacted())
    for secret in (
        settings.bot_token,
        "the-real-client-secret",
        "the-real-state-secret-value",
        "the-real-api-key",
    ):
        assert secret not in dumped


def test_settings_repr_is_not_used_for_logging(tmp_path):
    """Guard: repr() of Settings DOES contain secrets, so only redacted() may be logged."""
    import re
    
    pattern = re.compile(r"logger\.[a-z]+\(.*settings\)|print\(settings\)")
    offending_files = []
    
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offending_files.append(str(path.relative_to(PROJECT_ROOT)))
            
    assert not offending_files, f"Settings logged whole in: {offending_files}"


async def test_oauth_state_secret_is_required_to_forge_state(tmp_path, db):
    from src.verification.youtube_oauth import sign_state, verify_state

    real = "the-only-valid-secret-value"
    state = sign_state("n1", real)
    assert verify_state(state, real) == "n1"
    for guess in ("", "wrong", real + "x", real[:-1]):
        assert verify_state(state, guess) is None


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------


async def test_stats_denied_to_non_admin(gate):
    message = await gate.send_command("stats")
    assert "only for the bot owner" in message.text
    assert "Funnel stats" not in message.text


async def test_stats_denied_when_no_admins_configured(tmp_path, db, recorder, fake_bot):
    settings = make_settings(tmp_path, admin_user_ids=())
    gate = build_gate(settings, db, recorder, fake_bot, user_id=999)
    message = await gate.send_command("stats")
    assert "only for the bot owner" in message.text


async def test_admin_check_is_exact_not_substring(tmp_path):
    settings = make_settings(tmp_path, admin_user_ids=(12345,))
    assert settings.is_admin(12345) is True
    assert settings.is_admin(1234) is False
    assert settings.is_admin(123456) is False


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "gate:confirm; DROP TABLE users",
        "gate:confirm\n",
        "../../gate:confirm",
        "GATE:CONFIRM",
        "gate:admin",
        "<script>alert(1)</script>",
        "\x00gate:confirm",
        "gate:" + "a" * 5000,
        None,
    ],
)
def test_callback_allow_list_rejects_everything_unexpected(payload):
    assert Action.parse(payload) is None


def test_callback_allow_list_accepts_only_the_known_actions():
    for action in Action:
        assert Action.parse(action.value) is action


async def test_hostile_callback_cannot_obtain_the_invite(gate, db):
    from tests.conftest import FakeCallbackQuery, FakeContext

    for payload in ["gate:confirm ", "gate:invite", "'; --", "gate:approve"]:
        query = FakeCallbackQuery(payload, gate.user, gate.chat, gate.recorder)
        await gate.handlers.on_callback(
            gate._update(callback_query=query), FakeContext(bot=gate.bot)
        )
    assert not any("TestInviteLink" in text for text in gate.recorder.texts)


async def test_community_name_is_html_escaped(tmp_path, db, recorder, fake_bot):
    """A stray < or & in .env must not break message rendering."""
    settings = make_settings(tmp_path, community_name="Bob & <b>Friends</b>")
    gate = build_gate(settings, db, recorder, fake_bot)
    message = await gate.send_command("start")
    assert "&amp;" in message.text
    assert "&lt;b&gt;Friends&lt;/b&gt;" in message.text
    assert "<b>Friends</b>" not in message.text


# ---------------------------------------------------------------------------
# the group gate itself
# ---------------------------------------------------------------------------


async def test_join_request_ignored_without_a_configured_group(tmp_path, db, recorder, fake_bot):
    """Never approve someone into a chat we were not told to gate."""
    settings = make_settings(tmp_path, group_chat_ids=())
    gate = build_gate(settings, db, recorder, fake_bot)
    await db.touch_user(gate.user.id)
    await db.mark_confirmed(gate.user.id, "honor")

    await gate.request_join(group_chat_id=-100999999)

    assert fake_bot.approved == []


async def test_approve_refuses_without_a_group_id(tmp_path, db, fake_bot):
    from src.bot.invites import InviteService

    service = InviteService(make_settings(tmp_path, group_chat_ids=()), db)
    assert await service.approve_join_request(fake_bot, 1) is False
    assert fake_bot.approved == []


async def test_unverified_user_is_never_approved(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)  # got the disclosure, never confirmed

    await gate.request_join()

    assert fake_bot.approved == []


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


async def test_no_personal_data_is_stored(gate, db):
    """Drive a full journey, then prove nothing personal was written."""
    # Distinctive values that cannot collide with fixture data.
    gate.user.username = "zqx_username_marker"
    gate.user.first_name = "ZqxFirstNameMarker"

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)

    rows = list(await db.export_rows())
    assert len(rows) == 1
    stored = " ".join(str(value) for value in rows[0].values())

    for personal in (gate.user.username, gate.user.first_name):
        assert personal not in stored
    # Only the numeric id identifies the person.
    assert rows[0]["telegram_user_id"] == gate.user.id


async def test_forgetme_is_a_real_delete(gate, db):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    await gate.send_command("forgetme")

    assert list(await db.export_rows()) == []


async def test_honor_mode_never_asks_for_google(gate):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    combined = " ".join(gate.recorder.texts).lower()
    assert "google" not in combined
    assert "sign in" not in combined

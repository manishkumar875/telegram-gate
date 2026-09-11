"""The .env setup wizard: value extraction, validation, and safe writing."""

from __future__ import annotations

import pytest

from scripts import setup_wizard as wizard

# A realistic BotFather reply - this is what people actually copy.
BOTFATHER_REPLY = """Done! Congratulations on your new bot. You will find it at t.me/manish_gate_bot.

Use this token to access the HTTP API:
123456789:AAExampleTokenFromBotFatherAbCdEfGhIjKl

Keep your token secure and store it safely, it can be used by anyone to control your bot."""

VALID_TOKEN = "123456789:AAExampleTokenFromBotFatherAbCdEfGhIjKl"


# ---------------------------------------------------------------------------
# pulling the value out of a messy paste
# ---------------------------------------------------------------------------


def test_token_is_found_inside_a_full_botfather_reply():
    assert wizard.pick_value_line(BOTFATHER_REPLY, "TELEGRAM_BOT_TOKEN") == VALID_TOKEN


def test_single_line_paste_is_used_as_is():
    assert wizard.pick_value_line(f"  {VALID_TOKEN}  ", "TELEGRAM_BOT_TOKEN") == VALID_TOKEN


def test_unrelated_multiline_text_is_not_silently_accepted():
    """Prose with no token must fall through to the validator, not pick a line."""
    prose = "The Telegram bot has been created,\nadded to the group,\nand given permission."
    picked = wizard.pick_value_line(prose, "TELEGRAM_BOT_TOKEN")
    assert picked == prose.strip()  # whole text -> validator will reject it


def test_channel_id_is_found_in_a_paste():
    text = "Here is the info\nexternalId\nUC_x5XG1OV2P6uZZ5FSM9Ttw\nsomething else"
    assert wizard.pick_value_line(text, "YOUTUBE_CHANNEL_ID") == "UC_x5XG1OV2P6uZZ5FSM9Ttw"


def test_google_client_id_is_found_in_a_paste():
    text = "Client ID\n1234-abc.apps.googleusercontent.com\nClient secret\nGOCSPX-exampleXyz"
    assert (
        wizard.pick_value_line(text, "GOOGLE_CLIENT_ID")
        == "1234-abc.apps.googleusercontent.com"
    )


def test_group_id_is_found_in_a_paste():
    text = "GROUP NAME : manish\nGROUP ID   : -1001234567890"
    # The ID line has a prefix, so no line matches cleanly -> whole text.
    assert wizard.pick_value_line(text, "TELEGRAM_GROUP_ID") == text.strip()
    assert wizard.pick_value_line("-1001234567890", "TELEGRAM_GROUP_ID") == "-1001234567890"


def test_empty_paste_is_handled():
    assert wizard.pick_value_line("   \n  \n", "TELEGRAM_BOT_TOKEN") == ""


def test_unknown_key_falls_back_to_whole_text():
    assert wizard.pick_value_line("a\nb", "SOMETHING_ELSE") == "a\nb"


# ---------------------------------------------------------------------------
# .env writing preserves the file
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "TELEGRAM_BOT_TOKEN=\n"
        "\n"
        "# another comment\n"
        "INVITE_MODE=request\n"
        "COMMUNITY_NAME=our community\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wizard, "ENV_PATH", path)
    return path


def test_write_updates_in_place_and_keeps_comments(temp_env):
    wizard.write_env_value("TELEGRAM_BOT_TOKEN", VALID_TOKEN)
    text = temp_env.read_text(encoding="utf-8")

    assert f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n" in text
    assert "# a comment" in text
    assert "# another comment" in text
    assert "INVITE_MODE=request" in text
    # No duplicate key was appended.
    assert text.count("TELEGRAM_BOT_TOKEN=") == 1


def test_write_appends_a_missing_key(temp_env):
    wizard.write_env_value("BRAND_NEW_KEY", "hello")
    assert "BRAND_NEW_KEY=hello\n" in temp_env.read_text(encoding="utf-8")


def test_write_does_not_touch_commented_out_lines(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# VERIFICATION_MODE=honor\nVERIFICATION_MODE=youtube_oauth\n",
                    encoding="utf-8")
    monkeypatch.setattr(wizard, "ENV_PATH", path)

    wizard.write_env_value("VERIFICATION_MODE", "honor")
    text = path.read_text(encoding="utf-8")

    assert "# VERIFICATION_MODE=honor\n" in text  # comment untouched
    assert "VERIFICATION_MODE=honor\n" in text
    assert "youtube_oauth" not in text


def test_read_env_ignores_comments_and_strips_quotes(temp_env):
    temp_env.write_text(
        '# ignored=yes\nA="quoted"\nB=plain\n\nC=\n', encoding="utf-8"
    )
    values = wizard.read_env()
    assert values["A"] == "quoted"
    assert values["B"] == "plain"
    assert values["C"] == ""
    assert "ignored" not in values


# ---------------------------------------------------------------------------
# masking
# ---------------------------------------------------------------------------


def test_mask_never_reveals_a_long_secret():
    masked = wizard.mask(VALID_TOKEN)
    assert VALID_TOKEN not in masked
    assert "ExampleToken" not in masked
    assert "chars" in masked


def test_mask_hides_short_values_entirely():
    assert wizard.mask("secret12") == "*" * 8


def test_mask_handles_empty():
    assert wizard.mask("") == "<empty>"


# ---------------------------------------------------------------------------
# validators reject bad input without writing
# ---------------------------------------------------------------------------


async def test_bad_token_shape_is_rejected_without_network():
    ok, message, extra = await wizard.validate_bot_token("not-a-token")
    assert ok is False
    assert "BotFather" in message
    assert extra == {}


async def test_channel_url_with_id_also_fills_channel_id():
    ok, _message, extra = await wizard.validate_channel_url(
        "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", {}
    )
    assert ok is True
    assert extra["YOUTUBE_CHANNEL_ID"] == "UC_x5XG1OV2P6uZZ5FSM9Ttw"


async def test_non_youtube_url_is_rejected():
    ok, _message, _extra = await wizard.validate_channel_url("https://example.com", {})
    assert ok is False


@pytest.mark.parametrize(
    "value,expected_ok",
    [
        ("UC_x5XG1OV2P6uZZ5FSM9Ttw", True),
        ("UCtooshort", False),
        ("XX_x5XG1OV2P6uZZ5FSM9Ttw", False),
        ("", False),
    ],
)
async def test_channel_id_shape(value, expected_ok):
    ok, _message, _extra = await wizard.validate_channel_id(value, {})
    assert ok is expected_ok


@pytest.mark.parametrize(
    "value,expected_ok",
    [
        ("123-abc.apps.googleusercontent.com", True),
        ("123-abc.example.com", False),
        ("just-an-id", False),
    ],
)
async def test_google_client_id_shape(value, expected_ok):
    ok, _message, _extra = await wizard.validate_google_client_id(value, {})
    assert ok is expected_ok


async def test_public_url_must_be_https_without_trailing_slash():
    ok, message, _extra = await wizard.validate_public_url("http://x.com", {})
    assert ok is False and "https" in message

    ok, message, _extra = await wizard.validate_public_url("https://x.com/", {})
    assert ok is False and "trailing slash" in message


async def test_public_url_reports_the_exact_redirect_uri():
    ok, message, _extra = await wizard.validate_public_url(
        "https://abc.trycloudflare.com", {}
    )
    assert ok is True
    assert "https://abc.trycloudflare.com/oauth/callback" in message


async def test_client_secret_is_never_echoed(capsys):
    field = next(f for f in wizard.FIELDS if f.key == "GOOGLE_CLIENT_SECRET")
    assert field.secret is True
    ok, message, _extra = await wizard.validate_google_client_secret(
        "GOCSPX-exampleSecretValue", {}
    )
    assert ok is True
    assert "averysecretvalue" not in message

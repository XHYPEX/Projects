"""Tests for userbot.py, the bot's entry point (there is no main.py in this repo).

userbot.py builds its TelegramClient/Bot and loads routes/state as module-level
side effects, so tests import it fresh (see conftest.userbot_module) against a
tmp_path sandbox instead of the developer's real .env/routes.json/session files.

Coverage focuses on on_new_message, the handler that contains all of the bot's
actual forwarding logic (filtering, LLM polishing, reply-link mapping, flood
control, error handling).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter


def make_event(
    chat_id=-100111,
    sender_id=555,
    raw_text="This is a long enough signal message",
    media=None,
    photo=None,
    is_reply=False,
    reply_message=None,
    msg_id=1,
):
    event = SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        raw_text=raw_text,
        media=media,
        photo=photo,
        is_reply=is_reply,
        id=msg_id,
    )
    event.get_reply_message = AsyncMock(return_value=reply_message)
    event.download_media = AsyncMock(return_value=b"fake-jpeg-bytes")
    return event


@pytest.fixture
def bot(userbot_module, monkeypatch):
    fake_bot = AsyncMock()
    monkeypatch.setattr(userbot_module, "bot", fake_bot)
    return fake_bot


@pytest.fixture
def route(userbot_module):
    """Register a single active source -> target route, as main() would."""
    r = userbot_module.config.Route(
        source_chat=-100111, target_chat=-100222, sender_whitelist=set()
    )
    userbot_module.routes_by_chat_id[-100111] = r
    return r


def test_handler_is_registered_on_the_client(userbot_module):
    handlers = [cb for cb, _ in userbot_module.client.list_event_handlers()]
    assert userbot_module.on_new_message in handlers


async def test_every_message_is_logged_before_filtering(userbot_module, bot, route, caplog):
    # The point of the [MASUK] line is that it lands even for messages that get
    # dropped, so the log explains why something never arrived downstream.
    route.sender_whitelist = {111}
    with caplog.at_level("INFO", logger="signal-forwarder"):
        await userbot_module.on_new_message(
            make_event(sender_id=999, raw_text="pesan dari sender asing", msg_id=3)
        )

    assert "[MASUK]" in caplog.text
    assert "pesan dari sender asing" in caplog.text
    assert "[SKIP]" in caplog.text
    bot.send_message.assert_not_called()


async def test_short_message_is_logged_with_its_text(userbot_module, bot, route, caplog):
    with caplog.at_level("INFO", logger="signal-forwarder"):
        await userbot_module.on_new_message(make_event(raw_text="short"))

    assert "[MASUK]" in caplog.text
    assert "short" in caplog.text


async def test_polished_result_is_logged(userbot_module, bot, route, monkeypatch, caplog):
    monkeypatch.setattr(
        userbot_module, "polish_signal", AsyncMock(return_value="Polished output here")
    )
    bot.send_message.return_value = SimpleNamespace(message_id=1)

    with caplog.at_level("INFO", logger="signal-forwarder"):
        await userbot_module.on_new_message(make_event())

    assert "[HASIL]" in caplog.text
    assert "Polished output here" in caplog.text


async def test_unknown_route_is_ignored(userbot_module, bot):
    # No route registered for this chat_id.
    await userbot_module.on_new_message(make_event(chat_id=-999))
    bot.send_message.assert_not_called()


async def test_sender_not_in_whitelist_is_ignored(userbot_module, bot, route):
    route.sender_whitelist = {111, 222}
    await userbot_module.on_new_message(make_event(sender_id=999))
    bot.send_message.assert_not_called()


async def test_empty_message_without_media_is_ignored(userbot_module, bot, route):
    await userbot_module.on_new_message(make_event(raw_text="   "))
    bot.send_message.assert_not_called()


async def test_message_shorter_than_minimum_is_ignored(userbot_module, bot, route):
    await userbot_module.on_new_message(make_event(raw_text="short"))
    bot.send_message.assert_not_called()


async def test_llm_skip_result_is_not_forwarded(userbot_module, bot, route, monkeypatch):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value=None))
    await userbot_module.on_new_message(make_event())
    bot.send_message.assert_not_called()


async def test_llm_error_is_swallowed_and_nothing_is_sent(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(
        userbot_module, "polish_signal", AsyncMock(side_effect=RuntimeError("LLM down"))
    )
    await userbot_module.on_new_message(make_event())
    bot.send_message.assert_not_called()


async def test_happy_path_forwards_polished_text_and_records_mapping(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(
        userbot_module, "polish_signal", AsyncMock(return_value="Polished signal text")
    )
    bot.send_message.return_value = SimpleNamespace(message_id=4242)

    await userbot_module.on_new_message(make_event(msg_id=7))

    bot.send_message.assert_awaited_once_with(
        chat_id=-100222, text="Polished signal text", reply_to_message_id=None
    )
    assert userbot_module.sent_message_map[(-100111, 7)] == 4242


async def test_photo_message_is_forwarded_via_send_photo(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value="Caption"))
    bot.send_photo.return_value = SimpleNamespace(message_id=99)

    await userbot_module.on_new_message(make_event(photo=True))

    bot.send_photo.assert_awaited_once()
    _, kwargs = bot.send_photo.call_args
    assert kwargs["chat_id"] == -100222
    assert kwargs["caption"] == "Caption"
    assert kwargs["photo"].getvalue() == b"fake-jpeg-bytes"
    bot.send_message.assert_not_called()


async def test_reply_with_known_mapping_links_the_target_message(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value="Update"))
    userbot_module.sent_message_map[(-100111, 50)] = 777
    bot.send_message.return_value = SimpleNamespace(message_id=778)

    reply_message = SimpleNamespace(raw_text="Original signal", id=50)
    event = make_event(is_reply=True, reply_message=reply_message, msg_id=51)

    await userbot_module.on_new_message(event)

    bot.send_message.assert_awaited_once_with(
        chat_id=-100222, text="Update", reply_to_message_id=777
    )
    assert userbot_module.sent_message_map[(-100111, 51)] == 778


async def test_reply_target_deleted_falls_back_to_plain_send(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value="Update"))
    userbot_module.sent_message_map[(-100111, 50)] = 777
    bot.send_message.side_effect = [
        BadRequest("message to be replied not found"),
        SimpleNamespace(message_id=900),
    ]

    reply_message = SimpleNamespace(raw_text="Original signal", id=50)
    event = make_event(is_reply=True, reply_message=reply_message, msg_id=51)

    await userbot_module.on_new_message(event)

    assert bot.send_message.await_count == 2
    first_kwargs = bot.send_message.await_args_list[0].kwargs
    second_kwargs = bot.send_message.await_args_list[1].kwargs
    assert first_kwargs["reply_to_message_id"] == 777
    assert second_kwargs["reply_to_message_id"] is None
    assert userbot_module.sent_message_map[(-100111, 51)] == 900


async def test_flood_control_retries_then_succeeds(userbot_module, bot, route, monkeypatch):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value="Update"))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(userbot_module.asyncio, "sleep", sleep_mock)
    bot.send_message.side_effect = [RetryAfter(2), SimpleNamespace(message_id=321)]

    await userbot_module.on_new_message(make_event(msg_id=1))

    assert bot.send_message.await_count == 2
    sleep_mock.assert_awaited_once_with(3)
    assert userbot_module.sent_message_map[(-100111, 1)] == 321


async def test_send_failure_after_retries_is_reported_without_crashing(
    userbot_module, bot, route, monkeypatch
):
    monkeypatch.setattr(userbot_module, "polish_signal", AsyncMock(return_value="Update"))
    monkeypatch.setattr(userbot_module.asyncio, "sleep", AsyncMock())
    bot.send_message.side_effect = RuntimeError("network is down")

    # Should not raise: failures are logged/alerted, not propagated.
    await userbot_module.on_new_message(make_event(msg_id=1))

    assert (-100111, 1) not in userbot_module.sent_message_map

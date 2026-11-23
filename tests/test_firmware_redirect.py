import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import Update
import pytest

from firmware_bot.redirect import redirect_router
from test_firmware import RecordingSession


def test_old_bot_redirects_versions_attachments_and_old_variant_buttons_without_new_requests():
    async def scenario():
        session = RecordingSession()
        bot = Bot("123456:" + "A" * 35, session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(redirect_router("DriveModsSWBot"))
        common = {"message_id": 7, "date": 1795240000,
                  "chat": {"id": 101, "type": "private"},
                  "from": {"id": 101, "is_bot": False, "first_name": "Test"}}
        for number, content in enumerate([
            {"text": "LX2.KOR.S5W"},
            {"document": {"file_id": "old", "file_unique_id": "old"}},
        ], 1):
            await dispatcher.feed_update(bot, Update.model_validate({
                "update_id": number, "message": {**common, **content}}))
        await dispatcher.feed_update(bot, Update.model_validate({
            "update_id": 3, "callback_query": {
                "id": "old_button", "from": common["from"], "chat_instance": "101",
                "data": "fw_variant:61:STD_H", "message": {**common, "text": "Выберите вариант"}}}))
        sent = [m for m in session.methods if m.__api_method__ == "sendMessage"]
        assert len(sent) == 3
        assert all(m.chat_id == 101 and "@DriveModsSWBot" in m.text for m in sent)
        assert all(m.reply_markup.inline_keyboard[0][0].url == "https://t.me/DriveModsSWBot" for m in sent)
        assert [m.__api_method__ for m in session.methods].count("answerCallbackQuery") == 1
        await bot.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("username", ["", "https://t.me/bot", "bad user", "../token"])
def test_redirect_rejects_invalid_destinations(username):
    with pytest.raises(ValueError):
        redirect_router(username)

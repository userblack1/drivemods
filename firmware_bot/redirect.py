"""Keep the retired firmware bot useful without opening a supplier session."""
import argparse
import asyncio
from pathlib import Path
import re

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import dotenv_values

from drivemods_bot.locking import single_instance
from .api import make_bot


def redirect_router(username):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValueError("Invalid replacement bot username")
    url = f"https://t.me/{username}"
    text = ("Выдача прошивок переехала в новый бот: @" + username + ".\n"
            "Нажмите кнопку, затем «Старт» и отправьте свою версию Software Version. "
            "Очередь, выбор STD_H/STD_M и выдача инструкций работают в новом боте.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть DriveMods SW", url=url)
    ]])
    router = Router(name="retired_firmware_redirect")

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def redirect_message(message: Message):
        await message.answer(text, reply_markup=keyboard, parse_mode=None)

    @router.callback_query()
    async def redirect_callback(callback: CallbackQuery):
        await callback.answer("Выдача прошивок переехала: @" + username, show_alert=True)
        if isinstance(callback.message, Message) and callback.message.chat.type == ChatType.PRIVATE:
            await callback.message.answer(text, reply_markup=keyboard, parse_mode=None)

    return router


async def run_redirect(env_path):
    values = dotenv_values(env_path)
    username = (values.get("FIRMWARE_REDIRECT_TO") or "").lstrip("@")
    dispatcher = Dispatcher()
    dispatcher.include_router(redirect_router(username))
    async with make_bot(values.get("FIRMWARE_BOT_TOKEN") or "") as bot:
        me = await bot.get_me()
        if (me.username or "").casefold() == username.casefold():
            raise ValueError("The replacement bot cannot redirect to itself")
        if (await bot.get_webhook_info()).url:
            raise ValueError("The retired bot still has a webhook")
        await bot.set_my_commands([BotCommand(command="start", description="Перейти в новый бот прошивок")])
        await dispatcher.start_polling(bot, close_bot_session=False, handle_as_tasks=True,
                                       tasks_concurrency_limit=32,
                                       allowed_updates=dispatcher.resolve_used_update_types())


def main():
    parser = argparse.ArgumentParser(description="Retired firmware bot redirect")
    parser.add_argument("--env", type=Path, required=True)
    args = parser.parse_args()
    with single_instance(args.env.with_suffix(".lock")):
        asyncio.run(run_redirect(args.env))


if __name__ == "__main__":
    main()

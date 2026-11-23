"""Bind the bot to the account that sends a one-time private setup challenge.

Reads the token only from .env. Never prints tokens or complete Telegram updates.
No DriveMods calls, payments, or device files are involved.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import secrets

from aiogram import Bot
from dotenv import set_key

from drivemods_bot.config import Settings


def matching_owners(updates, challenge: str) -> dict:
    owners = {}
    expected = "/start setup_" + challenge
    for update in updates:
        message = update.message
        if (message and message.chat.type == "private" and message.from_user
                and not message.from_user.is_bot and message.chat.id == message.from_user.id
                and message.text == expected):
            owners[message.from_user.id] = message.from_user
    return owners


async def connect(expected_username: str, action: str) -> int:
    settings = Settings.load()
    if settings.mode != "demo" or settings.store_uploads_enabled or settings.store_purchases_enabled:
        raise RuntimeError("The setup requires demo mode with store operations disabled")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    challenge_file = settings.data_dir / "bot-setup-challenge.txt"
    if action == "prepare":
        challenge_file.write_text(secrets.token_urlsafe(24), encoding="ascii")
        print("Private owner setup challenge prepared.")
        return 0
    if not settings.bot_token or not challenge_file.exists():
        raise RuntimeError("Missing token or owner setup challenge")
    async with Bot(settings.bot_token) as bot:
        me = await bot.get_me(request_timeout=15)
        if not me.username or me.username.lower() != expected_username.lower().lstrip("@"):
            raise RuntimeError("The token does not belong to the expected new bot")
        webhook = await bot.get_webhook_info(request_timeout=15)
        if webhook.url:
            raise RuntimeError("An existing webhook must be checked before polling")
        updates = await bot.get_updates(timeout=0, request_timeout=15)
        owners = matching_owners(updates, challenge_file.read_text(encoding="ascii").strip())
        if len(owners) != 1:
            print(json.dumps({"bot_username": me.username, "owner_setup_pending": True}))
            return 2
        owner = next(iter(owners.values()))
        set_key(".env", "ADMIN_IDS", str(owner.id))
        set_key(".env", "BOT_NAME", "NextMod License Bot")
        if owner.username and not settings.support:
            set_key(".env", "SUPPORT_CONTACT", "@" + owner.username)
        await bot.set_my_description(
            "NextMod License Bot. Тестовый режим: тарифы FULL и ECO. "
            "Загрузка ID в DriveMods и настоящие оплаты отключены.", request_timeout=15)
        await bot.set_my_short_description(
            "NextMod License Bot — лицензии FULL / ECO. Сейчас тестовый режим без оплат.", request_timeout=15)
        summary = {"bot_id": me.id, "bot_username": me.username,
                   "bot_name": me.first_name, "owner_id": owner.id,
                   "owner_username": owner.username, "mode": "demo",
                   "connected_at": datetime.now(timezone.utc).isoformat()}
        (settings.data_dir / "telegram-connection.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"bot_username": me.username, "bot_name": me.first_name,
                          "owner_configured": True, "mode": "demo"}, ensure_ascii=False))
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "finish"])
    parser.add_argument("--username", default="NextModLicenseBot")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(connect(args.username, args.action)))
    except Exception as exc:
        print("Telegram setup failed: " + type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

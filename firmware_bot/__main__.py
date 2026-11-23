import argparse
import asyncio
from getpass import getpass
import json
import logging
from pathlib import Path
import sys

from drivemods_bot.locking import single_instance

from .config import Settings


async def connect_account(settings):
    settings.ensure_account_local()
    from telethon import TelegramClient
    from .transport import connection_type
    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash,
                            device_model="DriveMods SW", app_version="0.1.0",
                            connection=connection_type(settings.telegram_transport))
    try:
        await client.start(phone=lambda: input("Телефон Telegram-аккаунта (+код страны): ").strip(),
                           code_callback=lambda: getpass("Код входа из Telegram: ").strip(),
                           password=lambda: getpass("Пароль двухэтапной аутентификации Telegram: "))
        me = await client.get_me()
        if me.bot:
            raise ValueError("Нужен пользовательский Telegram-аккаунт, а не бот.")
        (settings.data_dir / "account.json").write_text(json.dumps({"user_id": me.id}), encoding="utf-8")
        print(f"Telegram-аккаунт подключён. Его числовой ID: {me.id}")
        print("Если это владелец, укажите этот ID в FIRMWARE_ADMIN_IDS.")
    finally:
        await client.disconnect()


async def online_check(settings):
    from .api import make_bot
    from .bridge import TelegramBridge
    bridge = TelegramBridge(settings)
    try:
        await bridge.connect()
        async with make_bot(settings.bot_token) as bot:
            me = await bot.get_me()
            if me.id == bridge.source.id:
                raise ValueError("Бот-получатель совпадает с источником.")
            if (await bot.get_webhook_info()).url:
                raise ValueError("У нового бота установлен webhook.")
            print(f"Новый бот: @{me.username}; источник: @{bridge.source.username}")
            print("Доступ к Telegram проверен. Запросы версий источнику не отправлялись.")
            if bridge.is_blocked():
                print("Связка заблокирована после незавершённого запроса. Проверьте диалог источника.")
                return False
            return True
    finally:
        await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="DriveMods SW — отдельный бот прошивок")
    parser.add_argument("command", choices=["check", "connect", "run", "reset-bridge"], nargs="?", default="check")
    parser.add_argument("--env", type=Path, help="Отдельный файл настроек (по умолчанию .firmware.env)")
    parser.add_argument("--online", action="store_true", help="Проверить подключение без запросов прошивки")
    parser.add_argument("--source-dialog-checked", action="store_true", help="Владелец проверил, что старый ответ источника завершён")
    args = parser.parse_args()
    if args.online and args.command != "check":
        parser.error("--online доступен только для check")
    try:
        settings = Settings.load(args.env)
        if args.command == "reset-bridge":
            if not args.source_dialog_checked:
                parser.error("Проверьте диалог источника, затем укажите --source-dialog-checked")
            with single_instance(settings.data_dir / "bot.lock"):
                target = settings.data_dir / "bridge-state.json"
                target.write_text('{"state": "ready"}', encoding="utf-8")
            print("Связка разблокирована после проверки диалога владельцем.")
            return
        problems = settings.problems(account_only=args.command == "connect")
        if problems:
            for problem in problems:
                print("Заполните в .firmware.env: " + problem)
            raise SystemExit(1)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with single_instance(settings.data_dir / "bot.lock"):
            if args.command == "connect":
                asyncio.run(connect_account(settings))
            elif args.command == "check":
                if not settings.session_path.is_file():
                    print("Сначала выполните: python -m firmware_bot connect")
                    raise SystemExit(1)
                if args.online:
                    if not asyncio.run(online_check(settings)):
                        raise SystemExit(1)
                else:
                    print("Настройки заполнены; сессия найдена. Проверка Telegram: check --online")
            else:
                from .bridge import TelegramBridge
                from .storage import Database
                from .telegram import run_bot
                logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
                logging.getLogger("firmware_bot.service").setLevel(logging.INFO)
                db = Database(settings.data_dir / "requests.sqlite3")
                try:
                    asyncio.run(run_bot(settings, db, TelegramBridge(settings)))
                finally:
                    db.close()
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

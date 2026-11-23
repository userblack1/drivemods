"""Explicit owner-only live test: customer bot -> supplier -> customer delivery."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from aiogram import Bot, Dispatcher

from drivemods_bot.locking import single_instance
from firmware_bot.bridge import TelegramBridge
from firmware_bot.api import make_bot
from firmware_bot.config import Settings
from firmware_bot.service import Service
from firmware_bot.storage import Database
from firmware_bot.telegram import Messenger, make_router


async def validate(settings):
    bridge = TelegramBridge(settings)
    db = Database(settings.data_dir / "requests.sqlite3")
    bot = make_bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, db, bridge))
    service = Service(settings, db, bridge, Messenger(bot, settings))
    offset = None
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "source_bot": settings.source_bot, "tests": []}

    async def pump():
        nonlocal offset
        updates = await bot.get_updates(offset=offset, timeout=0, limit=100,
                                        allowed_updates=dispatcher.resolve_used_update_types())
        for update in updates:
            await dispatcher.feed_update(bot, update)
            offset = update.update_id + 1
        return updates

    try:
        await bridge.connect()
        user = await bridge.client.get_me()
        own_bot = await bot.get_me()
        if user.id not in settings.admin_ids:
            raise RuntimeError("Для живого теста нужен аккаунт владельца нового бота.")
        report.update(account_id=user.id, account_name=user.first_name, bot_username=own_bot.username)
        if bridge.is_blocked():
            raise RuntimeError("Сначала проверьте завершение предыдущего ответа и сбросьте блокировку связки.")
        db.recover()
        await bridge.client.send_message(own_bot.username, "/start", parse_mode=None)
        await pump()
        while db.next_request():
            print("Processing existing queued request", db.next_request()["id"], flush=True)
            await service.step()
            if bridge.is_blocked():
                raise RuntimeError("Связка остановлена при обработке предыдущего запроса.")

        for query, expected in [("LX2.KOR.S5W", "delivered"), ("NEXTMODETEST.KOR.S5W", "not_found")]:
            latest = db.latest(user.id)
            if latest:
                await asyncio.sleep(max(0, settings.user_cooldown - (time.time() - latest["created"]) + 0.5))
            customer_sent = await bridge.client.send_message(own_bot.username, query, parse_mode=None)
            print("Sent customer-flow test:", query, flush=True)
            request = None
            deadline = time.monotonic() + 30
            while request is None and time.monotonic() < deadline:
                for update in await pump():
                    message = update.message
                    if message and message.from_user.id == user.id and message.text == query:
                        row = db.connection.execute("SELECT * FROM requests WHERE chat_id=? AND message_id=?",
                                                    (message.chat.id, message.message_id)).fetchone()
                        request = dict(row) if row else None
                if request is None:
                    await asyncio.sleep(0.5)
            if request is None:
                raise RuntimeError("Реальный обработчик Telegram не создал тестовый запрос.")
            print("Accepted request", request["id"], flush=True)
            while db.next_request() and db.latest(user.id)["state"] in {"queued", "searching", "delivering"}:
                await service.step()
            final = db.latest(user.id)
            recent = await bridge.client.get_messages(own_bot.username, limit=30)
            inbound = [m for m in recent if not m.out and m.id > customer_sent.id]
            result = {
                "query": query, "request_id": request["id"], "state": final["state"],
                "has_completion": any(f"Комплект по запросу #{request['id']} передан полностью" in (m.message or "") for m in inbound),
                "has_not_found": any("Данная прошивка или версия не найдена" in (m.message or "") for m in inbound),
                "hidden_link_entities": sum(type(e).__name__ == "MessageEntityTextUrl" for m in inbound for e in (m.entities or [])),
                "photos_received": sum(bool(m.photo) for m in inbound),
            }
            report["tests"].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if final["state"] != expected or (expected == "delivered" and not result["has_completion"]):
                raise RuntimeError("Неожиданный результат живого теста: " + final["state"])
        report["success"] = True
        connection_path = settings.data_dir / "connection.json"
        connection = json.loads(connection_path.read_text(encoding="utf-8")) if connection_path.exists() else {}
        connection.update(account_authorized=True, account_id=user.id, account_name=user.first_name,
                          source_lookup_status="verified", firmware_delivery_verified=True)
        connection_path.write_text(json.dumps(connection, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        report.update(success=False, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        (settings.data_dir / "live-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            if offset is not None:
                await bot.get_updates(offset=offset, timeout=0, limit=1)
        finally:
            await bot.session.close()
            await bridge.close()
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Живой тест отправляет две версии источнику и выдаёт ответ владельцу")
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--env", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.env)
    with single_instance(settings.data_dir / "bot.lock"):
        asyncio.run(validate(settings))

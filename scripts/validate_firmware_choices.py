"""Owner-only live check of both customer STD_H/STD_M buttons and deliveries."""
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from aiogram import Dispatcher
from telethon import functions

from drivemods_bot.locking import single_instance
from firmware_bot.api import make_bot
from firmware_bot.bridge import TelegramBridge
from firmware_bot.config import Settings
from firmware_bot.response import SourceMessage, button_payload, validate_response
from firmware_bot.service import Service
from firmware_bot.storage import Database
from firmware_bot.telegram import Messenger, make_router
from firmware_bot.versions import SoftwareVersion


async def validate(settings):
    bridge = TelegramBridge(settings)
    db = Database(settings.data_dir / "requests.sqlite3")
    bot = make_bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, db, bridge))
    service = Service(settings, db, bridge, Messenger(bot, settings))
    offset = None
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "tests": []}
    report_path = settings.data_dir / "variant-choice-validation.json"

    async def pump():
        nonlocal offset
        updates = await bot.get_updates(offset=offset, timeout=0, limit=100,
                                        allowed_updates=dispatcher.resolve_used_update_types())
        for update in updates:
            await dispatcher.feed_update(bot, update)
            offset = update.update_id + 1

    def request(request_id):
        return dict(db.connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    try:
        await bridge.connect()
        owner = await bridge.client.get_me()
        own_bot = await bot.get_me()
        if owner.id not in settings.admin_ids:
            raise RuntimeError("Live choices must use the bot owner's account")
        if bridge.is_blocked():
            await bridge.try_recover()
        if bridge.is_blocked():
            raise RuntimeError("Supplier conversation needs review before the live test")
        report.update(account_id=owner.id, bot_username=own_bot.username)
        peer = await bridge.client.get_entity(own_bot.username)
        db.recover()
        await pump()
        while db.next_request():
            await service.step()
            if bridge.is_blocked():
                raise RuntimeError("An existing queued request blocked the supplier")

        for variant in ("STD_H", "STD_M"):
            latest = db.latest(owner.id)
            if latest:
                await asyncio.sleep(max(0, settings.user_cooldown - (time.time() - latest["created"]) + 0.5))
            customer_sent = await bridge.client.send_message(peer, "IG19.KOR.SOP", parse_mode=None)
            deadline = time.monotonic() + 60
            row = None
            while time.monotonic() < deadline:
                await pump()
                row = db.latest(owner.id)
                if row and row["query"] == "IG19.KOR.SOP" and row["created"] >= customer_sent.date.timestamp():
                    break
                await asyncio.sleep(0.5)
            if not row or row["state"] != "awaiting_variant":
                raise RuntimeError("Short query did not create a pending variant choice")
            request_id = row["id"]
            recent = await bridge.client.get_messages(peer, limit=15)
            prompt = None
            buttons = {}
            prefix = f"fw_variant:{request_id}:".encode()
            for message in recent:
                found = {button.text: button_payload(button).data
                         for line in getattr(message.reply_markup, "rows", []) or [] for button in line.buttons
                         if getattr(button_payload(button), "data", b"").startswith(prefix)}
                if found:
                    prompt, buttons = message, found
                    break
            if not prompt or set(buttons) != {"STD_H", "STD_M"}:
                raise RuntimeError("Customer did not receive both variant buttons")
            click = asyncio.create_task(bridge.client(functions.messages.GetBotCallbackAnswerRequest(
                peer=peer, msg_id=prompt.id, data=buttons[variant])))
            try:
                deadline = time.monotonic() + 60
                while request(request_id)["state"] == "awaiting_variant" and time.monotonic() < deadline:
                    await pump()
                    await asyncio.sleep(0.25)
                row = request(request_id)
                if row["state"] != "queued" or row["variant"] != variant:
                    raise RuntimeError("Real customer click did not queue the selected variant")
            finally:
                # Never click twice if Telegram's callback acknowledgement timed out.
                with suppress(Exception):
                    await asyncio.wait_for(click, 10)
            while request(request_id)["state"] in {"queued", "searching", "delivering"}:
                if not await service.step():
                    raise RuntimeError("Selected request disappeared from the queue")
            row = request(request_id)
            history = await bridge.client.get_messages(peer, limit=30)
            incoming = [message for message in history if not message.out and message.id > customer_sent.id]
            messages = [SourceMessage.from_telegram(message) for message in incoming]
            expected = SoftwareVersion.parse("IG19.KOR.SOP").with_variant(variant)
            validate_response(expected, messages, settings)
            completion = any(f"Комплект по запросу #{request_id} передан полностью" in m.text for m in messages)
            photos = sum(m.media_kind == "photo" for m in messages)
            links = sum(len(m.urls()) for m in messages)
            if row["state"] != "delivered" or not completion or links < 2 or bridge.is_blocked():
                raise RuntimeError("Selected firmware was not delivered completely")
            result = dict(request_id=request_id, query=row["query"], variant=variant, state=row["state"],
                          buttons=sorted(buttons), has_completion=completion, photos_received=photos,
                          links=links, bridge_blocked=bridge.is_blocked())
            report["tests"].append(result)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        report["success"] = True
    except Exception as exc:
        report.update(success=False, error_type=type(exc).__name__)
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            if offset is not None:
                await bot.get_updates(offset=offset, timeout=0, limit=1)
        finally:
            await bot.session.close()
            await bridge.close()
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live owner test: short query, both variant buttons, firmware delivery")
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--env", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.env)
    with single_instance(settings.data_dir / "bot.lock"):
        asyncio.run(validate(settings))

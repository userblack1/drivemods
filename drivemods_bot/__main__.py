import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from .config import Settings
from .locking import single_instance
from .models import State
from .service import Service
from .storage import Database
from .supplier import BridgeSupplier, DemoSupplier, OperatorSupplier


class ConsoleMessenger:
    async def delivered(self, order: dict, path: Path, demo: bool) -> str:
        print("Тестовый файл: " + str(path.resolve()))
        return "demo-local-delivery"

    async def notify_admins(self, text: str) -> None:
        print(text)


async def demo(settings: Settings) -> None:
    if settings.mode != "demo":
        raise ValueError("Для демонстрации укажите APP_MODE=demo. Реальные оплаты не имитируются.")
    db = Database(settings.data_dir / "demo.sqlite3")
    service = Service(settings, db, DemoSupplier(settings))
    messenger = ConsoleMessenger()
    order_id, source = service.allocate()
    # Fresh synthetic input, never the real device file in Downloads.
    source.write_bytes(("SYNTHETIC DEMO DEVICE ID " + order_id).encode())
    order, _ = await service.upload(order_id, 1, 1, "demo-device.id.bin", source)
    print("1. Синтетический ID принят локально. В DriveMods ничего не отправлено.")
    print(f"2. FULL: {(settings.full_cost + settings.markup) / 100:g} USD; ECO: {(settings.eco_cost + settings.markup) / 100:g} USD.")
    order = db.choose(order_id, 1, "full")
    db.paid(order_id, 1, "XTR", order["stars"], "demo-" + order_id)
    print("3. Оплата имитирована. Telegram и Visa не используются.")
    for _ in range(4):
        await service.step(messenger)
    final = db.get(order_id)
    if final["state"] != State.DELIVERED:
        raise RuntimeError("Demo failed to reach delivery")
    print("4. Получен DEMO-NOT-A-LICENSE.txt — не настоящая лицензия.")
    print(json.dumps({"order_id": order_id, "status": final["state"]}, ensure_ascii=False))


async def identify(settings: Settings) -> None:
    from aiogram import Bot
    if not settings.bot_token:
        raise ValueError("Заполните BOT_TOKEN в .env")
    async with Bot(settings.bot_token) as bot:
        updates = await bot.get_updates(timeout=0)
        found = set()
        for update in updates:
            if update.message and update.message.from_user:
                found.add(update.message.from_user.id)
        for user_id in sorted(found):
            print("Telegram ID: " + str(user_id))
        if not found:
            print("Сначала отправьте /start своему боту в Telegram, затем повторите identify.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот лицензий DriveMods")
    parser.add_argument("command", choices=["check", "doctor", "demo", "run", "identify", "prepare-order", "complete-order"], nargs="?", default="check")
    parser.add_argument("--online", action="store_true", help="Только GET-проверки доступности, без загрузок ID и оплаты")
    parser.add_argument("--file", type=Path, help="Локальный файл; команда prepare-order не загружает его в магазин")
    parser.add_argument("--plan", choices=["full", "eco"])
    parser.add_argument("--order-id")
    parser.add_argument("--store-reference")
    parser.add_argument("--payment-reference")
    args = parser.parse_args()
    settings = Settings.load()
    if args.online and args.command != "doctor":
        parser.error("--online доступен только для doctor")
    if args.command in {"prepare-order", "complete-order"}:
        from .field_order import complete_packet, prepare_packet
        if not args.file:
            parser.error("Укажите --file")
        with single_instance(settings.data_dir / "field-orders.lock"):
            if args.command == "prepare-order":
                if not args.plan:
                    parser.error("Укажите --plan full или eco")
                output = prepare_packet(settings, args.file, args.plan)
                print("Пакет подготовлен только локально: " + str(output.resolve()))
                print("ID не отправлен в магазин. Оплата не выполнялась.")
            else:
                if not all((args.order_id, args.store_reference, args.payment_reference)):
                    parser.error("Нужны --order-id, --store-reference и --payment-reference")
                output = complete_packet(settings, args.order_id, args.file, args.store_reference, args.payment_reference)
                print("Файл сохранён локально: " + str(output.resolve()))
        return
    if args.command == "doctor":
        from .readiness import doctor
        report = asyncio.run(doctor(settings, args.online))
        if not report["preparation_ok"]:
            sys.exit(1)
        return
    if args.command == "check":
        print(f"Режим: {settings.mode}")
        print(f"FULL {(settings.full_cost + settings.markup) / 100:g} USD | ECO {(settings.eco_cost + settings.markup) / 100:g} USD")
        problems = settings.problems()
        for problem in problems:
            print("Нужно настроить: " + problem)
        if not problems:
            print("Локальная конфигурация заполнена. Для live обязательна отдельная проверка интеграции магазина и Visa.")
        return
    if args.command == "identify":
        asyncio.run(identify(settings))
        return
    with single_instance(settings.data_dir / "bot.lock"):
        if args.command == "demo":
            asyncio.run(demo(settings))
            return
        problems = settings.problems()
        if problems:
            sys.exit("Запуск остановлен:\n" + "\n".join(problems))
        from .telegram import run_bot
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        supplier = DemoSupplier(settings) if settings.mode == "demo" else OperatorSupplier() if settings.mode == "operator" else BridgeSupplier(settings)
        service = Service(settings, Database(settings.data_dir / "orders.sqlite3"), supplier)
        asyncio.run(run_bot(service))


if __name__ == "__main__":
    main()


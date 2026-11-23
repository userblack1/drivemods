"""Read-only preflight: never accepts device files and never posts to DriveMods."""
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

import httpx

from .config import Settings


def inspect_local(settings: Settings) -> dict:
    checks = []
    checks.append({"name": "Python 3.12+", "ok": sys.version_info >= (3, 12)})
    for package in ("aiogram", "httpx", "python-dotenv"):
        try:
            installed = version(package)
            checks.append({"name": package, "ok": True, "version": installed})
        except PackageNotFoundError:
            checks.append({"name": package, "ok": False})
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    probe = settings.data_dir / (".write-probe-" + uuid4().hex)
    try:
        probe.write_bytes(b"local readiness check")
        checks.append({"name": "Запись файлов заказа", "ok": probe.read_bytes() == b"local readiness check"})
    except OSError:
        checks.append({"name": "Запись файлов заказа", "ok": False})
    finally:
        probe.unlink(missing_ok=True)
    free = shutil.disk_usage(settings.data_dir).free
    checks.append({"name": "Свободно не менее 100 МБ", "ok": free >= 100 * 1024 * 1024})
    database = settings.data_dir / "orders.sqlite3"
    if database.exists():
        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as con:
                checks.append({"name": "Целостность базы заказов", "ok": con.execute("PRAGMA quick_check").fetchone()[0] == "ok"})
        except sqlite3.Error:
            checks.append({"name": "Целостность базы заказов", "ok": False})
    return {
        "preparation_ok": all(item["ok"] for item in checks),
        "checks": checks,
        "mode": settings.mode,
        "store_uploads_enabled": settings.store_uploads_enabled,
        "store_purchases_enabled": settings.store_purchases_enabled,
        "telegram_token_present": bool(settings.bot_token),
        "owner_configured": bool(settings.admin_ids),
        "live_blockers": replace(settings, mode="live").problems(),
        "purchase_verified": False,
        "note": "Проверка не загружает ID, не создаёт лицензии и не проверяет реальную оплату Visa.",
    }


async def inspect_online(settings: Settings) -> list[dict]:
    checks = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get("https://store.drivemods.org/")
            checks.append({"name": "Публичная страница DriveMods по HTTPS", "ok": response.status_code == 200})
    except httpx.HTTPError:
        checks.append({"name": "Публичная страница DriveMods по HTTPS", "ok": False})
    if settings.bot_token:
        from aiogram import Bot
        try:
            async with Bot(settings.bot_token) as bot:
                await bot.get_me()
                info = await bot.get_webhook_info()
                checks.append({"name": "Токен Telegram действует", "ok": True})
                checks.append({"name": "Нет webhook, мешающего polling", "ok": not bool(info.url)})
        except Exception:
            # Never include exception URLs: they can contain the bot token.
            checks.append({"name": "Доступ к Telegram по токену", "ok": False})
    return checks


async def doctor(settings: Settings, online: bool = False) -> dict:
    report = inspect_local(settings)
    if online:
        report["network_checks"] = await inspect_online(settings)
    path = settings.data_dir / "readiness.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for check in report["checks"] + report.get("network_checks", []):
        print(("OK   " if check["ok"] else "FAIL ") + check["name"])
    print("Загрузка ID в магазин: " + ("РАЗРЕШЕНА" if settings.store_uploads_enabled else "ЗАКРЫТА"))
    print("Покупки в магазине: " + ("РАЗРЕШЕНЫ" if settings.store_purchases_enabled else "ЗАКРЫТЫ"))
    for blocker in report["live_blockers"]:
        print("До реального запуска: " + blocker)
    print("Реальная покупка/Visa: отложена до подтверждённого заказа с новым ID.")
    print("Отчёт без секретов: " + str(path.resolve()))
    return report


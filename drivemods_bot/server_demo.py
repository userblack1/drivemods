"""Dedicated server entry point for the explicitly requested unpaid demo.

The entry point forces the restrictions even if the server environment is
accidentally changed. It never instantiates the live DriveMods integration.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import logging
import os

from .config import Settings
from .locking import single_instance
from .service import Service
from .storage import Database
from .supplier import DemoSupplier


def server_settings() -> Settings:
    os.environ["APP_MODE"] = "demo"
    os.environ["STORE_UPLOADS_ENABLED"] = "false"
    os.environ["STORE_PURCHASES_ENABLED"] = "false"
    os.environ.setdefault("BOT_INSTANCE_LABEL", "Сервер VDSka")
    return replace(Settings.load(), bridge_url="", bridge_token="", bridge_verified=False)


def check(settings: Settings) -> None:
    problems = settings.problems()
    if problems:
        raise RuntimeError("; ".join(problems))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    probe = settings.data_dir / ".server-write-check"
    probe.write_text("demo only", encoding="ascii")
    probe.unlink()


def main():
    parser = argparse.ArgumentParser(description="NextMod server demo: no payments or store uploads")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = server_settings()
    check(settings)
    if args.check:
        print(json.dumps({"configuration_ok": True, "mode": "demo", "owner_configured": bool(settings.admin_ids),
                          "store_uploads": False, "purchases": False, "instance": settings.instance_label}, ensure_ascii=False))
        return
    from .telegram import run_bot
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    with single_instance(settings.data_dir / "bot.lock"):
        service = Service(settings, Database(settings.data_dir / "orders.sqlite3"), DemoSupplier(settings))
        asyncio.run(run_bot(service))


if __name__ == "__main__":
    main()

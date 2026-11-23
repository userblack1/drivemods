"""Server entry point for explicitly selected operator fulfillment."""
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
from .supplier import OperatorSupplier


def server_settings() -> Settings:
    os.environ["APP_MODE"] = "operator"
    os.environ["STORE_UPLOADS_ENABLED"] = "false"
    os.environ["STORE_PURCHASES_ENABLED"] = "false"
    os.environ.setdefault("BOT_INSTANCE_LABEL", "Сервер VDSka")
    return replace(Settings.load(), bridge_url="", bridge_token="", bridge_verified=False)


def main():
    parser = argparse.ArgumentParser(description="DriveMods LICENCE with operator; no automatic charges")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = server_settings()
    if problems := settings.problems():
        raise RuntimeError("; ".join(problems))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if args.check:
        print(json.dumps(dict(configuration_ok=True, mode=settings.mode, auto_store_uploads=False,
                              auto_charges=False, owner_configured=bool(settings.admin_ids),
                              net_usd_credit_required=settings.require_usd_net_credit,
                              receiving_bank=settings.receiving_bank,
                              mbank_qr_enabled=settings.mbank_qr_enabled,
                              mbank_qr_rate_configured=bool(settings.mbank_qr_kgs_per_usd),
                              mbank_qr_fixed_prices_configured=bool(settings.mbank_qr_full_kgs_minor and settings.mbank_qr_eco_kgs_minor)), ensure_ascii=False))
        return
    from .telegram import run_bot
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    with single_instance(settings.data_dir / "bot.lock"):
        asyncio.run(run_bot(Service(settings, Database(settings.data_dir / "orders.sqlite3"), OperatorSupplier())))


if __name__ == "__main__":
    main()

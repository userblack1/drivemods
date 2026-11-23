from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

from .models import OrderError
from .purchase_policy import check_purchase_budget


def cents(value: str) -> int:
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0 or parsed.as_tuple().exponent < -2:
            raise ValueError("Money must be non-negative with at most two decimal places")
        return int(parsed * 100)
    except InvalidOperation as exc:
        raise ValueError("Invalid money amount") from exc


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    bot_name: str = "NextMod License Bot"
    instance_label: str = "Локальный компьютер"
    bot_token: str = field(default="", repr=False)
    admin_ids: frozenset[int] = frozenset()
    support: str = ""
    data_dir: Path = Path("data")
    markup: int = 1000
    full_cost: int = 9000
    eco_cost: int = 6000
    full_stars: int = 0
    eco_stars: int = 0
    bridge_url: str = ""
    bridge_token: str = field(default="", repr=False)
    bridge_verified: bool = False
    store_uploads_enabled: bool = False
    store_purchases_enabled: bool = False
    max_upload: int = 1_048_576
    max_license: int = 2_097_152
    quote_ttl: int = 1800
    require_usd_net_credit: bool = False
    receiving_bank: str = ""
    mbank_qr_enabled: bool = False
    mbank_qr_source: Path | None = field(default=None, repr=False)
    mbank_qr_kgs_per_usd: str | None = None
    mbank_qr_full_kgs_minor: int | None = None
    mbank_qr_eco_kgs_minor: int | None = None

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(override=False)
        result = cls(
            mode=os.getenv("APP_MODE", "demo"),
            bot_name=os.getenv("BOT_NAME", "NextMod License Bot"),
            instance_label=os.getenv("BOT_INSTANCE_LABEL", "Локальный компьютер"),
            bot_token=os.getenv("BOT_TOKEN", ""),
            admin_ids=frozenset(int(v.strip()) for v in os.getenv("ADMIN_IDS", "").split(",") if v.strip()),
            support=os.getenv("SUPPORT_CONTACT", ""),
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            markup=cents(os.getenv("MARKUP_USD", "10.00")),
            full_cost=cents(os.getenv("FULL_COST_USD", "90.00")),
            eco_cost=cents(os.getenv("ECO_COST_USD", "60.00")),
            full_stars=int(os.getenv("FULL_PRICE_STARS") or 0),
            eco_stars=int(os.getenv("ECO_PRICE_STARS") or 0),
            bridge_url=os.getenv("DRIVEMODS_BRIDGE_URL", "").rstrip("/"),
            bridge_token=os.getenv("DRIVEMODS_BRIDGE_TOKEN", ""),
            bridge_verified=os.getenv("DRIVEMODS_BRIDGE_VERIFIED", "false").lower() == "true",
            store_uploads_enabled=os.getenv("STORE_UPLOADS_ENABLED", "false").lower() == "true",
            store_purchases_enabled=os.getenv("STORE_PURCHASES_ENABLED", "false").lower() == "true",
            max_upload=int(os.getenv("MAX_UPLOAD_BYTES", "1048576")),
            max_license=int(os.getenv("MAX_LICENSE_BYTES", "2097152")),
            quote_ttl=int(os.getenv("QUOTE_TTL_SECONDS", "1800")),
            require_usd_net_credit=os.getenv("REQUIRE_USD_NET_CREDIT", "false").lower() == "true",
            receiving_bank=os.getenv("RECEIVING_BANK", ""),
            mbank_qr_enabled=os.getenv("MBANK_QR_ENABLED", "false").lower() == "true",
            mbank_qr_source=Path(os.environ["MBANK_QR_SOURCE_PATH"]).resolve() if os.getenv("MBANK_QR_SOURCE_PATH") else None,
            mbank_qr_kgs_per_usd=os.getenv("MBANK_QR_KGS_PER_USD") or None,
            mbank_qr_full_kgs_minor=cents(os.environ["MBANK_QR_FULL_KGS"]) if os.getenv("MBANK_QR_FULL_KGS") else None,
            mbank_qr_eco_kgs_minor=cents(os.environ["MBANK_QR_ECO_KGS"]) if os.getenv("MBANK_QR_ECO_KGS") else None,
        )
        if result.mode not in {"demo", "live", "operator"}:
            raise ValueError("APP_MODE must be demo, operator or live")
        if min(result.max_upload, result.max_license, result.quote_ttl) <= 0:
            raise ValueError("Limits must be positive")
        return result

    def cost(self, plan: str) -> int:
        return {"full": self.full_cost, "eco": self.eco_cost}[plan]

    def stars(self, plan: str) -> int:
        return {"full": self.full_stars, "eco": self.eco_stars}[plan]

    def problems(self) -> list[str]:
        problems = []
        if self.mbank_qr_enabled:
            from importlib.util import find_spec
            if find_spec("qrcode") is None or find_spec("png") is None:
                problems.append("QR MBANK: установите qrcode и pypng из requirements.lock")
            if self.mode != "operator":
                problems.append("QR MBANK доступен только с проверкой зачисления оператором")
            if self.receiving_bank.upper() not in {"MBANK", "МБАНК"}:
                problems.append("Для QR MBANK укажите RECEIVING_BANK=MBANK")
            try:
                from .bank_qr import MBankQr
                from .payment_setup import amount_kgs_minor
                if self.mbank_qr_source is None:
                    raise ValueError("нужен исходный QR получателя")
                MBankQr.read(self.mbank_qr_source)
                fixed = (self.mbank_qr_full_kgs_minor, self.mbank_qr_eco_kgs_minor)
                if any(value is not None for value in fixed):
                    if self.mbank_qr_kgs_per_usd is not None or any(type(value) is not int or not 0 < value <= 100_000_000 for value in fixed):
                        raise ValueError("задайте две положительные фиксированные суммы KGS без одновременного курса")
                else:
                    if self.mbank_qr_kgs_per_usd is None:
                        raise ValueError("нужны суммы FULL/ECO в сомах или согласованный курс KGS за USD")
                    for plan in ("full", "eco"):
                        amount_kgs_minor(self.cost(plan) + self.markup, self.mbank_qr_kgs_per_usd)
            except (OSError, ValueError) as exc:
                problems.append("QR MBANK: " + str(exc))
        if not self.bot_token:
            problems.append("BOT_TOKEN: нужен токен BotFather")
        if not self.admin_ids:
            problems.append("ADMIN_IDS: нужен Telegram ID владельца")
        if self.mode in {"live", "operator"}:
            for plan in ("full", "eco"):
                try:
                    check_purchase_budget(plan, self.cost(plan))
                except OrderError as exc:
                    problems.append(str(exc))
            if not self.support:
                problems.append("SUPPORT_CONTACT: нужен контакт поддержки")
        if self.mode == "operator" and (self.store_uploads_enabled or self.store_purchases_enabled):
            problems.append("Режим оператора требует отключённых автоматических отправок ID и закупок")
        if self.mode == "live":
            if not self.bridge_url or not self.bridge_token or not self.bridge_verified:
                problems.append("DriveMods: нужен подключённый и проверенный адаптер магазина и оплаты Visa")
            if not self.store_uploads_enabled or not self.store_purchases_enabled:
                problems.append("DriveMods закрыт для изменений: STORE_UPLOADS_ENABLED / STORE_PURCHASES_ENABLED")
            if min(self.full_stars, self.eco_stars) <= 0:
                problems.append("FULL_PRICE_STARS / ECO_PRICE_STARS: укажите розничную цену в Stars")
            parsed = urlparse(self.bridge_url)
            if self.bridge_url and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password):
                problems.append("DRIVEMODS_BRIDGE_URL: требуется HTTPS без логина/пароля в URL")
        return problems


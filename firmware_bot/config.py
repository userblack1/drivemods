from dataclasses import dataclass
import json
from pathlib import Path
import re

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    api_id: int = 0
    api_hash: str = ""
    telegram_transport: str = "tcp"
    source_bot: str = ""
    admin_ids: frozenset[int] = frozenset()
    owner_only: bool = True
    support_contact: str = ""
    instance_label: str = "Локальный компьютер"
    data_dir: Path = ROOT / "data/firmware"
    response_timeout: float = 90
    settle_seconds: float = 8
    min_collect_seconds: float = 12
    max_queue: int = 100
    user_cooldown: float = 3
    cache_ttl: float = 900
    delivery_concurrency: int = 4
    queue_timeout: float = 1800
    install_markers: tuple[str, ...] = ("инструкция по установке", "инструкция по инсталляции", "installation instruction")
    download_markers: tuple[str, ...] = ("прошивка", "файлы обновления", "firmware")

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telegram-user.session"

    def ensure_account_local(self) -> None:
        marker = self.data_dir / "server-deployment.json"
        if not marker.exists():
            return
        try:
            deployment = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise RuntimeError("Не удалось проверить размещение сессии. Проверьте server-deployment.json.") from None
        if deployment.get("active"):
            raise RuntimeError("Сессия NextMode работает на сервере VDSka. Для переноса обратно сначала остановите серверную службу и снимите флаг active в server-deployment.json.")

    @classmethod
    def load(cls, env_path: Path | None = None):
        env_path = (env_path or ROOT / ".firmware.env").resolve()
        # Deliberately do not load the license bot's .env or its BOT_TOKEN.
        values = dotenv_values(env_path)

        def val(name, default=""):
            return (values.get(name) or str(default)).strip()

        def boolean(name, default):
            raw = val(name, default).lower()
            if raw not in {"true", "false"}:
                raise ValueError(f"{name}: укажите true или false")
            return raw == "true"

        data = Path(val("FIRMWARE_DATA_DIR", "data/firmware"))
        if not data.is_absolute():
            data = env_path.parent / data
        defaults = cls()
        return cls(
            bot_token=val("FIRMWARE_BOT_TOKEN"), api_id=int(val("TELEGRAM_API_ID", 0)),
            api_hash=val("TELEGRAM_API_HASH"), source_bot=val("FIRMWARE_SOURCE_BOT").lstrip("@"),
            telegram_transport=val("TELEGRAM_TRANSPORT", "tcp").casefold(),
            admin_ids=frozenset(int(item.strip()) for item in val("FIRMWARE_ADMIN_IDS").split(",") if item.strip()),
            owner_only=boolean("FIRMWARE_OWNER_ONLY", "true"), support_contact=val("FIRMWARE_SUPPORT_CONTACT"),
            instance_label=val("FIRMWARE_INSTANCE_LABEL", "Локальный компьютер"),
            data_dir=data.resolve(), response_timeout=float(val("FIRMWARE_RESPONSE_TIMEOUT", 90)),
            settle_seconds=float(val("FIRMWARE_SETTLE_SECONDS", 8)),
            min_collect_seconds=float(val("FIRMWARE_MIN_COLLECT_SECONDS", 12)),
            max_queue=int(val("FIRMWARE_MAX_QUEUE", 100)), user_cooldown=float(val("FIRMWARE_USER_COOLDOWN", 3)),
            cache_ttl=float(val("FIRMWARE_CACHE_TTL", 900)),
            delivery_concurrency=int(val("FIRMWARE_DELIVERY_CONCURRENCY", 4)),
            queue_timeout=float(val("FIRMWARE_QUEUE_TIMEOUT", 1800)),
            install_markers=tuple(x.strip().casefold() for x in val("FIRMWARE_INSTALL_MARKERS", ";".join(defaults.install_markers)).split(";") if x.strip()),
            download_markers=tuple(x.strip().casefold() for x in val("FIRMWARE_DOWNLOAD_MARKERS", ";".join(defaults.download_markers)).split(";") if x.strip()),
        )

    def problems(self, *, account_only=False) -> list[str]:
        errors = []
        if self.api_id <= 0:
            errors.append("TELEGRAM_API_ID")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", self.api_hash):
            errors.append("TELEGRAM_API_HASH (32 шестнадцатеричных символа)")
        if self.telegram_transport not in {"tcp", "wss"}:
            errors.append("TELEGRAM_TRANSPORT (tcp или wss)")
        if account_only:
            return errors
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", self.bot_token):
            errors.append("FIRMWARE_BOT_TOKEN (токен нового бота)")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", self.source_bot):
            errors.append("FIRMWARE_SOURCE_BOT (@username источника)")
        if not self.admin_ids or any(item <= 0 for item in self.admin_ids):
            errors.append("FIRMWARE_ADMIN_IDS (числовой ID владельца)")
        if not (0 < self.settle_seconds <= self.min_collect_seconds < self.response_timeout <= 600):
            errors.append("интервалы: 0 < SETTLE_SECONDS <= MIN_COLLECT_SECONDS < RESPONSE_TIMEOUT <= 600")
        if not 1 <= self.max_queue <= 100 or self.user_cooldown < 0:
            errors.append("MAX_QUEUE (1..100) / USER_COOLDOWN (>=0)")
        if not 0 <= self.cache_ttl <= 3600 or not 1 <= self.delivery_concurrency <= 10 or not 60 <= self.queue_timeout <= 7200:
            errors.append("CACHE_TTL (0..3600) / DELIVERY_CONCURRENCY (1..10) / QUEUE_TIMEOUT (60..7200)")
        if not self.install_markers or not self.download_markers:
            errors.append("INSTALL_MARKERS / DOWNLOAD_MARKERS")
        license_token = dotenv_values(ROOT / ".env").get("BOT_TOKEN")
        if self.bot_token and self.bot_token == license_token:
            errors.append("нельзя использовать токен действующего бота лицензий")
        return errors

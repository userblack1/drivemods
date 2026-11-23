"""Local first-order packet. No network access and no supplier side effects."""
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .config import Settings
from .models import OrderError
from .supplier import validate_license


def prepare_packet(settings: Settings, source: Path, plan: str) -> Path:
    if plan not in {"full", "eco"} or source.suffix.lower() not in {".bin", ".id"}:
        raise OrderError("Нужен исходный .bin/.id и пакет full или eco.")
    if not source.is_file() or not 0 < source.stat().st_size <= settings.max_upload:
        raise OrderError("Исходный файл пустой, отсутствует или слишком большой.")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    root = settings.data_dir / "field-orders"
    root.mkdir(parents=True, exist_ok=True)
    # An accidental repeat returns the same packet instead of suggesting a new order.
    for existing in root.glob("*/order.json"):
        previous = json.loads(existing.read_text(encoding="utf-8"))
        if previous.get("device_sha256") == digest:
            if previous["plan"] != plan:
                raise OrderError("Этот ID уже подготовлен с другим пакетом. Сначала сверьте существующий заказ.")
            return existing.parent
    order_id = uuid4().hex
    folder = root / order_id
    folder.mkdir()
    (folder / "device.id.bin").write_bytes(content)
    manifest = {
        "order_id": order_id, "plan": plan,
        "supplier_cost_usd_cents": settings.cost(plan),
        "retail_usd_cents": settings.cost(plan) + settings.markup,
        "original_filename": source.name, "device_sha256": digest,
        "dealer_comment": f"TG:{order_id} {plan.upper()}",
        "status": "prepared_local_only", "store_reference": None,
        "payment_reference": None, "license_sha256": None,
        "note": "Пакет создан локально. ID в DriveMods не отправлялся. Оплата не выполнялась.",
    }
    (folder / "order.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def complete_packet(settings: Settings, order_id: str, license_file: Path, store_reference: str, payment_reference: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", order_id):
        raise OrderError("Некорректный ID локального заказа.")
    folder = settings.data_dir / "field-orders" / order_id
    manifest_path = folder / "order.json"
    if not manifest_path.is_file():
        raise OrderError("Локальный пакет заказа не найден.")
    if not store_reference.strip() or not payment_reference.strip():
        raise OrderError("Нужны номер лицензии/заказа магазина и номер оплаты.")
    if not license_file.is_file() or license_file.stat().st_size > settings.max_license:
        raise OrderError("Файл лицензии отсутствует или слишком большой.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = license_file.read_bytes()
    validate_license(content, manifest["device_sha256"], settings)
    digest = hashlib.sha256(content).hexdigest()
    if manifest.get("license_sha256") and manifest["license_sha256"] != digest:
        raise OrderError("В этом заказе уже сохранена другая лицензия. Нужна сверка.")
    output = folder / "device.license.bin"
    part = folder / "device.license.bin.part"
    part.write_bytes(content)
    part.replace(output)
    manifest.update(status="downloaded_pending_vehicle_check", store_reference=store_reference,
                    payment_reference=payment_reference, license_sha256=digest,
                    note="Готовый файл сохранён оператором. Подлинность и привязку подтверждает DriveMods; проверка в автомобиле ещё не отмечена.")
    # If writing the receipt is interrupted, the old JSON remains valid.
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    return output


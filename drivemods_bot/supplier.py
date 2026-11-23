"""Supplier boundary. The bridge contract is ours; it is not DriveMods API documentation."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse
import hashlib
import math
import time

import httpx

from .config import Settings
from .models import Offer, OrderError, Quote, SupplierOrder
from .purchase_policy import check_purchase_budget


class Supplier(Protocol):
    async def quote(self, source: Path, key: str) -> Quote: ...
    async def buy(self, order: dict) -> SupplierOrder: ...
    async def status(self, reference: str) -> SupplierOrder: ...
    async def reconcile(self, key: str) -> SupplierOrder | None: ...
    async def download(self, reference: str) -> bytes: ...
    async def close(self) -> None: ...


def local_catalog(settings: Settings, key: str) -> Quote:
    """A retail offer, not a compatibility check or an upload to DriveMods."""
    return Quote("local-" + key, time.time() + settings.quote_ttl, tuple(
        Offer(plan, "unresolved-" + plan, settings.cost(plan), settings.cost(plan) + settings.markup,
              settings.stars(plan) or (1 if settings.mode == "demo" else 0))
        for plan in ("full", "eco")
    ))


@dataclass
class DemoSupplier:
    settings: Settings

    async def quote(self, source: Path, key: str) -> Quote:
        # Local simulation only. It does not verify or transmit the device ID.
        return Quote("demo-" + key, time.time() + self.settings.quote_ttl, tuple(
            Offer(p, "demo-" + p, self.settings.cost(p), self.settings.cost(p) + self.settings.markup, self.settings.stars(p) or 1)
            for p in ("full", "eco")))

    async def buy(self, order: dict) -> SupplierOrder:
        return SupplierOrder("demo-" + order["id"], "ready")

    async def status(self, reference: str) -> SupplierOrder:
        return SupplierOrder(reference, "ready")

    async def reconcile(self, key: str) -> SupplierOrder | None:
        # Cannot prove a previous purchase after a restart; never invent one.
        return None

    async def download(self, reference: str) -> bytes:
        return ("DEMO ONLY - NOT A VALID DEVICE LICENSE\n"
                "Тестовый файл. Не устанавливать в автомобиль.\n" + reference + "\n").encode("utf-8")

    async def close(self) -> None:
        pass


class OperatorSupplier:
    """Operator mode can deliver approved files but cannot call the store."""
    async def quote(self, source: Path, key: str) -> Quote:
        raise OrderError("Лицензию оформляет оператор; автоматическая отправка ID отключена.")

    async def buy(self, order: dict) -> SupplierOrder:
        raise OrderError("Закупку выполняет оператор; списание с карты отключено.")

    async def status(self, reference: str) -> SupplierOrder:
        raise OrderError("Статус DriveMods проверяет оператор.")

    async def reconcile(self, key: str) -> SupplierOrder | None:
        raise OrderError("Используйте /ops для обработки заказа оператором.")

    async def download(self, reference: str) -> bytes:
        raise OrderError("Готовую лицензию передаёт оператор.")

    async def close(self) -> None:
        pass


class BridgeSupplier:
    """Strict HTTP client for an independently implemented, verified store bridge.

    No automatic POST retries. An uncertain purchase is reconciled by its key.
    Visa details/3DS belong to the payment processor, never the Telegram bot.
    """
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        if not settings.bridge_url or not settings.bridge_verified:
            raise ValueError("DriveMods bridge is not verified/configured")
        self.client = client or httpx.AsyncClient(
            base_url=settings.bridge_url + "/",
            headers={"Authorization": "Bearer " + settings.bridge_token},
            timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
        )

    async def quote(self, source: Path, key: str) -> Quote:
        if not self.settings.store_uploads_enabled:
            raise OrderError("Отправка ID в DriveMods отключена на время подготовки.")
        with source.open("rb") as stream:
            response = await self.client.post("v1/quotes", files={"device_id": ("device.id.bin", stream, "application/octet-stream")}, data={"external_reference": key}, headers={"Idempotency-Key": key})
        response.raise_for_status()
        data = response.json()
        expires = float(data["expires_at"])
        if not math.isfinite(expires) or expires <= time.time():
            raise OrderError("Магазин вернул истёкшее предложение.")
        expires = min(expires, time.time() + self.settings.quote_ttl)
        offers = []
        for item in data["offers"]:
            plan = item["plan"]
            if plan not in {"full", "eco"}:
                continue
            cost = item["cost_usd_cents"]
            if type(cost) is not int or cost != self.settings.cost(plan):
                raise OrderError("Закупочная цена изменилась. Владелец должен проверить тарифы.")
            check_purchase_budget(plan, cost)
            offers.append(Offer(plan, str(item["supplier_plan_id"]), cost, cost + self.settings.markup, self.settings.stars(plan)))
        if not offers or len({o.plan for o in offers}) != len(offers):
            raise OrderError("Магазин не вернул доступные пакеты для этого устройства.")
        return Quote(str(data["reference"]), expires, tuple(offers))

    def _receipt(self, response: httpx.Response) -> SupplierOrder:
        response.raise_for_status()
        data = response.json()
        if data["status"] not in {"pending", "action_required", "ready", "failed"} or not data.get("reference"):
            raise ValueError("Invalid supplier response")
        action = data.get("action_url")
        if action:
            url = urlparse(action)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Unsafe supplier payment URL")
        return SupplierOrder(str(data["reference"]), data["status"], action)

    async def buy(self, order: dict) -> SupplierOrder:
        if not self.settings.store_purchases_enabled:
            raise OrderError("Покупки в DriveMods отключены на время подготовки.")
        if not order.get("charge_id"):
            raise OrderError("Покупка без подтверждённой оплаты клиента запрещена.")
        if order["charge_id"].startswith("demo-"):
            raise OrderError("Тестовая оплата не разрешает реальную покупку.")
        check_purchase_budget(order["plan"], order["cost_cents"])
        previous_quote = Quote.loads(order["quote_json"])
        if previous_quote.reference.startswith("local-"):
            raise OrderError("Перед покупкой нужно получить предложение магазина для оплаченного заказа.")
        offer = next(o for o in previous_quote.offers if o.plan == order["plan"])
        if offer.cost_cents != order["cost_cents"] or previous_quote.expires_at <= time.time():
            raise OrderError("Цена магазина не совпадает с заказом или предложение истекло.")
        response = await self.client.post("v1/orders", json={
            "external_reference": order["id"], "quote_reference": previous_quote.reference,
            "supplier_plan_id": offer.supplier_plan_id, "expected_cost_usd_cents": order["cost_cents"],
            "device_sha256": order["sha256"],
        }, headers={"Idempotency-Key": order["id"]})
        return self._receipt(response)

    async def status(self, reference: str) -> SupplierOrder:
        return self._receipt(await self.client.get("v1/orders/" + quote(reference, safe="")))

    async def reconcile(self, key: str) -> SupplierOrder | None:
        response = await self.client.get("v1/orders/by-external-reference/" + quote(key, safe=""))
        if response.status_code == 404:
            return None
        return self._receipt(response)

    async def download(self, reference: str) -> bytes:
        async with self.client.stream("GET", "v1/orders/" + quote(reference, safe="") + "/license") as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0] in {"application/json", "text/html"}:
                raise ValueError("Supplier returned a web page instead of a license")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > self.settings.max_license:
                    raise ValueError("Supplier license exceeds the configured limit")
            return bytes(chunks)

    async def close(self) -> None:
        await self.client.aclose()


def validate_license(content: bytes, source_digest: str, settings: Settings) -> None:
    if not content or len(content) > settings.max_license:
        raise ValueError("Empty or oversized license")
    head = content.lstrip()[:50].lower()
    if content.startswith((b"%PDF-", b"\x89PNG\r\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        raise ValueError("Изображение или PDF не является файлом лицензии.")
    if settings.mode != "demo" and head.startswith(b"demo only - not a valid device license"):
        raise ValueError("Тестовый файл нельзя выдавать как настоящую лицензию.")
    if head.startswith((b"<!doctype", b"<html", b'{"error"', b'{"message"')):
        raise ValueError("An error response is not a device license")
    if hashlib.sha256(content).hexdigest() == source_digest:
        raise ValueError("Supplier returned the input file instead of a license")


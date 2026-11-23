import asyncio
import hashlib
import logging
from pathlib import Path
import time
from typing import Protocol
from uuid import uuid4

from .config import Settings
from .models import DeliveryRejected, OrderError, Quote, State, SupplierOrder, is_demo_order
from .purchase_policy import check_purchase_budget
from .storage import Database
from .supplier import Supplier, local_catalog, validate_license


class Messenger(Protocol):
    async def delivered(self, order: dict, path: Path, demo: bool) -> str: ...
    async def notify_admins(self, text: str) -> None: ...
    async def notified(self, chat_id: int, text: str) -> None: ...
    async def payment_qr(self, chat_id: int, text: str, path: Path, payment_url: str) -> None: ...


class Service:
    def __init__(self, settings: Settings, db: Database, supplier: Supplier):
        self.settings, self.db, self.supplier = settings, db, supplier
        self.order_dir = settings.data_dir / "orders"
        self.order_dir.mkdir(parents=True, exist_ok=True)

    def allocate(self) -> tuple[str, Path]:
        order_id = uuid4().hex
        folder = self.order_dir / order_id
        folder.mkdir()
        return order_id, folder / "device.id.bin"

    async def upload(self, order_id: str, user_id: int, chat_id: int, filename: str, path: Path) -> tuple[dict, bool]:
        if Path(filename).suffix.lower() not in {".bin", ".id"}:
            raise OrderError("Нужен файл device.id или device.id.bin.")
        if not path.is_file() or not 0 < path.stat().st_size <= self.settings.max_upload:
            raise OrderError("Файл пустой или превышает допустимый размер.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        order, created = self.db.add(order_id, user_id, chat_id, filename, path, digest, self.settings.mode)
        if not created:
            # An expired, unselected local offer must not trap a repeated upload.
            if order["state"] == State.QUOTED and Quote.loads(order["quote_json"]).expires_at <= time.time():
                self.db.update(order["id"], State.QUOTED, (State.QUOTED,), quote_json=local_catalog(self.settings, order["id"]).dumps())
                order = self.db.get(order["id"])
            return order, False
        # Uploading to DriveMods can create a payable, non-removable record.
        # Receiving a file in Telegram must therefore remain completely local.
        quote = local_catalog(self.settings, order_id)
        if not self.db.update(order_id, State.QUOTED, (State.UPLOADED,), quote_json=quote.dumps()):
            raise OrderError("Заказ уже обрабатывается.")
        return self.db.get(order_id), True

    async def _prepare_purchase(self, order: dict) -> dict:
        check_purchase_budget(order["plan"], order["cost_cents"])
        if self.settings.mode == "live" and not self.db.has_confirmed_payment(order):
            raise OrderError("Нет подтверждённой оплаты клиента от подключённого платёжного провайдера.")
        source = Path(order["source_path"])
        if not order["charge_id"] or hashlib.sha256(source.read_bytes()).hexdigest() != order["sha256"]:
            raise OrderError("Нельзя отправлять неоплаченный или изменённый ID.")
        # Called once, only after PAID -> BUYING has been persisted. Even an
        # uncertain quote/upload is reconciled; the device is never re-uploaded.
        quote = await self.supplier.quote(source, order["id"])
        offer = next((item for item in quote.offers if item.plan == order["plan"]), None)
        if not offer or offer.cost_cents != order["cost_cents"] or quote.expires_at <= time.time():
            raise OrderError("DriveMods не подтвердил выбранный пакет и согласованную закупочную цену.")
        if not self.db.update(order["id"], State.BUYING, (State.BUYING,), quote_json=quote.dumps()):
            raise OrderError("Состояние заказа изменилось.")
        return self.db.get(order["id"])

    async def _accept_receipt(self, order: dict, receipt: SupplierOrder, messenger: Messenger) -> None:
        order_id = order["id"]
        if order.get("supplier_ref") and receipt.reference != order["supplier_ref"]:
            raise ValueError("Supplier changed the order reference")
        if receipt.status == "failed":
            self.db.update(order_id, State.REVIEW, supplier_ref=receipt.reference, last_error="DriveMods сообщил об ошибке заказа; нужна сверка оплаты.")
            await messenger.notify_admins(f"Нужна проверка DriveMods: {order_id}")
            return
        old_action = order.get("action_url")
        # Persist the supplier reference before downloading or notifying anyone.
        self.db.update(order_id, State.WAITING, supplier_ref=receipt.reference, action_url=receipt.action_url, next_attempt=time.time() + 15, attempts=0)
        if receipt.status == "action_required" and (not order.get("supplier_ref") or old_action != receipt.action_url):
            await messenger.notify_admins(f"Нужно подтверждение платежа Visa для заказа {order_id}.\n"
                                          f"Заказ DriveMods: {receipt.reference}\n"
                                          + (receipt.action_url or "Проверьте кабинет DriveMods."))
        if receipt.status == "ready":
            content = await self.supplier.download(receipt.reference)
            validate_license(content, order["sha256"], self.settings)
            name = "DEMO-NOT-A-LICENSE.txt" if self.settings.mode == "demo" else "device.license.bin"
            output = self.order_dir / order_id / name
            part = output.with_suffix(output.suffix + ".part")
            part.write_bytes(content)
            part.replace(output)
            self.db.update(order_id, State.READY, output_path=str(output), output_sha256=hashlib.sha256(content).hexdigest(),
                           last_error=None, next_attempt=0, attempts=0)

    async def step(self, messenger: Messenger) -> bool:
        notice = self.db.claim_notice()
        if notice:
            try:
                if notice["attachment_path"]:
                    path = Path(notice["attachment_path"])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != notice["attachment_sha256"]:
                        raise DeliveryRejected("QR изменился после подготовки")
                    order = self.db.get(notice["id"].split(":")[1])
                    if order["state"] != State.OPERATOR_PAYMENT:
                        self.db.finish_notice(notice["id"], skipped=True)
                        return True
                    await messenger.payment_qr(notice["chat_id"], notice["body"], path, notice["payment_url"])
                else:
                    await messenger.notified(notice["chat_id"], notice["body"])
                self.db.finish_notice(notice["id"])
            except Exception as exc:
                self.db.finish_notice(notice["id"], type(exc).__name__)
            return True
        mutations_allowed = self.settings.mode == "demo" or (
            self.settings.store_uploads_enabled and self.settings.store_purchases_enabled)
        order = self.db.claim(State.PAID, State.BUYING) if mutations_allowed else None
        if order:
            try:
                order = await self._prepare_purchase(order)
                receipt = await self.supplier.buy(order)
            except Exception as exc:
                # The POST may have succeeded remotely. Do not buy again.
                self.db.update(order["id"], State.REVIEW, (State.BUYING,), last_error="Передача ID/покупка требуют сверки: " + type(exc).__name__)
                await messenger.notify_admins(f"Нужна сверка отправки ID или покупки {order['id']}. Повторная отправка и покупка заблокированы.\n/reconcile {order['id']}")
                return True
            try:
                await self._accept_receipt(order, receipt, messenger)
            except Exception as exc:
                # Keep a known reference even if validation/download fails.
                self.db.update(order["id"], State.WAITING, supplier_ref=receipt.reference, next_attempt=time.time() + 15, last_error="Подготовка файла: " + type(exc).__name__)
            return True

        order = self.db.claim(State.WAITING, State.BUYING)
        if order:
            try:
                receipt = await self.supplier.status(order["supplier_ref"])
                await self._accept_receipt(order, receipt, messenger)
            except Exception as exc:
                attempts = order["attempts"] + 1
                state = State.REVIEW if attempts >= 10 else State.WAITING
                self.db.update(order["id"], state, supplier_ref=order["supplier_ref"], attempts=attempts,
                               next_attempt=time.time() + min(15 * 2 ** min(attempts, 6), 900),
                               last_error="Проверка DriveMods: " + type(exc).__name__)
                if state == State.REVIEW:
                    await messenger.notify_admins(f"DriveMods долго не отвечает, заказ {order['id']}. Нужна сверка.")
            return True

        order = self.db.claim(State.READY, State.SENDING)
        if order:
            try:
                output = Path(order["output_path"])
                if order.get("output_sha256") and hashlib.sha256(output.read_bytes()).hexdigest() != order["output_sha256"]:
                    raise DeliveryRejected("Файл изменился после проверки")
                file_id = await messenger.delivered(order, output, is_demo_order(order))
                self.db.update(order["id"], State.DELIVERED, (State.SENDING,), telegram_file_id=file_id, last_error=None)
            except Exception as exc:
                attempts = order["attempts"] + 1
                state = State.REVIEW if isinstance(exc, DeliveryRejected) or attempts >= 5 else State.READY
                self.db.update(order["id"], state, (State.SENDING,), attempts=attempts, next_attempt=time.time() + 30 * attempts,
                               last_error="Отправка файла: " + type(exc).__name__)
                if state == State.REVIEW:
                    await messenger.notify_admins(f"Лицензия готова, но отправка не удалась: {order['id']}. Покупать повторно не нужно.")
            return True
        return False

    async def reconcile(self, order_id: str, messenger: Messenger) -> None:
        order = self.db.get(order_id)
        if order["state"] != State.REVIEW or not order["charge_id"]:
            raise OrderError("Сверка доступна только для оплаченного заказа, ожидающего проверки.")
        if order["supplier_ref"] and order["output_path"]:
            self.db.update(order_id, State.READY, (State.REVIEW,), next_attempt=0, attempts=0)
            return
        receipt = await self.supplier.reconcile(order_id)
        if receipt is None:
            raise OrderError("Заказ не найден в DriveMods. Автоматическая повторная покупка не выполняется; проверьте списание Visa.")
        await self._accept_receipt(order, receipt, messenger)

    async def worker(self, messenger: Messenger) -> None:
        while True:
            try:
                worked = await self.step(messenger)
            except asyncio.CancelledError:
                raise
            except Exception:
                # State remains durable even when notification or Telegram fails.
                logging.getLogger(__name__).error("Worker operation failed; persisted order state needs inspection")
                worked = False
            await asyncio.sleep(0.2 if worked else 2)


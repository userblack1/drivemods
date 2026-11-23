import asyncio
from datetime import datetime, timezone
import pytest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User

from drivemods_bot.config import Settings
from drivemods_bot.operator_orders import OperatorOrders, review_token
from drivemods_bot.service import Service
from drivemods_bot.storage import Database
from drivemods_bot.supplier import DemoSupplier, OperatorSupplier
from drivemods_bot.telegram import TelegramMessenger, make_router


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if method.__api_method__ == "sendMessage":
            return Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if method.__api_method__ == "sendDocument":
            return Message(message_id=101, date=datetime.now(timezone.utc), chat=Chat(id=method.chat_id, type="private"),
                           document={"file_id": "test-delivered-file", "file_unique_id": "test-file-unique"})
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


def make_app(tmp_path, mode="demo", **settings_overrides):
    settings = Settings(mode=mode, data_dir=tmp_path, admin_ids=frozenset({11}), full_stars=10000, eco_stars=7000, **settings_overrides)
    db = Database(tmp_path / "orders.sqlite3")
    service = Service(settings, db, OperatorSupplier() if mode == "operator" else DemoSupplier(settings))
    session = RecordingSession()
    bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
    messenger = TelegramMessenger(bot, settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(service, messenger))
    return db, service, session, bot, dispatcher


def test_actual_dispatcher_rejects_live_precheckout_in_demo(tmp_path):
    _, _, session, bot, dispatcher = make_app(tmp_path)
    update = Update.model_validate({"update_id": 1, "pre_checkout_query": {
        "id": "query", "from": {"id": 11, "is_bot": False, "first_name": "Owner"},
        "currency": "XTR", "total_amount": 10000, "invoice_payload": "dm:" + "a" * 32 + ":full",
    }})
    asyncio.run(dispatcher.feed_update(bot, update))
    assert len(session.methods) == 1
    assert session.methods[0].__api_method__ == "answerPreCheckoutQuery"
    assert session.methods[0].ok is False


def test_status_shows_demo_placement_only_to_owner(tmp_path):
    _, _, session, bot, dispatcher = make_app(tmp_path)

    async def scenario():
        for user_id in (12, 11):
            update = Update.model_validate({"update_id": user_id, "message": {
                "message_id": user_id, "date": 1794050000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                "text": "/status", "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            }})
            await dispatcher.feed_update(bot, update)
        assert len(session.methods) == 1
        answer = session.methods[0]
        assert answer.chat_id == 11
        assert "Размещение: Локальный компьютер" in answer.text
        assert "Режим: тестовый" in answer.text
        assert "Отправка ID в DriveMods: отключена" in answer.text
        assert "Реальные оплаты: отключены" in answer.text

    asyncio.run(scenario())


def test_unmatched_real_payment_is_recorded_and_refunded(tmp_path):
    db, _, session, bot, dispatcher = make_app(tmp_path, "live")
    update = Update.model_validate({"update_id": 2, "message": {
        "message_id": 2, "date": 1794050000, "chat": {"id": 11, "type": "private"},
        "from": {"id": 11, "is_bot": False, "first_name": "Owner"},
        "successful_payment": {"currency": "XTR", "total_amount": 10000,
            "invoice_payload": "dm:" + "a" * 32 + ":full", "telegram_payment_charge_id": "unknown-charge", "provider_payment_charge_id": ""},
    }})
    asyncio.run(dispatcher.feed_update(bot, update))
    assert [m.__api_method__ for m in session.methods] == ["refundStarPayment", "sendMessage"]
    assert db.record_receipt("unknown-charge", 11, "dm:" + "a" * 32 + ":full", "XTR", 10000) == "refunded"
    # Re-delivery of the same Telegram update must not refund or buy twice.
    asyncio.run(dispatcher.feed_update(bot, update))
    assert len(session.methods) == 2


def test_valid_receipt_persists_before_fulfillment_and_replay(tmp_path):
    db, service, session, bot, dispatcher = make_app(tmp_path, "live")
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"test input")
        order, _ = await service.upload(order_id, 11, 11, "device.bin", path)
        db.choose(order_id, 11, "full")
        update = Update.model_validate({"update_id": 3, "message": {
            "message_id": 3, "date": 1794050000, "chat": {"id": 11, "type": "private"},
            "from": {"id": 11, "is_bot": False, "first_name": "Owner"},
            "successful_payment": {"currency": "XTR", "total_amount": 10000,
                "invoice_payload": f"dm:{order_id}:full", "telegram_payment_charge_id": "charge", "provider_payment_charge_id": ""},
        }})
        await dispatcher.feed_update(bot, update)
        await dispatcher.feed_update(bot, update)
        assert db.get(order_id)["state"] == "paid"
        assert [m.__api_method__ for m in session.methods] == ["sendMessage", "sendMessage"]
        assert "Подтверждена оплата Telegram Stars" in session.methods[0].text
        assert "Закупка DriveMods: 90 $" in session.methods[0].text
        assert db.record_receipt("charge", 11, f"dm:{order_id}:full", "XTR", 10000) == "matched"
    asyncio.run(scenario())


@pytest.mark.parametrize("evidence", [
    {"photo": [{"file_id": "photo", "file_unique_id": "photo-unique", "width": 100, "height": 100}]},
    {"document": {"file_id": "pdf", "file_unique_id": "pdf-unique", "file_name": "receipt.pdf", "mime_type": "application/pdf", "file_size": 100}},
    {"document": {"file_id": "png", "file_unique_id": "png-unique", "file_name": "receipt.png", "mime_type": "image/png", "file_size": 100}},
    {"document": {"file_id": "renamed", "file_unique_id": "renamed-unique", "file_name": "device.bin", "mime_type": "application/pdf", "file_size": 100}},
])
def test_receipt_image_or_pdf_never_marks_order_paid_or_starts_supplier(tmp_path, evidence):
    db, service, session, bot, dispatcher = make_app(tmp_path, "live")

    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"test input")
        order, _ = await service.upload(order_id, 12, 12, "device.bin", path)
        update = Update.model_validate({"update_id": 10, "message": {
            "message_id": 10, "date": 1794050000, "chat": {"id": 12, "type": "private"},
            "from": {"id": 12, "is_bot": False, "first_name": "Customer"}, **evidence}})
        await dispatcher.feed_update(bot, update)
        assert db.get(order["id"])["state"] == "quoted"
        assert db.get(order["id"])["charge_id"] is None
        assert not await service.step(TelegramMessenger(bot, service.settings))
        assert [m.__api_method__ for m in session.methods] == ["sendMessage"]
        assert "не меняет статус заказа" in session.methods[0].text
    asyncio.run(scenario())


def test_payment_info_has_confirmed_retail_prices_and_no_unconnected_bank_invoice(tmp_path):
    _, _, session, bot, dispatcher = make_app(tmp_path)
    update = Update.model_validate({"update_id": 11, "message": {
        "message_id": 11, "date": 1794050000, "chat": {"id": 12, "type": "private"},
        "from": {"id": 12, "is_bot": False, "first_name": "Customer"},
        "text": "/paymentinfo", "entities": [{"type": "bot_command", "offset": 0, "length": 12}]}})
    asyncio.run(dispatcher.feed_update(bot, update))
    assert len(session.methods) == 1
    text = session.methods[0].text
    assert "FULL — 100 $" in text and "ECO — 70 $" in text
    assert "пока не подключена" in text
    assert "90 $" not in text and "60 $" not in text


def test_payment_notification_reaches_owner_without_disclosing_wholesale_price_to_customer(tmp_path):
    db, service, session, bot, dispatcher = make_app(tmp_path, "live")

    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"customer device")
        await service.upload(order_id, 12, 12, "device.bin", path)
        db.choose(order_id, 12, "eco")
        update = Update.model_validate({"update_id": 20, "message": {
            "message_id": 20, "date": 1794050000, "chat": {"id": 12, "type": "private"},
            "from": {"id": 12, "is_bot": False, "first_name": "Customer"},
            "successful_payment": {"currency": "XTR", "total_amount": 7000,
                "invoice_payload": f"dm:{order_id}:eco", "telegram_payment_charge_id": "eco-charge", "provider_payment_charge_id": ""}}})
        await dispatcher.feed_update(bot, update)
        await dispatcher.feed_update(bot, update)
        owner = [m for m in session.methods if getattr(m, "chat_id", None) == 11]
        customer = [m for m in session.methods if getattr(m, "chat_id", None) == 12]
        assert len(owner) == len(customer) == 1
        assert "Закупка DriveMods: 60 $" in owner[0].text
        assert "Розничный тариф: 70 $" in owner[0].text
        assert "Оплата получена" in customer[0].text and "60 $" not in customer[0].text
        assert db.get(order_id)["state"] == "paid"
    asyncio.run(scenario())


def callback_update(update_id, who, data):
    return Update.model_validate({"update_id": update_id, "callback_query": {
        "id": str(update_id), "from": {"id": who, "is_bot": False, "first_name": "User"},
        "chat_instance": "private", "data": data,
        "message": {"message_id": 100, "date": 1794050000,
                    "chat": {"id": who, "type": "private"}, "text": "offer"}}})


def text_update(update_id, who, text):
    command = text.split()[0]
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id, "date": 1794050000, "chat": {"id": who, "type": "private"},
        "from": {"id": who, "is_bot": False, "first_name": "User"}, "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}]}})


def test_operator_ui_receives_order_and_requires_owner_confirmation(tmp_path):
    db, service, session, bot, dispatcher = make_app(tmp_path, "operator")
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC OPERATOR DEVICE")
        await service.upload(order_id, 12, 12, "device.bin", path)
        await dispatcher.feed_update(bot, callback_update(100, 12, f"choose:{order_id}:full"))
        assert db.get(order_id)["state"] == "operator_payment"
        assert all(m.__api_method__ != "sendInvoice" for m in session.methods)
        await dispatcher.feed_update(bot, text_update(101, 12, f"bill {order_id} USD 1 test"))
        await dispatcher.feed_update(bot, text_update(102, 11, f"/bill {order_id} USD 100.00 TEST RECIPIENT"))
        await dispatcher.feed_update(bot, text_update(103, 11, f"/paid {order_id} MBANK TEST-BANK-1234"))
        assert db.get(order_id)["state"] == "operator_payment"
        token = review_token(OperatorOrders(service.settings, db).get(order_id, 11), "payment")
        await dispatcher.feed_update(bot, callback_update(104, 12, f"oppay:{order_id}:{token}"))
        assert db.get(order_id)["state"] == "operator_payment"
        await dispatcher.feed_update(bot, callback_update(105, 11, f"oppay:{order_id}:{token}"))
        assert db.get(order_id)["state"] == "operator_paid"
        assert db.get(order_id)["charge_id"] is None
        assert not db.has_confirmed_payment(db.get(order_id))
    asyncio.run(scenario())


def test_operator_document_requires_final_review_before_delivery(tmp_path, monkeypatch):
    from drivemods_bot.operator_orders import OperatorOrders
    db, service, session, bot, dispatcher = make_app(tmp_path, "operator")
    async def download(self, doc, destination, **kwargs):
        destination.write_bytes(b"\x00UNIT TEST ONLY - SYNTHETIC LICENSE\x00")
    monkeypatch.setattr(Bot, "download", download)
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC DEVICE")
        await service.upload(order_id, 12, 12, "device.bin", path)
        ops = OperatorOrders(service.settings, db)
        ops.select(order_id, 12, "full")
        ops.bill(order_id, 11, "USD", 10000, "TEST RECEIVER")
        ops.propose_payment(order_id, 11, "MBANK", "TEST-1234")
        ops.confirm_payment(order_id, 11)
        update = Update.model_validate({"update_id": 200, "message": {
            "message_id": 200, "date": 1794050000, "chat": {"id": 11, "type": "private"},
            "from": {"id": 11, "is_bot": False, "first_name": "Owner"},
            "caption": f"/license {order_id} TEST-STORE-ID DM-ABC123",
            "document": {"file_id": "test-license", "file_unique_id": "unique-test-license", "file_name": "license.bin", "file_size": 40}}})
        await dispatcher.feed_update(bot, update)
        assert db.get(order_id)["state"] == "operator_file"
        await dispatcher.feed_update(bot, text_update(201, 11, "/review " + order_id))
        assert any(getattr(m, "reply_markup", None) for m in session.methods)
        token = review_token(ops.get(order_id, 11), "license")
        await dispatcher.feed_update(bot, callback_update(202, 12, f"opfile:{order_id}:{token}"))
        assert db.get(order_id)["state"] == "operator_file"
        await dispatcher.feed_update(bot, callback_update(203, 11, f"opfile:{order_id}:{token}"))
        assert db.get(order_id)["state"] == "ready"
    asyncio.run(scenario())


def test_duplicate_demo_payment_button_does_not_repeat_fulfillment_ack(tmp_path):
    db, service, session, bot, dispatcher = make_app(tmp_path)
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC DEMO DEVICE")
        await service.upload(order_id, 11, 11, "device.bin", path)
        db.choose(order_id, 11, "full")
        await dispatcher.feed_update(bot, callback_update(300, 11, f"testpay:{order_id}:full"))
        await dispatcher.feed_update(bot, callback_update(301, 11, f"testpay:{order_id}:full"))
        assert len([m for m in session.methods if m.__api_method__ == "sendMessage"]) == 1
    asyncio.run(scenario())


def test_operator_net_credit_amount_is_parsed_and_shown_before_confirmation(tmp_path):
    from drivemods_bot.operator_orders import OperatorOrders
    db, service, session, bot, dispatcher = make_app(tmp_path, "operator", require_usd_net_credit=True, receiving_bank="O!Bank")
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC NET CREDIT CHECK")
        await service.upload(order_id, 12, 12, "device.id.bin", path)
        ops = OperatorOrders(service.settings, db)
        ops.select(order_id, 12, "full")
        ops.bill(order_id, 11, "USD", 10000, "TEST RECEIVER ONLY")
        await dispatcher.feed_update(bot, text_update(400, 11, f"/paid {order_id} OBANK TEST-1234"))
        assert ops.get(order_id, 11)["bank_reference"] is None
        await dispatcher.feed_update(bot, text_update(401, 11, f"/paid {order_id} OBANK TEST-1234 100.00"))
        assert ops.get(order_id, 11)["net_usd_cents"] == 10000
        assert any("Чистое зачисление по этой операции: 100.00 USD" in getattr(m, "text", "") for m in session.methods)
        assert db.get(order_id)["state"] == "operator_payment"
    asyncio.run(scenario())


@pytest.mark.parametrize("plan,retail,cost", [("full", 10000, 9000), ("eco", 7000, 6000)])
def test_operator_dispatcher_complete_order_with_exact_net_credit(tmp_path, monkeypatch, plan, retail, cost):
    """Exercise real routing, both roles, documents, buttons and delivery together."""
    from pathlib import Path
    from drivemods_bot.models import State
    db, service, session, bot, dispatcher = make_app(
        tmp_path, "operator", require_usd_net_credit=True, receiving_bank="O!Bank")
    ops = OperatorOrders(service.settings, db)
    synthetic_id = b"SYNTHETIC ROUTER DEVICE - NOT FOR STORE " + plan.encode()
    synthetic_license = b"\x00SYNTHETIC ROUTER OUTPUT - UNIT TEST ONLY\x00"
    async def download(self, doc, destination, **kwargs):
        destination.write_bytes(synthetic_id if doc.file_id == "test-device" else synthetic_license)
    monkeypatch.setattr(Bot, "download", download)

    async def drain(current):
        for _ in range(30):
            if not await current.step(TelegramMessenger(bot, current.settings)):
                return
        pytest.fail("Operator worker did not settle")

    async def feed_document(update_id, who, file_id, caption=None):
        await dispatcher.feed_update(bot, Update.model_validate({"update_id": update_id, "message": {
            "message_id": update_id, "date": 1794050000, "chat": {"id": who, "type": "private"},
            "from": {"id": who, "is_bot": False, "first_name": "Test"}, "caption": caption,
            "document": {"file_id": file_id, "file_unique_id": file_id + "-unique",
                         "file_name": "device.id.bin" if file_id == "test-device" else "license.bin", "file_size": 50}}}))

    async def scenario():
        await dispatcher.feed_update(bot, text_update(500, 12, "/start"))
        await feed_document(501, 12, "test-device")
        order = db.recent(12)[0]
        order_id = order["id"]
        offers = session.methods[-1].reply_markup.inline_keyboard
        assert {button.callback_data.rsplit(":", 1)[1] for row in offers for button in row} == {"full", "eco"}
        for update_id in (502, 503):
            await dispatcher.feed_update(bot, callback_update(update_id, 12, f"choose:{order_id}:{plan}"))
        await drain(service)
        assert db.get(order_id)["retail_cents"] == retail and db.get(order_id)["cost_cents"] == cost
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT

        # A receipt and a buyer's operator command must not approve anything.
        await dispatcher.feed_update(bot, Update.model_validate({"update_id": 504, "message": {
            "message_id": 504, "date": 1794050000, "chat": {"id": 12, "type": "private"},
            "from": {"id": 12, "is_bot": False, "first_name": "Test"},
            "photo": [{"file_id": "fake-receipt", "file_unique_id": "fake-receipt-unique", "width": 10, "height": 10}]}}))
        await dispatcher.feed_update(bot, text_update(505, 12, f"/paid {order_id} OBANK TEST-SELF {retail / 100:.2f}"))
        assert ops.get(order_id, 11)["bank_reference"] is None

        await dispatcher.feed_update(bot, text_update(506, 11, f"/bill {order_id} USD {retail / 100:.2f} TEST ONLY - DO NOT PAY"))
        await drain(service)
        assert any(f"Согласованная сумма: {retail / 100:.2f} USD" in getattr(m, "text", "") for m in session.methods)
        await dispatcher.feed_update(bot, text_update(507, 11, f"/paid {order_id} OBANK TEST-WRONG {(retail - 1) / 100:.2f}"))
        assert ops.get(order_id, 11)["bank_reference"] is None
        await dispatcher.feed_update(bot, text_update(508, 11, f"/paid {order_id} OBANK TEST-{plan.upper()} {retail / 100:.2f}"))
        payment_button = session.methods[-1].reply_markup.inline_keyboard[0][0].callback_data
        assert len(payment_button.encode()) <= 64
        await dispatcher.feed_update(bot, callback_update(509, 12, payment_button))
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT
        # Old messages from the previous deployment cannot approve a new candidate.
        await dispatcher.feed_update(bot, callback_update(510, 11, "oppay:" + order_id))
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT
        await dispatcher.feed_update(bot, callback_update(511, 11, payment_button))
        await dispatcher.feed_update(bot, callback_update(512, 11, payment_button))
        assert db.get(order_id)["state"] == State.OPERATOR_PAID
        assert db.get(order_id)["charge_id"] is None
        await drain(service)
        assert not any(m.__api_method__ == "sendDocument" for m in session.methods)

        await dispatcher.feed_update(bot, text_update(513, 11, "/device " + order_id))
        source_delivery = session.methods[-1]
        assert source_delivery.document.filename == "device.id.bin"
        assert Path(source_delivery.document.path).read_bytes() == synthetic_id
        await feed_document(514, 11, "test-license", f"/license {order_id} TEST-STORE-{plan.upper()} DM-TEST123")
        assert db.get(order_id)["state"] == State.OPERATOR_FILE
        file_button = session.methods[-1].reply_markup.inline_keyboard[0][0].callback_data
        assert len(file_button.encode()) <= 64
        await dispatcher.feed_update(bot, callback_update(515, 12, file_button))
        assert db.get(order_id)["state"] == State.OPERATOR_FILE
        await dispatcher.feed_update(bot, callback_update(516, 11, file_button))
        await dispatcher.feed_update(bot, callback_update(517, 11, file_button))
        # Approval and the outbox must survive process restart.
        reopened = Database(db.path)
        reopened.recover()
        restarted = Service(service.settings, reopened, OperatorSupplier())
        await drain(restarted)
        assert reopened.get(order_id)["state"] == State.DELIVERED
        files = [m for m in session.methods if m.__api_method__ == "sendDocument" and m.document.filename == "device.license.bin"]
        assert len(files) == 1 and files[0].chat_id == 12
        assert Path(files[0].document.path).read_bytes() == synthetic_license
        await dispatcher.feed_update(bot, text_update(518, 12, "/resend " + order_id))
        files = [m for m in session.methods if m.__api_method__ == "sendDocument" and m.document.filename == "device.license.bin"]
        assert len(files) == 2
        assert not any(m.__api_method__ in {"sendInvoice", "refundStarPayment"} for m in session.methods)
        customer_text = " ".join(getattr(m, "text", "") or "" for m in session.methods if getattr(m, "chat_id", None) == 12)
        assert "Закупка" not in customer_text

    asyncio.run(scenario())


def test_operator_resend_recovers_failed_delivery_instead_of_buying_again(tmp_path):
    from drivemods_bot.models import State
    db, service, session, bot, dispatcher = make_app(tmp_path, "operator", require_usd_net_credit=True)
    ops = OperatorOrders(service.settings, db)
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC RECOVERY DEVICE")
        await service.upload(order_id, 12, 12, "device.id.bin", path)
        ops.select(order_id, 12, "eco")
        ops.bill(order_id, 11, "USD", 7000, "TEST ONLY RECIPIENT")
        ops.propose_payment(order_id, 11, "OBANK", "TEST-RECOVERY", 7000)
        ops.confirm_payment(order_id, 11)
        output = path.parent / "test-license.bin"
        output.write_bytes(b"\x00SYNTHETIC RECOVERY LICENSE\x00")
        ops.stage_license(order_id, 11, "TEST-RECOVERY", "DM-TEST123", output)
        ops.approve_license(order_id, 11)
        db.update(order_id, State.REVIEW, last_error="Отправка файла: DeliveryRejected")
        await dispatcher.feed_update(bot, text_update(600, 12, "/resend " + order_id))
        assert db.get(order_id)["state"] == State.REVIEW
        await dispatcher.feed_update(bot, text_update(601, 11, "/review " + order_id))
        assert "/resend" in session.methods[-1].text
        await dispatcher.feed_update(bot, text_update(602, 11, "/resend " + order_id))
        assert db.get(order_id)["state"] == State.READY
        for _ in range(20):
            if not await service.step(TelegramMessenger(bot, service.settings)):
                break
        assert db.get(order_id)["state"] == State.DELIVERED
    asyncio.run(scenario())


@pytest.mark.parametrize("plan,amount", [("full", "8750.00"), ("eco", "6125.00")])
def test_operator_qr_is_sent_after_choice_and_recovered_only_by_its_customer(tmp_path, bank_qr_source, plan, amount):
    from drivemods_bot.models import State
    db, service, session, bot, dispatcher = make_app(
        tmp_path, "operator", receiving_bank="MBANK", require_usd_net_credit=True,
        mbank_qr_enabled=True, mbank_qr_source=bank_qr_source, mbank_qr_kgs_per_usd="87.50")
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC QR DEVICE")
        await service.upload(order_id, 12, 12, "device.id.bin", path)
        await dispatcher.feed_update(bot, callback_update(700, 12, f"choose:{order_id}:{plan}"))
        for _ in range(20):
            if not await service.step(TelegramMessenger(bot, service.settings)):
                break
        documents = [m for m in session.methods if m.__api_method__ == "sendDocument"]
        assert len(documents) == 1
        qr = documents[0]
        assert qr.chat_id == 12 and qr.document.filename == "payment-qr.png"
        assert f"К оплате: {amount} KGS" in qr.caption
        assert qr.reply_markup.inline_keyboard[0][0].url.startswith("https://pay.payqr.kg/qr/#")
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT
        await dispatcher.feed_update(bot, text_update(701, 99, "/pay " + order_id))
        assert len([m for m in session.methods if m.__api_method__ == "sendDocument"]) == 1
        await dispatcher.feed_update(bot, text_update(702, 12, "/pay " + order_id))
        documents = [m for m in session.methods if m.__api_method__ == "sendDocument"]
        assert len(documents) == 2 and documents[1].document.path == qr.document.path
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT
        assert not any(m.__api_method__ == "sendInvoice" for m in session.methods)
    asyncio.run(scenario())


@pytest.mark.parametrize("plan,amount", [("full", "9000.00"), ("eco", "6500.00")])
def test_fixed_mbank_qr_commands_confirm_the_exact_kgs_credit(tmp_path, bank_qr_source, plan, amount):
    from drivemods_bot.models import State
    db, service, session, bot, dispatcher = make_app(
        tmp_path, "operator", receiving_bank="MBANK", require_usd_net_credit=True,
        mbank_qr_enabled=True, mbank_qr_source=bank_qr_source,
        mbank_qr_full_kgs_minor=900000, mbank_qr_eco_kgs_minor=650000)
    async def scenario():
        order_id, path = service.allocate()
        path.write_bytes(b"SYNTHETIC FIXED KGS DEVICE")
        await service.upload(order_id, 12, 12, "device.id.bin", path)
        await dispatcher.feed_update(bot, callback_update(800, 12, f"choose:{order_id}:{plan}"))
        await dispatcher.feed_update(bot, text_update(801, 11, f"/paid {order_id} MBANK TEST-TOO-LOW 100.00"))
        assert OperatorOrders(service.settings, db).get(order_id, 11)["bank_reference"] is None
        await dispatcher.feed_update(bot, text_update(802, 11, f"/paid {order_id} MBANK TEST-EXACT {amount}"))
        assert f"Чистое зачисление по этой операции: {amount} KGS" in session.methods[-1].text
        assert "Счёт получателя: MBANK · KGS" in session.methods[-1].text
        button = session.methods[-1].reply_markup.inline_keyboard[0][0].callback_data
        assert db.get(order_id)["state"] == State.OPERATOR_PAYMENT
        await dispatcher.feed_update(bot, callback_update(803, 11, button))
        assert db.get(order_id)["state"] == State.OPERATOR_PAID
    asyncio.run(scenario())


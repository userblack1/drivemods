import asyncio
from dataclasses import replace
from pathlib import Path
import time

import pytest

from drivemods_bot.config import Settings
from drivemods_bot.models import DeliveryRejected, OrderError, Quote, State
from drivemods_bot.operator_orders import OperatorOrders, review_token
from drivemods_bot.server_operator import server_settings
from drivemods_bot.service import Service
from drivemods_bot.storage import Database
from drivemods_bot.supplier import DemoSupplier, OperatorSupplier


class Capture:
    def __init__(self):
        self.notices, self.files, self.alerts = [], [], []
        self.offline = False

    async def notified(self, chat_id, text):
        if self.offline:
            raise OSError("offline")
        self.notices.append((chat_id, text))

    async def delivered(self, order, path, demo):
        self.files.append((order["id"], path.read_bytes(), demo))
        return "telegram-document-id"

    async def notify_admins(self, text):
        self.alerts.append(text)


@pytest.fixture
def app(tmp_path):
    settings = Settings(mode="operator", data_dir=tmp_path, admin_ids=frozenset({11, 13}), support="@owner")
    db = Database(tmp_path / "orders.sqlite3")
    return Service(settings, db, OperatorSupplier()), OperatorOrders(settings, db), Capture()


def upload(service, content=b"SYNTHETIC DEVICE ID", user=12):
    order_id, source = service.allocate()
    source.write_bytes(content)
    return asyncio.run(service.upload(order_id, user, user, "device.id.bin", source))[0]


def selected(service, ops, *, plan="full", content=b"SYNTHETIC DEVICE ID"):
    order = upload(service, content)
    assert ops.select(order["id"], 12, plan)
    return service.db.get(order["id"])


def paid(service, ops, **kwargs):
    order = selected(service, ops, **kwargs)
    ops.bill(order["id"], 11, "KGS", 875000, "TEST RECIPIENT - NO REAL ACCOUNT")
    ops.propose_payment(order["id"], 11, "MBANK", "TEST-OPERATION-" + order["id"])
    ops.confirm_payment(order["id"], 11)
    return service.db.get(order["id"])


def stage(service, ops, order, *, content=b"\x00UNIT TEST ONLY - SYNTHETIC LICENSE\x00"):
    path = Path(order["source_path"]).parent / "uploaded-license.bin"
    path.write_bytes(content)
    return ops.stage_license(order["id"], 11, "TEST-" + order["id"], "DM-ABC123", path)


def drain(service, messenger):
    for _ in range(20):
        if not asyncio.run(service.step(messenger)):
            break


@pytest.mark.parametrize("plan,cost,retail", [("full", 9000, 10000), ("eco", 6000, 7000)])
def test_operator_full_chain_does_not_use_automatic_bank_or_supplier_clients(app, monkeypatch, plan, cost, retail):
    service, ops, messenger = app
    async def forbidden(*args, **kwargs):
        pytest.fail("Operator flow must not invoke automated supplier operations")
    for name in ("quote", "buy", "status", "download", "reconcile"):
        monkeypatch.setattr(service.supplier, name, forbidden)
    order = paid(service, ops, plan=plan)
    assert order["charge_id"] is None and order["state"] == State.OPERATOR_PAID
    assert order["cost_cents"] == cost and order["retail_cents"] == retail
    drain(service, messenger)
    assert not messenger.files
    stage(service, ops, order)
    drain(service, messenger)
    assert not messenger.files
    assert ops.approve_license(order["id"], 11)
    assert ops.approve_license(order["id"], 11) is False
    drain(service, messenger)
    assert service.db.get(order["id"])["state"] == State.DELIVERED
    assert len(messenger.files) == 1 and messenger.files[0][2] is False
    customer_text = " ".join(text for who, text in messenger.notices if who == 12)
    assert "Закупка" not in customer_text and "Оператор подтвердил" in customer_text


def test_bank_reference_can_only_fund_one_order_and_confirmation_is_explicit(app):
    service, ops, messenger = app
    first = selected(service, ops)
    second = selected(service, ops, content=b"DIFFERENT SYNTHETIC ID")
    for order in (first, second):
        ops.bill(order["id"], 11, "USD", 10000, "TEST RECIPIENT ONLY")
    ops.propose_payment(first["id"], 11, "OBANK", "BANK-TEST-1234")
    assert service.db.get(first["id"])["state"] == State.OPERATOR_PAYMENT
    with pytest.raises(OrderError, match="операция"):
        ops.propose_payment(second["id"], 11, "OBANK", "BANK-TEST-1234")
    with pytest.raises(OrderError):
        ops.confirm_payment(first["id"], 13)
    ops.confirm_payment(first["id"], 11)
    assert ops.confirm_payment(first["id"], 11) is False


def test_customer_cannot_confirm_payment_or_license_or_inspect_another_order(app):
    service, ops, _ = app
    order = selected(service, ops)
    for operation in (
        lambda: ops.get(order["id"], 12),
        lambda: ops.confirm_payment(order["id"], 12),
        lambda: ops.approve_license(order["id"], 12),
        lambda: ops.select(order["id"], 99, "full"),
    ):
        with pytest.raises(OrderError):
            operation()


def test_payment_requires_a_fixed_bill_and_cannot_change_amount_silently(app):
    service, ops, _ = app
    order = selected(service, ops)
    with pytest.raises(OrderError):
        ops.propose_payment(order["id"], 11, "MBANK", "TEST-1234")
    with pytest.raises(OrderError):
        ops.bill(order["id"], 11, "USD", 1, "TEST RECIPIENT")
    ops.bill(order["id"], 11, "KGS", 875000, "TEST RECIPIENT")
    assert ops.bill(order["id"], 11, "KGS", 875000, "TEST RECIPIENT") is False
    with pytest.raises(OrderError):
        ops.bill(order["id"], 11, "KGS", 999999, "TEST RECIPIENT")


@pytest.mark.parametrize("content", [b"<html>login</html>", b"SYNTHETIC DEVICE ID", b"DEMO ONLY - NOT A VALID DEVICE LICENSE\n", b"", b"%PDF-1.7 FAKE RECEIPT", b"\x89PNG\r\nFAKE RECEIPT", b"\xff\xd8\xffFAKE PHOTO"])
def test_wrong_or_demo_file_is_not_accepted_for_operator_delivery(app, content):
    service, ops, messenger = app
    order = paid(service, ops)
    with pytest.raises(ValueError):
        stage(service, ops, order, content=content)
    drain(service, messenger)
    assert not messenger.files
    assert service.db.get(order["id"])["state"] == State.OPERATOR_PAID


@pytest.mark.parametrize("tamper", ["source", "license"])
def test_tampered_source_or_license_blocks_final_approval(app, tamper):
    service, ops, _ = app
    order = paid(service, ops)
    prepared = stage(service, ops, order)
    Path(prepared["source_path"] if tamper == "source" else prepared["output_path"]).write_bytes(b"MODIFIED")
    with pytest.raises(OrderError, match="измен"):
        ops.approve_license(order["id"], 11)
    assert service.db.get(order["id"])["state"] == State.OPERATOR_FILE


def test_file_changed_after_approval_is_not_delivered(app):
    service, ops, messenger = app
    order = paid(service, ops)
    prepared = stage(service, ops, order)
    ops.approve_license(order["id"], 11)
    Path(prepared["output_path"]).write_bytes(b"MODIFIED AFTER APPROVAL")
    drain(service, messenger)
    assert not messenger.files and service.db.get(order["id"])["state"] == State.REVIEW


def test_pending_operator_confirmation_survives_restart(app):
    service, ops, messenger = app
    order = paid(service, ops)
    stage(service, ops, order)
    reopened = Database(service.db.path)
    reopened.recover()
    restored = OperatorOrders(service.settings, reopened)
    assert restored.get(order["id"], 11)["state"] == State.OPERATOR_FILE
    assert restored.approve_license(order["id"], 11)
    new_service = Service(service.settings, reopened, OperatorSupplier())
    drain(new_service, messenger)
    assert reopened.get(order["id"])["state"] == State.DELIVERED


def test_failed_notification_is_persisted_and_retried_without_confirming_payment(app):
    service, ops, messenger = app
    order = selected(service, ops)
    messenger.offline = True
    asyncio.run(service.step(messenger))
    with service.db.connect() as con:
        failed = con.execute("SELECT * FROM notification_outbox WHERE attempts=1").fetchone()
        assert failed and failed["state"] == "pending"
        con.execute("UPDATE notification_outbox SET next_attempt=0")
    messenger.offline = False
    drain(service, messenger)
    assert len(messenger.notices) == 3
    assert service.db.get(order["id"])["state"] == State.OPERATOR_PAYMENT


def test_expired_unselected_offer_can_be_refreshed_by_uploading_same_file(app):
    service, _, _ = app
    order = upload(service)
    quote = replace(Quote.loads(order["quote_json"]), expires_at=time.time() - 1)
    service.db.update(order["id"], State.QUOTED, quote_json=quote.dumps())
    refreshed = upload(service)
    assert refreshed["id"] == order["id"]
    assert Quote.loads(refreshed["quote_json"]).expires_at > time.time()


def test_old_demo_order_keeps_demo_caption_after_switch_to_operator(app):
    service, _, messenger = app
    demo_settings = replace(service.settings, mode="demo")
    demo_service = Service(demo_settings, service.db, DemoSupplier(demo_settings))
    order = upload(demo_service)
    chosen = service.db.choose(order["id"], 12, "full")
    service.db.paid(order["id"], 12, "XTR", chosen["stars"], "demo-" + order["id"])
    asyncio.run(demo_service.step(messenger))
    asyncio.run(service.step(messenger))
    assert messenger.files[0][2] is True


def test_operator_entry_point_cannot_enable_automatic_purchases(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.setenv("STORE_UPLOADS_ENABLED", "true")
    monkeypatch.setenv("STORE_PURCHASES_ENABLED", "true")
    monkeypatch.setenv("DRIVEMODS_BRIDGE_URL", "https://must-not-be-used.example")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = server_settings()
    assert settings.mode == "operator"
    assert not settings.store_uploads_enabled and not settings.store_purchases_enabled
    assert not settings.bridge_url and not settings.bridge_token


def test_cancelled_unpaid_order_can_be_replaced_without_losing_its_audit(app):
    service, ops, _ = app
    first = selected(service, ops)
    assert ops.cancel_unpaid(first["id"], 11, "Synthetic test completed")
    second = upload(service)
    assert second["id"] != first["id"]
    assert service.db.get(first["id"])["state"] == State.CANCELLED


def test_confirmed_payment_cannot_be_cancelled_as_an_unpaid_order(app):
    service, ops, _ = app
    order = paid(service, ops)
    with pytest.raises(OrderError):
        ops.cancel_unpaid(order["id"], 11, "Not allowed")


def test_a_supplier_payment_cannot_be_used_for_two_deliveries(app):
    service, ops, _ = app
    first = paid(service, ops)
    stage(service, ops, first)
    second = paid(service, ops, content=b"DIFFERENT SYNTHETIC DEVICE")
    with pytest.raises(OrderError, match="оплата"):
        stage(service, ops, second)


def test_bank_reference_case_cannot_bypass_duplicate_protection(app):
    service, ops, _ = app
    first = selected(service, ops)
    second = selected(service, ops, content=b"SECOND SYNTHETIC ID")
    for order in (first, second):
        ops.bill(order["id"], 11, "USD", 10000, "TEST RECIPIENT")
    ops.propose_payment(first["id"], 11, "MBANK", "test-1234")
    with pytest.raises(OrderError, match="операция"):
        ops.propose_payment(second["id"], 11, "MBANK", "TEST-1234")


@pytest.mark.parametrize("plan,net", [("full", 10000), ("eco", 7000)])
def test_required_net_usd_credit_is_explicit_and_preserved(app, plan, net):
    service, _, _ = app
    settings = replace(service.settings, require_usd_net_credit=True, receiving_bank="O!Bank")
    ops = OperatorOrders(settings, service.db)
    order = selected(service, ops, plan=plan)
    ops.bill(order["id"], 11, "USD", net, "TEST RECEIVER ONLY")
    with pytest.raises(OrderError, match="фактически"):
        ops.propose_payment(order["id"], 11, "OBANK", "TEST-NET-1234")
    candidate = ops.propose_payment(order["id"], 11, "OBANK", "TEST-NET-1234", net)
    assert candidate["net_usd_cents"] == net
    assert service.db.get(order["id"])["state"] == State.OPERATOR_PAYMENT
    ops.confirm_payment(order["id"], 11)
    restored = OperatorOrders(settings, Database(service.db.path))
    assert restored.get(order["id"], 11)["net_usd_cents"] == net
    stage(service, restored, service.db.get(order["id"]))
    assert restored.approve_license(order["id"], 11)


@pytest.mark.parametrize("net", [0, 1, 9000, 9999, 10001, True, 10000.0])
def test_incorrect_net_usd_credit_does_not_confirm_payment(app, net):
    service, _, _ = app
    ops = OperatorOrders(replace(service.settings, require_usd_net_credit=True), service.db)
    order = selected(service, ops)
    ops.bill(order["id"], 11, "KGS", 875000, "SYNTHETIC RATE EXAMPLE ONLY")
    with pytest.raises(OrderError, match="Чистое зачисление"):
        ops.propose_payment(order["id"], 11, "OBANK", "TEST-1234", net)
    row = ops.get(order["id"], 11)
    assert row["state"] == State.OPERATOR_PAYMENT and row["bank_reference"] is None


def test_old_pending_confirmation_must_be_reentered_with_net_credit(app):
    service, ops, _ = app
    order = selected(service, ops)
    ops.bill(order["id"], 11, "USD", 10000, "TEST RECEIVER")
    ops.propose_payment(order["id"], 11, "OBANK", "TEST-OLD-1234")
    strict = OperatorOrders(replace(service.settings, require_usd_net_credit=True), service.db)
    with pytest.raises(OrderError, match="фактически"):
        strict.confirm_payment(order["id"], 11)
    strict.confirm_payment(order["id"], 11, cancel=True)
    strict.propose_payment(order["id"], 11, "OBANK", "TEST-OLD-1234", 10000)
    assert strict.confirm_payment(order["id"], 11)


@pytest.mark.parametrize("cancel", [False, True])
def test_old_payment_buttons_cannot_confirm_or_cancel_a_replacement(app, cancel):
    service, ops, _ = app
    order = selected(service, ops)
    ops.bill(order["id"], 11, "USD", 10000, "TEST RECEIVER")
    first = ops.propose_payment(order["id"], 11, "OBANK", "TEST-FIRST", 10000)
    old_token = review_token(first, "payment")
    ops.confirm_payment(order["id"], 11, cancel=True, expected_token=old_token)
    second = ops.propose_payment(order["id"], 11, "OBANK", "TEST-SECOND", 10000)
    with pytest.raises(OrderError, match="другому подтверждению"):
        ops.confirm_payment(order["id"], 11, cancel=cancel, expected_token=old_token)
    assert ops.get(order["id"], 11)["bank_reference"] == "OBANK:TEST-SECOND"
    assert ops.confirm_payment(order["id"], 11, expected_token=review_token(second, "payment"))


@pytest.mark.parametrize("cancel", [False, True])
def test_old_license_buttons_cannot_approve_or_drop_a_replacement(app, cancel):
    service, ops, _ = app
    order = paid(service, ops)
    first = stage(service, ops, order)
    old_token = review_token(first, "license")
    ops.approve_license(order["id"], 11, cancel=True, expected_token=old_token)
    second = stage(service, ops, order, content=b"\x00REPLACEMENT SYNTHETIC TEST LICENSE\x00")
    with pytest.raises(OrderError, match="другому файлу"):
        ops.approve_license(order["id"], 11, cancel=cancel, expected_token=old_token)
    assert service.db.get(order["id"])["state"] == State.OPERATOR_FILE
    assert ops.approve_license(order["id"], 11, expected_token=review_token(second, "license"))


def test_delivery_can_resume_after_telegram_rejection_without_repurchase(app, monkeypatch):
    service, ops, messenger = app
    order = paid(service, ops)
    stage(service, ops, order)
    ops.approve_license(order["id"], 11)
    async def rejected(*args):
        raise DeliveryRejected("test customer blocked bot")
    original = messenger.delivered
    monkeypatch.setattr(messenger, "delivered", rejected)
    drain(service, messenger)
    assert service.db.get(order["id"])["state"] == State.REVIEW
    with pytest.raises(OrderError, match="оператору"):
        ops.retry_delivery(order["id"], 12)
    reopened = Database(service.db.path)
    reopened.recover()
    restored = OperatorOrders(service.settings, reopened)
    assert restored.retry_delivery(order["id"], 11)
    assert restored.retry_delivery(order["id"], 11) is False
    monkeypatch.setattr(messenger, "delivered", original)
    new_service = Service(service.settings, reopened, OperatorSupplier())
    drain(new_service, messenger)
    assert reopened.get(order["id"])["state"] == State.DELIVERED
    assert len(messenger.files) == 1


@pytest.mark.parametrize("target", ["source_path", "output_path"])
@pytest.mark.parametrize("missing", [False, True])
def test_delivery_recovery_refuses_missing_or_changed_files(app, target, missing):
    service, ops, _ = app
    order = paid(service, ops)
    prepared = stage(service, ops, order)
    ops.approve_license(order["id"], 11)
    service.db.update(order["id"], State.REVIEW)
    path = Path(prepared[target])
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"TAMPERED")
    with pytest.raises(OrderError, match="недоступен|изменился"):
        ops.retry_delivery(order["id"], 11)
    assert service.db.get(order["id"])["state"] == State.REVIEW

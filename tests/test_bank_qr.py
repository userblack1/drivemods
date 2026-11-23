import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest

from drivemods_bot.bank_qr import MBankQr, parse_fields
from drivemods_bot.config import Settings
from drivemods_bot.models import OrderError, State
from drivemods_bot.operator_orders import OperatorOrders
from drivemods_bot.service import Service
from drivemods_bot.storage import Database
from drivemods_bot.supplier import OperatorSupplier


def test_original_qr_is_kgs_without_amount_and_percent_decoding_precedes_tlv(bank_qr_source):
    qr = MBankQr.read(bank_qr_source)
    assert qr.amount_minor is None
    assert qr.recipient == "TEST QR"
    assert MBankQr.parse(qr.url()).fields == qr.fields


@pytest.mark.parametrize("amount", [1, 700000, 875000, 9_999_999_999_999])
def test_prefilled_qr_keeps_recipient_and_currency_and_uses_tyiyn(bank_qr_source, amount):
    original = MBankQr.read(bank_qr_source)
    paid = original.with_amount(amount)
    before, after = dict(original.fields), dict(paid.fields)
    assert after["54"] == str(amount) and paid.amount_minor == amount
    assert before["53"] == after["53"] == "417"
    assert before["01"] == after["01"] == "11"
    old_route, new_route = dict(parse_fields(before["32"])), dict(parse_fields(after["32"]))
    for field in ("00", "01", "10", "13"):
        assert old_route[field] == new_route[field]
    assert new_route["12"] == "12"
    assert "11" not in new_route  # No fabricated bank transaction/invoice ID.
    raw = unquote(urlsplit(paid.url()).fragment)
    assert hashlib.sha256(raw[:-8].encode()).hexdigest()[-4:].upper() == raw[-4:]


@pytest.mark.parametrize("amount", [0, -1, True, 100.0, "100", 10**13])
def test_bad_amount_never_produces_a_payment_qr(bank_qr_source, amount):
    with pytest.raises(ValueError):
        MBankQr.read(bank_qr_source).with_amount(amount)


@pytest.mark.parametrize("replacement", ["http://pay.payqr.kg", "https://evil.example", "https://pay.payqr.kg.evil.example", "https://user@pay.payqr.kg", "https://pay.payqr.kg:443"])
def test_untrusted_payment_hosts_are_rejected(bank_qr_source, replacement):
    payload = bank_qr_source.read_text().replace("https://pay.payqr.kg", replacement)
    with pytest.raises(ValueError):
        MBankQr.parse(payload)


def test_checksum_tampering_is_rejected_before_amount_insertion(bank_qr_source):
    payload = bank_qr_source.read_text().replace("996000000000", "996000000001")
    with pytest.raises(ValueError, match="Контрольная сумма"):
        MBankQr.parse(payload)


def test_real_png_round_trips_through_an_independent_decoder(bank_qr_source, tmp_path):
    decoder = pytest.importorskip("zxingcpp")
    pillow = pytest.importorskip("PIL.Image")
    qr = MBankQr.read(bank_qr_source).with_amount(875000)
    target = tmp_path / "qr.png"
    qr.write_png(target)
    result = decoder.read_barcodes(pillow.open(target))
    assert len(result) == 1 and result[0].text == qr.url()


class Capture:
    def __init__(self):
        self.qrs, self.notices = [], []
        self.offline = False

    async def notified(self, who, body):
        self.notices.append((who, body))

    async def payment_qr(self, who, body, path, url):
        if self.offline:
            raise OSError("TEST OFFLINE")
        self.qrs.append((who, body, Path(path).read_bytes(), url))

    async def notify_admins(self, body):
        self.notices.append((11, body))

    async def delivered(self, *args):
        pytest.fail("A QR is not proof of payment and must never deliver a license")


def app(tmp_path, source, rate="87.50"):
    settings = Settings(mode="operator", data_dir=tmp_path / "data", admin_ids=frozenset({11}), support="@owner",
                        receiving_bank="MBANK", require_usd_net_credit=True, mbank_qr_enabled=True,
                        mbank_qr_source=source, mbank_qr_kgs_per_usd=rate)
    db = Database(settings.data_dir / "orders.sqlite3")
    return Service(settings, db, OperatorSupplier()), OperatorOrders(settings, db)


def select(service, ops, plan="full"):
    order_id, source = service.allocate()
    source.write_bytes(b"SYNTHETIC ID - DO NOT USE IN STORE " + order_id.encode())
    asyncio.run(service.upload(order_id, 12, 12, "device.id.bin", source))
    assert ops.select(order_id, 12, plan)
    return ops.get(order_id, 11)


def drain(service, messenger):
    for _ in range(20):
        if not asyncio.run(service.step(messenger)):
            return
    pytest.fail("QR outbox did not settle")


@pytest.mark.parametrize("plan,retail,kgs", [("full", 10000, 875000), ("eco", 7000, 612500)])
def test_package_selection_queues_one_fixed_qr_without_approving_payment(tmp_path, bank_qr_source, plan, retail, kgs):
    service, ops = app(tmp_path, bank_qr_source)
    order = select(service, ops, plan)
    assert order["retail_cents"] == retail
    assert order["currency"] == "KGS" and order["amount_minor"] == kgs
    assert order["state"] == State.OPERATOR_PAYMENT and order["bank_reference"] is None
    assert MBankQr.parse(order["qr_payload"]).amount_minor == kgs
    assert ops.select(order["id"], 12, plan) is False
    messenger = Capture()
    drain(service, messenger)
    assert len(messenger.qrs) == 1 and messenger.qrs[0][0] == 12
    assert "Зачисление пока проверяет оператор" in messenger.qrs[0][1]
    assert "Закупка" not in messenger.qrs[0][1]
    assert service.db.get(order["id"])["state"] == State.OPERATOR_PAYMENT
    with pytest.raises(OrderError):
        ops.propose_payment(order["id"], 11, "MBANK", "TEST-MISSING-NET")
    with pytest.raises(OrderError, match="входящую операцию в MBANK"):
        ops.propose_payment(order["id"], 11, "OBANK", "TEST-WRONG-BANK", retail)


def test_issued_amount_and_receiver_survive_rate_change_and_restart(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source)
    first = select(service, ops)
    # Future configuration changes do not change an already-issued QR.
    changed = replace(service.settings, mbank_qr_kgs_per_usd="90.00")
    reopened = Database(service.db.path)
    reopened.recover()
    restored = OperatorOrders(changed, reopened)
    assert restored.select(first["id"], 12, "full") is False
    same = restored.customer_qr(first["id"], 12)
    assert same["qr_payload"] == first["qr_payload"] and same["amount_minor"] == 875000
    with pytest.raises(OrderError, match="не ваш"):
        restored.customer_qr(first["id"], 99)
    with pytest.raises(OrderError, match="Нельзя молча"):
        restored.bill(first["id"], 11, "KGS", 900000, "TEST OTHER RECIPIENT")


def test_missing_rate_rolls_back_selection_without_sending_wrong_amount(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source, rate=None)
    with pytest.raises(OrderError, match="Сумма не выставлена"):
        select(service, ops)
    assert service.db.recent()[0]["state"] == State.QUOTED
    with service.db.connect() as con:
        assert con.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0
    assert any("согласованный курс" in problem for problem in service.settings.problems())


def test_qr_delivery_failure_is_retried_after_restart_and_never_marks_paid(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source)
    order = select(service, ops)
    messenger = Capture()
    messenger.offline = True
    drain(service, messenger)
    assert not messenger.qrs
    with service.db.connect() as con:
        pending = con.execute("SELECT * FROM notification_outbox WHERE attachment_path IS NOT NULL").fetchone()
        assert pending["state"] == "pending" and pending["attempts"] == 1
        con.execute("UPDATE notification_outbox SET next_attempt=0")
    reopened = Database(service.db.path)
    reopened.recover()
    messenger.offline = False
    drain(Service(service.settings, reopened, OperatorSupplier()), messenger)
    assert len(messenger.qrs) == 1
    assert reopened.get(order["id"])["state"] == State.OPERATOR_PAYMENT


def test_cancelled_order_does_not_send_a_pending_qr(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source)
    order = select(service, ops)
    ops.cancel_unpaid(order["id"], 11, "TEST CANCEL")
    messenger = Capture()
    drain(service, messenger)
    assert not messenger.qrs
    with pytest.raises(OrderError):
        ops.customer_qr(order["id"], 12)


def test_already_verified_order_does_not_receive_an_unsent_payment_qr(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source)
    order = select(service, ops)
    ops.propose_payment(order["id"], 11, "MBANK", "TEST-CREDIT-1234", received_amount_minor=order["amount_minor"], received_currency="KGS")
    ops.confirm_payment(order["id"], 11)
    messenger = Capture()
    drain(service, messenger)
    assert not messenger.qrs
    with service.db.connect() as con:
        assert con.execute("SELECT state FROM notification_outbox WHERE attachment_path IS NOT NULL").fetchone()[0] == "skipped"


def test_changed_qr_file_is_not_sent_or_reissued(tmp_path, bank_qr_source):
    service, ops = app(tmp_path, bank_qr_source)
    order = select(service, ops)
    Path(order["qr_path"]).write_bytes(b"TAMPERED FILE")
    messenger = Capture()
    drain(service, messenger)
    assert not messenger.qrs
    with pytest.raises(OrderError, match="QR изменился"):
        ops.customer_qr(order["id"], 12)


@pytest.mark.parametrize("plan,expected", [("full", 900000), ("eco", 650000)])
def test_owners_fixed_kgs_prices_are_independent_of_one_exchange_rate(tmp_path, bank_qr_source, plan, expected):
    service, _ = app(tmp_path, bank_qr_source, rate=None)
    settings = replace(service.settings, mbank_qr_full_kgs_minor=900000, mbank_qr_eco_kgs_minor=650000)
    ops = OperatorOrders(settings, service.db)
    order = select(service, ops, plan)
    assert order["amount_minor"] == expected and order["qr_rate"] is None
    assert MBankQr.parse(order["qr_payload"]).amount_minor == expected
    assert "Курс:" not in ops.qr_caption(order)
    assert not any(problem.startswith("QR MBANK") for problem in settings.problems())
    ops.propose_payment(order["id"], 11, "MBANK", "TEST-FIXED-1234", received_amount_minor=expected, received_currency="KGS")
    ops.confirm_payment(order["id"], 11)
    stored = ops.get(order["id"], 11)
    assert stored["received_currency"] == "KGS" and stored["received_amount_minor"] == expected
    assert stored["net_usd_cents"] is None and stored["state"] == State.OPERATOR_PAID


@pytest.mark.parametrize("amount,currency", [(10000, "USD"), (900000, "USD"), (899999, "KGS"), (900001, "KGS"), (True, "KGS"), (900000.0, "KGS")])
def test_fixed_qr_payment_rejects_wrong_sum_or_currency(tmp_path, bank_qr_source, amount, currency):
    service, _ = app(tmp_path, bank_qr_source, rate=None)
    ops = OperatorOrders(replace(service.settings, mbank_qr_full_kgs_minor=900000, mbank_qr_eco_kgs_minor=650000), service.db)
    order = select(service, ops)
    with pytest.raises(OrderError, match="ровно 9000.00 KGS"):
        ops.propose_payment(order["id"], 11, "MBANK", "TEST-WRONG-1234", received_amount_minor=amount, received_currency=currency)
    assert ops.get(order["id"], 11)["bank_reference"] is None


@pytest.mark.parametrize("full,eco,rate", [(900000, None, None), (900000, 650000, "89"), (0, 650000, None)])
def test_partial_or_conflicting_fixed_price_configuration_blocks_qr(tmp_path, bank_qr_source, full, eco, rate):
    service, _ = app(tmp_path, bank_qr_source, rate=rate)
    settings = replace(service.settings, mbank_qr_full_kgs_minor=full, mbank_qr_eco_kgs_minor=eco)
    assert any(problem.startswith("QR MBANK") for problem in settings.problems())
    with pytest.raises(OrderError):
        select(service, OperatorOrders(settings, service.db))


def test_fixed_kgs_payment_can_fulfill_only_after_both_operator_confirmations(tmp_path, bank_qr_source):
    service, _ = app(tmp_path, bank_qr_source, rate=None)
    settings = replace(service.settings, mbank_qr_full_kgs_minor=900000, mbank_qr_eco_kgs_minor=650000)
    ops = OperatorOrders(settings, service.db)
    order = select(service, ops)
    output = Path(order["source_path"]).parent / "test-license.bin"
    output.write_bytes(b"\x00TEST ONLY - SYNTHETIC OUTPUT - NOT A LICENSE\x00")
    with pytest.raises(OrderError):
        ops.stage_license(order["id"], 11, "TEST-LICENSE", "DM-TEST123", output)
    ops.propose_payment(order["id"], 11, "MBANK", "TEST-FULL-1234", received_amount_minor=900000, received_currency="KGS")
    assert service.db.get(order["id"])["state"] == State.OPERATOR_PAYMENT
    ops.confirm_payment(order["id"], 11)
    ops.stage_license(order["id"], 11, "TEST-LICENSE", "DM-TEST123", output)
    messenger = Capture()
    drain(service, messenger)
    assert service.db.get(order["id"])["state"] == State.OPERATOR_FILE
    ops.approve_license(order["id"], 11)
    files = []
    async def capture_file(order, path, demo):
        files.append(Path(path).read_bytes())
        return "TEST-DELIVERED"
    messenger.delivered = capture_file
    drain(service, messenger)
    assert len(files) == 1 and service.db.get(order["id"])["state"] == State.DELIVERED

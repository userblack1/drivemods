import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import time

import httpx
import pytest

from drivemods_bot.config import Settings, cents
from drivemods_bot.locking import single_instance
from drivemods_bot.models import OrderError, Quote, State, SupplierOrder
from drivemods_bot.service import Service
from drivemods_bot.storage import Database
from drivemods_bot.supplier import BridgeSupplier, DemoSupplier, validate_license


class CaptureMessenger:
    def __init__(self):
        self.files = []
        self.notifications = []
        self.fail_delivery = False

    async def delivered(self, order, path, demo):
        if self.fail_delivery:
            raise OSError("offline")
        self.files.append((order["id"], path.read_bytes()))
        return "telegram-file-id"

    async def notify_admins(self, text):
        self.notifications.append(text)


class FakeSupplier(DemoSupplier):
    def __init__(self, settings):
        super().__init__(settings)
        self.buys = 0
        self.quotes = 0
        self.quote_uncertain = False
        self.price_increased = False
        self.initial = "ready"
        self.current = "ready"
        self.uncertain = False
        self.bad_download = False
        self.downloads = 0
        self.ref = None

    async def quote(self, source, key):
        self.quotes += 1
        if self.quote_uncertain:
            # The upload may already have created a non-removable store record.
            self.ref = "store-" + key
            raise httpx.ReadTimeout("upload result unknown")
        quote = await super().quote(source, key)
        if self.price_increased:
            return replace(quote, offers=tuple(replace(o, cost_cents=o.cost_cents + 500) for o in quote.offers))
        return quote

    async def buy(self, order):
        self.buys += 1
        self.ref = "store-" + order["id"]
        if self.uncertain:
            raise httpx.ReadTimeout("the bank may already have charged")
        return SupplierOrder(self.ref, self.initial, "https://bank.example/3ds" if self.initial == "action_required" else None)

    async def status(self, reference):
        assert reference == self.ref
        return SupplierOrder(reference, self.current)

    async def reconcile(self, key):
        return SupplierOrder(self.ref, self.current) if self.ref else None

    async def download(self, reference):
        self.downloads += 1
        return b"<html>Sign in</html>" if self.bad_download else b"\x00NOT A REAL LICENSE - UNIT TEST ONLY\x00"


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_ids=frozenset({11}), full_stars=10000, eco_stars=7000)
    db = Database(tmp_path / "orders.sqlite3")
    supplier = FakeSupplier(settings)
    service = Service(settings, db, supplier)
    return settings, db, supplier, service, CaptureMessenger()


def upload(service, contents=b"example device ID", user_id=11):
    order_id, path = service.allocate()
    path.write_bytes(contents)
    return asyncio.run(service.upload(order_id, user_id, user_id, "device.id.bin", path))[0]


def pay(db, order, plan="full", charge="charge-1"):
    chosen = db.choose(order["id"], order["user_id"], plan)
    db.precheckout(order["id"], order["user_id"], "XTR", chosen["stars"], "checkout-1")
    db.paid(order["id"], order["user_id"], "XTR", chosen["stars"], charge)
    return db.get(order["id"])


def test_prices_include_ten_dollar_markup(setup):
    _, _, _, service, _ = setup
    quote = Quote.loads(upload(service)["quote_json"])
    assert {o.plan: o.retail_cents for o in quote.offers} == {"full": 10000, "eco": 7000}
    assert {o.plan: o.cost_cents for o in quote.offers} == {"full": 9000, "eco": 6000}


def test_successful_full_flow_and_replayed_payment_buy_only_once(setup):
    _, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    assert db.paid(order["id"], 11, "XTR", 10000, "charge-1") is False
    for _ in range(5):
        asyncio.run(service.step(messenger))
    final = db.get(order["id"])
    assert final["state"] == State.DELIVERED
    assert supplier.buys == 1
    assert supplier.quotes == 1
    assert len(messenger.files) == 1
    assert final["telegram_file_id"] == "telegram-file-id"


def test_no_supplier_purchase_before_successful_payment(setup):
    _, db, supplier, service, messenger = setup
    order = upload(service)
    chosen = db.choose(order["id"], 11, "full")
    db.precheckout(order["id"], 11, "XTR", chosen["stars"], "checkout")
    assert asyncio.run(service.step(messenger)) is False
    assert supplier.buys == 0
    assert supplier.quotes == 0


@pytest.mark.parametrize("user,currency,amount", [(12, "XTR", 10000), (11, "USD", 10000), (11, "XTR", 1), (11, "XTR", 0)])
def test_wrong_payer_currency_or_amount_cannot_pay(setup, user, currency, amount):
    _, db, supplier, service, messenger = setup
    order = upload(service)
    db.choose(order["id"], 11, "full")
    with pytest.raises(OrderError):
        db.precheckout(order["id"], user, currency, amount, "checkout")
    with pytest.raises(OrderError):
        db.paid(order["id"], user, currency, amount, "forged")
    asyncio.run(service.step(messenger))
    assert supplier.buys == 0


def test_order_and_package_cannot_be_selected_by_another_user(setup):
    _, db, _, service, _ = setup
    order = upload(service)
    with pytest.raises(OrderError):
        db.get(order["id"], 12)
    with pytest.raises(OrderError):
        db.choose(order["id"], 12, "full")
    db.choose(order["id"], 11, "full")
    with pytest.raises(OrderError):
        db.choose(order["id"], 11, "eco")


def test_duplicate_upload_returns_existing_order(setup):
    _, db, _, service, _ = setup
    first, second = upload(service), upload(service)
    assert first["id"] == second["id"]
    assert len(db.recent(11)) == 1


def test_checkout_reservation_blocks_a_different_payment_attempt(setup):
    _, db, _, service, _ = setup
    order = upload(service)
    db.choose(order["id"], 11, "full")
    db.precheckout(order["id"], 11, "XTR", 10000, "first")
    db.precheckout(order["id"], 11, "XTR", 10000, "first")
    with pytest.raises(OrderError):
        db.precheckout(order["id"], 11, "XTR", 10000, "different")


def test_second_charge_cannot_repurchase_same_order(setup):
    _, db, _, service, _ = setup
    order = pay(db, upload(service))
    with pytest.raises(OrderError):
        db.paid(order["id"], 11, "XTR", 10000, "charge-2")


def test_charge_cannot_be_reused_for_a_different_order(setup):
    _, db, _, service, _ = setup
    pay(db, upload(service))
    order = upload(service, b"a different device")
    db.choose(order["id"], 11, "full")
    with pytest.raises(OrderError):
        db.paid(order["id"], 11, "XTR", 10000, "charge-1")


def test_concurrent_workers_only_claim_once(setup):
    _, db, _, service, _ = setup
    pay(db, upload(service))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: db.claim(State.PAID, State.BUYING), range(4)))
    assert sum(result is not None for result in results) == 1


def test_unknown_purchase_result_requires_reconciliation_without_rebuy(setup):
    _, db, supplier, service, messenger = setup
    supplier.uncertain = True
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    for _ in range(3):
        asyncio.run(service.step(messenger))
    assert supplier.buys == 1
    asyncio.run(service.reconcile(order["id"], messenger))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.DELIVERED
    assert supplier.buys == 1


def test_visa_action_waits_for_supplier_confirmation(setup):
    _, db, supplier, service, messenger = setup
    supplier.initial = "action_required"
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    waiting = db.get(order["id"])
    assert waiting["state"] == State.WAITING
    assert waiting["supplier_ref"]
    assert supplier.downloads == 0 and not messenger.files
    assert len(messenger.notifications) == 1
    db.update(order["id"], State.WAITING, next_attempt=0)
    asyncio.run(service.step(messenger))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.DELIVERED
    assert supplier.buys == 1


def test_failed_download_retains_reference_and_does_not_rebuy(setup):
    _, db, supplier, service, messenger = setup
    supplier.bad_download = True
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["supplier_ref"]
    assert db.get(order["id"])["state"] == State.WAITING
    assert not messenger.files
    supplier.bad_download = False
    db.update(order["id"], State.WAITING, next_attempt=0)
    asyncio.run(service.step(messenger))
    asyncio.run(service.step(messenger))
    assert supplier.buys == 1 and len(messenger.files) == 1


def test_delivery_retry_never_buys_again(setup):
    _, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    messenger.fail_delivery = True
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.READY
    messenger.fail_delivery = False
    db.update(order["id"], State.READY, next_attempt=0)
    asyncio.run(service.step(messenger))
    assert supplier.buys == 1 and len(messenger.files) == 1


def test_restart_during_purchase_blocks_repurchase(setup):
    settings, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    db.claim(State.PAID, State.BUYING)
    reopened = Database(settings.data_dir / "orders.sqlite3")
    reopened.recover()
    assert reopened.get(order["id"])["state"] == State.REVIEW
    asyncio.run(service.step(messenger))
    assert supplier.buys == 0


def test_restart_after_payment_continues_without_new_payment(setup):
    settings, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    reopened = Database(settings.data_dir / "orders.sqlite3")
    reopened.recover()
    new_service = Service(settings, reopened, supplier)
    asyncio.run(new_service.step(messenger))
    asyncio.run(new_service.step(messenger))
    assert reopened.get(order["id"])["state"] == State.DELIVERED


@pytest.mark.parametrize("content", [b"", b"<html>login</html>", b'{"error":"failed"}', b"same input"])
def test_invalid_output_is_not_delivered(setup, content):
    settings, *_ = setup
    with pytest.raises(ValueError):
        validate_license(content, hashlib.sha256(b"same input").hexdigest(), settings)


def test_expired_quote_rejected_before_payment(setup):
    _, db, _, service, _ = setup
    order = upload(service)
    original = Quote.loads(order["quote_json"])
    expired = replace(original, expires_at=time.time() - 1)
    db.update(order["id"], State.QUOTED, quote_json=expired.dumps())
    with pytest.raises(OrderError):
        db.choose(order["id"], 11, "full")


def test_supplier_price_drift_blocks_customer_quote(setup):
    settings, *_ = setup
    async def scenario():
        client = httpx.AsyncClient(base_url="https://bridge.example/", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "reference": "q", "expires_at": time.time() + 100,
            "offers": [{"plan": "full", "supplier_plan_id": "p", "cost_usd_cents": 9500}],
        })))
        supplier = BridgeSupplier(replace(settings, bridge_url="https://bridge.example", bridge_verified=True, store_uploads_enabled=True), client)
        path = settings.data_dir / "input.bin"
        path.write_bytes(b"sample")
        try:
            with pytest.raises(OrderError, match="цена"):
                await supplier.quote(path, "key")
        finally:
            await supplier.close()
    asyncio.run(scenario())


def test_live_configuration_cannot_start_with_missing_integrations(tmp_path):
    settings = Settings(mode="live", data_dir=tmp_path)
    problems = "\n".join(settings.problems())
    assert "BOT_TOKEN" in problems and "DriveMods" in problems and "Stars" in problems


@pytest.mark.parametrize("value", ["-1", "0.001", "NaN", "Infinity"])
def test_invalid_money_rejected(value):
    with pytest.raises(ValueError):
        cents(value)


def test_duplicate_process_is_blocked(tmp_path):
    with single_instance(tmp_path / "bot.lock"):
        with pytest.raises(RuntimeError):
            with single_instance(tmp_path / "bot.lock"):
                pass


def test_receiving_device_never_calls_supplier_even_when_supplier_is_offline(setup):
    _, _, supplier, service, _ = setup
    supplier.quote_uncertain = True
    order = upload(service)
    assert order["state"] == State.QUOTED
    assert supplier.quotes == 0 and supplier.buys == 0


def test_uncertain_device_upload_blocks_repeat_upload_and_purchase(setup):
    _, db, supplier, service, messenger = setup
    supplier.quote_uncertain = True
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    for _ in range(3):
        asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    assert supplier.quotes == 1 and supplier.buys == 0
    # Reconciliation only reads the existing supplier order; no upload repeats.
    asyncio.run(service.reconcile(order["id"], messenger))
    assert supplier.quotes == 1 and supplier.buys == 0


def test_changed_source_is_never_uploaded_after_payment(setup):
    _, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    Path(order["source_path"]).write_bytes(b"different device")
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    assert supplier.quotes == 0 and supplier.buys == 0


def test_price_drift_after_payment_blocks_visa_purchase(setup):
    _, db, supplier, service, messenger = setup
    supplier.price_increased = True
    order = pay(db, upload(service))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    assert supplier.quotes == 1 and supplier.buys == 0


def test_live_worker_leaves_paid_order_queued_when_store_operations_are_closed(setup):
    settings, db, supplier, service, messenger = setup
    order = pay(db, upload(service))
    service.settings = replace(settings, mode="live")
    assert asyncio.run(service.step(messenger)) is False
    assert db.get(order["id"])["state"] == State.PAID
    assert supplier.quotes == 0 and supplier.buys == 0


def test_bridge_upload_lock_prevents_even_a_network_request(setup):
    settings, *_ = setup
    def unexpected_request(request):
        pytest.fail("The disabled bridge must not send requests")
    async def scenario():
        client = httpx.AsyncClient(base_url="https://bridge.example/", transport=httpx.MockTransport(unexpected_request))
        supplier = BridgeSupplier(replace(settings, bridge_url="https://bridge.example", bridge_verified=True), client)
        try:
            with pytest.raises(OrderError, match="отключена"):
                await supplier.quote(settings.data_dir / "does-not-exist.bin", "key")
            with pytest.raises(OrderError, match="отключены"):
                await supplier.buy({})
        finally:
            await supplier.close()
    asyncio.run(scenario())


def test_eco_order_preserves_sixty_dollar_purchase_budget(setup):
    _, db, supplier, service, messenger = setup
    order = pay(db, upload(service), plan="eco")
    assert order["cost_cents"] == 6000 and order["retail_cents"] == 7000
    asyncio.run(service.step(messenger))
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.DELIVERED
    assert supplier.buys == 1 and supplier.quotes == 1


@pytest.mark.parametrize("plan", ["full", "eco"])
def test_live_purchase_and_delivery_require_a_matching_provider_receipt(setup, plan):
    settings, db, supplier, service, messenger = setup
    initial = upload(service)
    chosen = db.choose(initial["id"], 11, plan)
    payload = f"dm:{initial['id']}:{plan}"
    db.record_receipt("real-charge", 11, payload, "XTR", chosen["stars"])
    db.paid(initial["id"], 11, "XTR", chosen["stars"], "real-charge")
    db.resolve_receipt("real-charge", "matched")
    service.settings = replace(settings, mode="live", store_uploads_enabled=True, store_purchases_enabled=True)
    for _ in range(4):
        asyncio.run(service.step(messenger))
    assert db.get(initial["id"])["state"] == State.DELIVERED
    assert supplier.quotes == supplier.buys == len(messenger.files) == 1


@pytest.mark.parametrize("receipt", ["missing", "demo", "unmatched", "wrong_payer", "wrong_plan", "wrong_amount", "wrong_currency", "refunded"])
def test_live_purchase_rejects_unverified_payment_before_any_upload(setup, receipt):
    settings, db, supplier, service, messenger = setup
    order = pay(db, upload(service), charge="demo-test" if receipt == "demo" else "charge")
    if receipt != "missing":
        db.record_receipt(order["charge_id"], 12 if receipt == "wrong_payer" else 11,
                          f"dm:{order['id']}:{'eco' if receipt == 'wrong_plan' else 'full'}",
                          "USD" if receipt == "wrong_currency" else "XTR",
                          1 if receipt == "wrong_amount" else 10000)
        db.resolve_receipt(order["charge_id"], receipt if receipt in {"unmatched", "refunded"} else "matched")
    service.settings = replace(settings, mode="live", store_uploads_enabled=True, store_purchases_enabled=True)
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    assert supplier.quotes == supplier.buys == supplier.downloads == 0
    assert not messenger.files


@pytest.mark.parametrize("plan,limit", [("full", 9000), ("eco", 6000)])
def test_order_over_owner_budget_is_blocked_even_if_tariff_configuration_was_raised(setup, plan, limit):
    settings, db, supplier, service, messenger = setup
    order = pay(db, upload(service), plan=plan)
    db.update(order["id"], State.PAID, cost_cents=limit + 1)
    service.settings = replace(settings, **{plan + "_cost": limit + 1})
    asyncio.run(service.step(messenger))
    assert db.get(order["id"])["state"] == State.REVIEW
    assert supplier.quotes == supplier.buys == 0
    live = replace(service.settings, mode="live")
    assert any("предел" in problem for problem in live.problems())


@pytest.mark.parametrize("reason", ["over_budget", "demo", "expired", "price_changed"])
def test_bridge_never_sends_purchase_for_an_unauthorized_or_stale_order(setup, reason):
    settings, db, _, service, _ = setup
    order = pay(db, upload(service), charge="demo-test" if reason == "demo" else "charge")
    quote = Quote.loads(order["quote_json"])
    offer = next(o for o in quote.offers if o.plan == "full")
    cost = 9001 if reason == "over_budget" else 9000
    offer = replace(offer, cost_cents=cost + (1 if reason == "price_changed" else 0), supplier_plan_id="actual-plan")
    quote = replace(quote, reference="store-quote", offers=(offer,),
                    expires_at=time.time() - 1 if reason == "expired" else time.time() + 100)
    order = {**order, "quote_json": quote.dumps(), "cost_cents": cost}
    def unexpected_request(request):
        pytest.fail("An unauthorized purchase must not contact the store")
    async def scenario():
        client = httpx.AsyncClient(base_url="https://bridge.example/", transport=httpx.MockTransport(unexpected_request))
        supplier = BridgeSupplier(replace(settings, bridge_url="https://bridge.example", bridge_verified=True,
                                          store_purchases_enabled=True, full_cost=cost), client)
        try:
            with pytest.raises(OrderError):
                await supplier.buy(order)
        finally:
            await supplier.close()
    asyncio.run(scenario())



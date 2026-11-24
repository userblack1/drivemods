"""Actual adapter/Parse serialization against mock transport, never the shop."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from crypto_checkout.binance import DepositCredit
from crypto_checkout.demo import TEST_ADDRESS, synthetic_receipt
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger
from crypto_checkout.drivemods import LicenseExpectation
from crypto_checkout.store_adapter import (
    CreationReplyContract, DriveModsPurchaseClient, DriveModsStoreAdapter,
    PurchaseReviewRequired, StoreJournal,
)
from crypto_checkout.tron import parse_solid_receipt


OWNER = "Owner00001"
OUTPUT = b"\x00\x01signed-output-fixture-never-a-real-license"


class PublicContractShop:
    def __init__(self):
        self.rows, self.calls, self.creates = {}, [], {"license": 0, "payment": 0, "checkout": 0}
        self.loss = None
        self.price = 90
        self.recoverable = False
        self.checkout_fields = True
        self.extra_prices = []
        self.before_create = None

    def owner(self):
        return {"__type": "Pointer", "className": "_User", "objectId": OWNER}

    def common(self, record_id, body, record_type):
        return {"__type": "Object", "className": "Record", "objectId": record_id,
                "createdBy": self.owner(), "type": record_type, "isActive": True,
                "currency": 2, "priceTotal": body.get("priceTotal", body.get("sum")),
                "paymentStatus": 1, "dealerComment": body["dealerComment"],
                "createdAt": {"__type": "Date", "iso": "2026-11-24T00:00:00.000Z"}}

    def __call__(self, request):
        body = json.loads(request.content)
        name = request.url.path.rsplit("/", 1)[1]
        self.calls.append((name, deepcopy(body)))
        assert request.url.host == "store.drivemods.org" and request.method == "POST"
        assert body["_ApplicationId"] == "dmStoreApp"
        if name == "login":
            return httpx.Response(200, json={"objectId": OWNER, "username": "fixture-user", "sessionToken": "fixture-session"})
        assert body["_SessionToken"] == "fixture-session"
        result = None
        if name == "licenseGetInfo":
            assert body["uid"] == "fixture-user"
            assert base64.urlsafe_b64decode(body["did"]).startswith(b"fixture-device-id-")
            result = {"info": {"product": "IG19.KOR.SOP", "device_id": "device-123",
                                "version_software": "V102", "version_custom": None,
                                "bundles": ["FULL", "ECO"], "regions": [],
                                "recoverable": self.recoverable, "outdated": False}}
        elif name == "getUserPriceGroup":
            result = {"currency": 2}
        elif name == "getUserPriceItems":
            result = [{"isActive": True, "price": self.price,
                       "product": {"__type": "Object", "className": "Product", "objectId": "Product001",
                                   "name": "IG19.KOR.SOP", "spec": "FULL", "region": None}},
                      {"isActive": True, "price": 60,
                       "product": {"name": "IG19.KOR.SOP", "spec": "ECO", "region": None}}]
            result.extend(self.extra_prices)
        elif name == "processQuery":
            if body["type"] == "GET_ONE":
                result = {"data": self.rows[body["params"]["id"]]}
            else:
                assert body["type"] == "GET_LIST"
                filters = body["params"]["filter"]
                assert filters["createdBy"] == OWNER
                rows = [r for r in self.rows.values() if r["type"] == filters["type_num"]]
                result = {"data": rows, "total": len(rows)}
        elif name == "createNewLicense":
            if self.before_create is not None:
                self.before_create(body)
            self.creates["license"] += 1
            if self.loss == "before_license":
                raise httpx.ReadTimeout("fixture-password-must-not-leak")
            assert body["op"] == "GENERATE" and body["operationId"] == 1
            assert body["imp"] is None and body["opts"] == "" and body["currency"] == 2
            assert body["priceBase"] == body["priceTotal"] in (90, 60)
            self.rows["License001"] = {**self.common("License001", body, 2),
                "product": body["prod"], "bundle": body["bndl"], "region": body["region"],
                "deviceId": body["deviceId"], "licFileName": "device-license.bin",
                "licFile": base64.b64encode(OUTPUT).decode()}
            if self.loss == "after_license":
                raise httpx.ReadTimeout("fixture-session-must-not-leak")
            result = {"e": ""}  # No assumed record ID in the real unknown response.
        elif name == "createPayment":
            self.creates["payment"] += 1
            assert body["items"][0]["id"] == "License001" and len(body["items"]) == 1
            assert body["items"][0]["createdAt"]["__type"] == "Date"
            assert body["ext"] is False and body["imp"] is None
            self.rows["Payment001"] = {**self.common("Payment001", body, 4), "payItems": body["items"]}
            if self.loss == "after_payment":
                raise httpx.ReadTimeout("fixture-session-must-not-leak")
            result = None  # Creation is reconciled by own marker, not guessed output.
        elif name == "checkStripeOrder":
            self.creates["checkout"] += 1
            assert body["id"] == "Payment001" and body["payment_id"] == ""
            result = {"id": "cs_fixture", "client_secret": "cs_fixture_secret_fixture"}
            if self.checkout_fields:
                result.update(amount_total=self.rows["Payment001"]["priceTotal"] * 100, currency="usd")
        else:
            pytest.fail("Unexpected store function: " + name)
        return httpx.Response(200, json={"result": result})


def ledger_invoice(tmp_path, *, paid=True, plan="full"):
    ledger = Ledger(tmp_path / "ledger.sqlite3", simulation=False)
    source = tmp_path / "device.id.bin"
    source.write_bytes(b"fixture-device-id-" + uuid4().hex.encode())
    invoice = ledger.invoice(uuid4().hex, 123, plan, source,
                             hashlib.sha256(source.read_bytes()).hexdigest(), 1000,
                             shared_address=TEST_ADDRESS, allow_amount_suffix=True)
    if paid:
        txid = "a" * 64
        transfer = parse_solid_receipt(synthetic_receipt(TEST_ADDRESS, invoice["amount_atoms"], txid,
                                      timestamp_ms=invoice["created_ms"] + 1), txid)[0]
        ledger.observe(transfer)
        ledger.confirm_exchange(invoice["id"], DepositCredit("100001", txid, TEST_ADDRESS, invoice["amount_atoms"]))
    return ledger, ledger.get(invoice["id"])


async def setup_adapter(tmp_path, *, paid=True, executor=None, enabled=True, plan="full", verify_binding=True):
    ledger, invoice = ledger_invoice(tmp_path, paid=paid, plan=plan)
    shop = PublicContractShop()
    client = DriveModsPurchaseClient(allow_creates=True, transport=httpx.MockTransport(shop))
    await client.login("fixture-user", "fixture-password")
    if verify_binding:
        # Existing owned fixture, read-only evidence; no create is used to
        # manufacture the capability and no real merchant record is accessed.
        proof_source = b"existing-owner-proof-device-id"
        proof_marker = "existing-owner-proof"
        common = {"createdBy": shop.owner(), "isActive": True, "paymentStatus": 3,
                  "currency": 2, "priceTotal": 90, "dealerComment": proof_marker}
        shop.rows["ProofLic01"] = {**deepcopy(common), "id": "ProofLic01", "type": 2,
            "product": "MB-S5WM", "bundle": "FULL", "region": None, "deviceId": "proof-device",
            "licFile": base64.b64encode(OUTPUT).decode(), "licFileName": "device-license.bin"}
        shop.rows["ProofPay01"] = {**deepcopy(common), "id": "ProofPay01", "type": 4,
            "payItems": [{"id": "ProofLic01", "quantity": 1, "sum": 90}]}
        await client.verify_existing_paid_binding(LicenseExpectation(
            "ProofLic01", "ProofPay01", OWNER, "full", "MB-S5WM", "FULL", None, "proof-device",
            hashlib.sha256(proof_source).hexdigest(), 9000, proof_marker), proof_source)
    adapter = DriveModsStoreAdapter(client, ledger, StoreJournal(tmp_path / "store.sqlite3"),
                                    purchases_enabled=enabled, executor=executor)
    return adapter, shop, invoice


@pytest.mark.parametrize("plan", ["full", "eco"])
def test_real_adapter_prepares_exact_license_and_invoice_then_stops_without_executor(tmp_path, plan):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path, plan=plan)
        async with adapter.client:
            order = await adapter.prepare(invoice)
            assert order.status == "unpaid" and order.total_cents == {"full": 9000, "eco": 6000}[plan]
            assert shop.creates == {"license": 1, "payment": 1, "checkout": 0}
            repeated = await adapter.prepare(invoice)
            assert repeated.reference == order.reference and shop.creates["license"] == 1
            blocked = await adapter.pay(order.reference, "license:" + invoice["id"])
            assert blocked.status == "action_required" and shop.creates["checkout"] == 0
            with pytest.raises(Exception):
                await adapter.download(order.reference)
    asyncio.run(run())


@pytest.mark.parametrize("condition", ["unpaid", "disabled", "changed_price", "recoverable", "changed_source"])
def test_no_creation_before_credit_and_exact_quote_proof(tmp_path, condition):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path, paid=condition != "unpaid", enabled=condition != "disabled")
        if condition == "changed_price":
            shop.price = 91
        if condition == "recoverable":
            shop.recoverable = True
        if condition == "changed_source":
            from pathlib import Path
            Path(invoice["source_path"]).write_bytes(b"changed")
        async with adapter.client:
            with pytest.raises(PurchaseReviewRequired):
                await adapter.prepare(invoice)
            assert shop.creates == {"license": 0, "payment": 0, "checkout": 0}
    asyncio.run(run())


@pytest.mark.parametrize("loss", ["after_license", "after_payment"])
def test_lost_creation_reply_is_reconciled_after_restart_without_duplicate(tmp_path, loss):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        async with adapter.client:
            shop.loss = loss
            with pytest.raises(Exception) as caught:
                await adapter.prepare(invoice)
            assert "fixture-" not in str(caught.value)
            shop.loss = None
            restarted = DriveModsStoreAdapter(adapter.client, adapter.ledger, StoreJournal(tmp_path / "store.sqlite3"),
                                              purchases_enabled=True)
            order = await restarted.prepare(invoice)
            assert order.status == "unpaid" and shop.creates == {"license": 1, "payment": 1, "checkout": 0}
    asyncio.run(run())


def test_uncertain_creation_without_record_never_retries(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        async with adapter.client:
            shop.loss = "before_license"
            with pytest.raises(Exception):
                await adapter.prepare(invoice)
            shop.loss = None
            with pytest.raises(PurchaseReviewRequired, match="retry is forbidden"):
                await adapter.prepare(invoice)
            assert shop.creates == {"license": 1, "payment": 0, "checkout": 0}
    asyncio.run(run())


class ExplicitExecutor:
    automatic_payment_ready = True  # Verified mock integration only, never production.

    def __init__(self):
        self.calls, self.shop, self.mode = 0, None, "pending"

    async def start(self, session, authorization, key):
        self.calls += 1
        assert authorization.card_last4 == "6994"
        assert session.total_cents == authorization.max_cents == 9000
        assert session.currency == authorization.currency == "USD"
        assert session.payment_id == authorization.payment_id == "Payment001"
        assert key == "license:" + authorization.invoice_id
        assert "secret" not in repr(session)
        if self.mode == "paid_in_backend":
            for record in self.shop.rows.values():
                record["paymentStatus"] = 3
            return "pending"  # Only backend record reads may report paid.
        if self.mode == "lost":
            raise TimeoutError("fixture-callback-lost")
        return self.mode


@pytest.mark.parametrize("mode", ["pending", "action_required", "paid_in_backend"])
def test_checkout_callback_is_not_payment_proof_and_cannot_run_twice(tmp_path, mode):
    async def run():
        executor = ExplicitExecutor()
        adapter, shop, invoice = await setup_adapter(tmp_path, executor=executor)
        executor.shop, executor.mode = shop, mode
        async with adapter.client:
            order = await adapter.prepare(invoice)
            outcome = await adapter.pay(order.reference, "license:" + invoice["id"])
            assert outcome.status == ("paid" if mode == "paid_in_backend" else mode)
            await adapter.pay(order.reference, "license:" + invoice["id"])
            assert executor.calls == shop.creates["checkout"] == 1
            if outcome.status == "paid":
                assert await adapter.download(order.reference) == OUTPUT
            else:
                with pytest.raises(Exception):
                    await adapter.download(order.reference)
    asyncio.run(run())


def test_unknown_stripe_amount_blocks_executor_and_checkout_is_not_recreated(tmp_path):
    async def run():
        executor = ExplicitExecutor()
        adapter, shop, invoice = await setup_adapter(tmp_path, executor=executor)
        async with adapter.client:
            order = await adapter.prepare(invoice)
            shop.checkout_fields = False
            with pytest.raises(PurchaseReviewRequired, match="amount"):
                await adapter.pay(order.reference, "license:" + invoice["id"])
            assert executor.calls == 0
            assert (await adapter.pay(order.reference, "license:" + invoice["id"])).status == "pending"
            assert shop.creates["checkout"] == 1
    asyncio.run(run())


def test_lost_executor_reply_only_allows_read_only_reconciliation(tmp_path):
    async def run():
        executor = ExplicitExecutor()
        adapter, shop, invoice = await setup_adapter(tmp_path, executor=executor)
        async with adapter.client:
            order = await adapter.prepare(invoice)
            executor.mode = "lost"
            with pytest.raises(PurchaseReviewRequired, match="uncertain"):
                await adapter.pay(order.reference, "license:" + invoice["id"])
            for row in shop.rows.values():
                row["paymentStatus"] = 3
            assert (await adapter.inspect(order.reference)).status == "paid"
            assert (await adapter.pay(order.reference, "license:" + invoice["id"])).status == "paid"
            assert executor.calls == shop.creates["checkout"] == 1
    asyncio.run(run())


def test_paid_backend_different_license_or_changed_binary_cannot_be_delivered(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        async with adapter.client:
            order = await adapter.prepare(invoice)
            for row in shop.rows.values():
                row["paymentStatus"] = 3
            assert (await adapter.inspect(order.reference)).status == "paid"
            shop.rows["License001"]["licFile"] = base64.b64encode(b"another-license").decode()
            with pytest.raises(Exception):
                await adapter.download(order.reference)
            shop.rows["Payment001"]["payItems"][0]["id"] = "Other001"
            with pytest.raises(PurchaseReviewRequired):
                await adapter.inspect(order.reference)
    asyncio.run(run())


def test_concurrent_workers_cannot_create_duplicate_licenses(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        other = DriveModsStoreAdapter(adapter.client, adapter.ledger, StoreJournal(tmp_path / "store.sqlite3"), purchases_enabled=True)
        async with adapter.client:
            await asyncio.gather(adapter.prepare(invoice), other.prepare(invoice), return_exceptions=True)
            assert shop.creates["license"] == shop.creates["payment"] == 1
            assert (await adapter.prepare(invoice)).status == "unpaid"
    asyncio.run(run())


def test_readiness_cannot_be_enabled_by_login_or_purchase_flags_alone(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        async with adapter.client:
            assert adapter.merchant_auth_verified is True
            assert adapter.automatic_payment_ready is False
            adapter.executor = object()
            assert adapter.automatic_payment_ready is False
            order = await adapter.prepare(invoice)
            assert (await adapter.pay(order.reference, "license:" + invoice["id"])).status == "action_required"
            assert shop.creates["checkout"] == 0
        assert adapter.merchant_auth_verified is False
    asyncio.run(run())


def test_missing_real_schema_device_binding_blocks_before_upload_or_creation(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path, verify_binding=False)
        async with adapter.client:
            assert adapter.merchant_auth_verified is True
            assert adapter.merchant_device_binding_verified is False
            with pytest.raises(PurchaseReviewRequired, match="binding has not been verified"):
                await adapter.prepare(invoice)
            assert not any(name == "licenseGetInfo" for name, _ in shop.calls)
            assert shop.creates == {"license": 0, "payment": 0, "checkout": 0}
    asyncio.run(run())


def test_engine_runs_actual_adapter_contract_once_through_delivery_using_only_mock_shop(tmp_path):
    class FixtureDelivery:
        simulation = False

        def __init__(self):
            self.sent = 0

        async def send(self, invoice, path, filename):
            assert path.read_bytes() == OUTPUT and filename == "device.license.bin"
            self.sent += 1
            return "fixture-telegram-ack"

    async def run():
        executor = ExplicitExecutor()
        adapter, shop, invoice = await setup_adapter(tmp_path, executor=executor)
        executor.shop, executor.mode = shop, "paid_in_backend"
        delivery = FixtureDelivery()
        engine = Engine(adapter.ledger, adapter, delivery, live_purchase_enabled=True)
        async with adapter.client:
            for _ in range(8):
                await engine.step(invoice["id"])
            assert adapter.ledger.get(invoice["id"])["state"] == "delivered"
            assert delivery.sent == 1 and executor.calls == 1
            assert shop.creates == {"license": 1, "payment": 1, "checkout": 1}
    asyncio.run(run())


def test_catalog_preflight_needs_no_device_and_does_not_equate_other_specs_or_prices(tmp_path):
    async def run():
        adapter, shop, _ = await setup_adapter(tmp_path, verify_binding=False)
        shop.extra_prices = [
            {"isActive": True, "price": 60, "product": {"name": "LG-GEN5", "spec": "CUSTOM+RUS", "region": None}},
            {"isActive": True, "price": 175, "product": {"name": "HM-GEN5W", "spec": "FULL", "region": None}},
        ]
        async with adapter.client:
            offers = await adapter.client.preflight_catalog()
            assert [(o.bundle, o.cost_cents) for o in offers] == [("FULL", 9000), ("ECO", 6000)]
            assert not any(name in {"licenseGetInfo", "createNewLicense", "createPayment"} for name, _ in shop.calls)
            assert adapter.merchant_device_binding_verified is False
    asyncio.run(run())


def test_creation_rid_source_hash_and_own_id_baseline_are_committed_before_network_write(tmp_path):
    async def run():
        adapter, shop, invoice = await setup_adapter(tmp_path)
        checked = []

        def before_write(body):
            row = adapter.journal.get("DMCO-" + invoice["id"])
            proof = row["request_proof"]
            assert row["phase"] == "license_creating"
            assert proof["rid"] == body["rid"] == row["binding"]["license_rid"]
            assert proof["source_sha256"] == invoice["source_sha"]
            assert proof["owner_id"] == OWNER and proof["baseline_ids"] == ["ProofLic01"]
            assert "License001" not in proof["baseline_ids"]
            checked.append(True)

        shop.before_create = before_write
        async with adapter.client:
            await adapter.prepare(invoice)
            assert checked == [True]
    asyncio.run(run())


def test_creation_reply_contract_has_no_guessed_default_and_candidate_never_activates_capabilities():
    with pytest.raises(TypeError):
        CreationReplyContract()
    with pytest.raises(ValueError):
        CreationReplyContract(())
    contract = CreationReplyContract(("new_record", "id"))  # Synthetic contract, not an asserted vendor path.
    context = dict(request_id="RQ-fixture", source=b"fixture-device-id", owner_id=OWNER,
                   baseline_ids=frozenset({"OldRecord1"}))
    with pytest.raises(PurchaseReviewRequired):
        contract.parse({"e": ""}, **context)  # Actual public caller consumes only e; no ID can be inferred.
    candidate = contract.parse({"e": "", "new_record": {"id": "NewRecord1"}}, **context)
    assert candidate.record_id == "NewRecord1"
    assert candidate.source_sha256 == hashlib.sha256(context["source"]).hexdigest()
    assert not hasattr(candidate, "merchant_device_binding_verified")
    with pytest.raises(PurchaseReviewRequired):
        contract.parse({"e": "", "new_record": {"id": "OldRecord1"}}, **context)
    with pytest.raises(PurchaseReviewRequired):
        contract.parse({"e": "", "new_record": {"id": True}}, **context)

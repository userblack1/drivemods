"""Guarded DriveMods preparation and reconciliation, with no card implementation.

Creates are explicit, journaled once before network I/O and never retried after
an uncertain result. A separate, trusted PaymentExecutor must perform checkout;
neither a Checkout Session nor an executor callback is treated as paid proof.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Protocol
from uuid import uuid4

from .drivemods import (
    APP_VERSION, DriveModsError, DriveModsParseClient, DriveModsVerificationError,
    LicenseExpectation, OwnedRecord, _cents, _object_id, _text, _valid_id,
)
from .fulfillment import StoreOrder
from .ledger import Ledger


_PRICES = {"full": 9000, "eco": 6000}
_TXID = re.compile(r"[0-9a-f]{64}\Z")


class PurchaseReviewRequired(DriveModsError):
    """Stop and reconcile, never repeat a create or charge on an uncertain result."""


@dataclass(frozen=True)
class CatalogOffer:
    product: str
    bundle: str
    region: str | None
    cost_cents: int


@dataclass(frozen=True)
class CreationReplyCandidate:
    """Correlation evidence only; NOT paid/device proof or a readiness flag."""

    record_id: str
    request_id: str
    source_sha256: str
    owner_id: str
    baseline_sha256: str
    reply_sha256: str


@dataclass(frozen=True)
class CreationReplyContract:
    """Explicit future-observed JSON path. No real default path is known.

    Public DriveMods code consumes only result.e. This parser therefore cannot
    infer an ID field, enable a capability or substitute an old matching record.
    A candidate still requires authenticated response provenance and GET_ONE
    ownership/new-record/device checks in a verified integration.
    """

    record_id_path: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.record_id_path, tuple) or not 1 <= len(self.record_id_path) <= 8
                or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
                       for key in self.record_id_path)):
            raise ValueError("An explicit observed creation-reply path is required")

    def parse(self, reply, *, request_id: str, source: bytes, owner_id: str,
              baseline_ids: frozenset[str]) -> CreationReplyCandidate:
        if (not isinstance(reply, dict) or reply.get("e") not in (None, "") or "error" in reply
                or not _valid_id(request_id) or not request_id.startswith("RQ-")
                or not isinstance(source, bytes) or not source or len(source) > 16 * 1024 * 1024
                or not _valid_id(owner_id) or not isinstance(baseline_ids, frozenset)
                or len(baseline_ids) > 10000 or not all(_valid_id(x) for x in baseline_ids)):
            raise PurchaseReviewRequired("Creation reply correlation context is not verified")
        value = reply
        for key in self.record_id_path:
            if not isinstance(value, dict) or key not in value:
                raise PurchaseReviewRequired("Creation reply does not match its observed contract")
            value = value[key]
        if not _valid_id(value) or value in baseline_ids:
            raise PurchaseReviewRequired("Creation reply did not identify a new record")
        try:
            # Fingerprints contain no raw reply, license or session credentials.
            encoded = json.dumps(reply, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError):
            raise PurchaseReviewRequired("Creation reply shape is not verified") from None
        baseline = json.dumps(sorted(baseline_ids), separators=(",", ":")).encode()
        return CreationReplyCandidate(value, request_id, hashlib.sha256(source).hexdigest(), owner_id,
                                      hashlib.sha256(baseline).hexdigest(), hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class DeviceOffer:
    product: str
    bundle: str
    region: str | None
    device_id: str = field(repr=False)
    version: str
    version_custom: str | None
    cost_cents: int


@dataclass(frozen=True)
class CheckoutSession:
    session_id: str
    payment_id: str
    total_cents: int
    currency: Literal["USD"]
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class PaymentAuthorization:
    invoice_id: str
    license_id: str
    payment_id: str
    plan: Literal["full", "eco"]
    max_cents: int
    currency: Literal["USD"] = "USD"
    # Explicit existing owner authorization; no PAN/CVC or card secrets here.
    card_last4: str = "6994"


class PaymentExecutor(Protocol):
    """Implement against a verified checkout surface, enforcing authorization.

    Must verify exact merchant, session, amount/currency and authorized card
    BEFORE submission. Must honor the persisted idempotency key and handle 3DS
    as action_required. Returning 'paid' is deliberately not supported.
    """

    @property
    def automatic_payment_ready(self) -> bool:
        """True only after this executor's real checkout/card integration is verified."""
        ...

    async def start(self, session: CheckoutSession, authorization: PaymentAuthorization,
                    idempotency_key: str) -> Literal["pending", "action_required"]: ...


class DriveModsPurchaseClient(DriveModsParseClient):
    """Explicitly writable companion; inherited normal reader remains read-only."""

    def __init__(self, *, allow_creates: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.allow_creates = allow_creates is True
        self._binding_proof_owner_id: str | None = None

    async def preflight_catalog(self) -> tuple[CatalogOffer, ...]:
        """Read supported own prices without uploading or requiring a Device ID."""
        group = await self._cloud("getUserPriceGroup", {})
        items = await self._cloud("getUserPriceItems", {})
        if not isinstance(group, dict) or type(group.get("currency")) is not int or group["currency"] != 2:
            raise PurchaseReviewRequired("Supplier price is not in USD")
        if not isinstance(items, list):
            raise DriveModsVerificationError("Unknown supplier price list")
        offers, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("product"), dict):
                raise DriveModsVerificationError("Unknown supplier price item")
            if item.get("isActive") is not True:
                continue
            product = item["product"]
            name, spec, region = product.get("name"), product.get("spec"), product.get("region")
            if not _text(name) or not _text(spec) or region is not None and not _text(region):
                raise DriveModsVerificationError("Unknown supplier product classification")
            if spec.casefold() not in _PRICES:
                continue  # CUSTOM+RUS is not automatically ECO just because it costs $60.
            price = _cents(item.get("price"))
            if price != _PRICES[spec.casefold()]:
                continue  # e.g. FULL $175 is not authorized by the FULL $90 cap.
            key = (name, spec, region)
            if key in seen:
                raise PurchaseReviewRequired("Supplier price selection is ambiguous")
            seen.add(key)
            offers.append(CatalogOffer(name, spec, region, price))
        if not offers:
            raise PurchaseReviewRequired("No supported FULL or ECO supplier prices are available")
        return tuple(offers)

    @property
    def merchant_device_binding_verified(self) -> bool:
        return bool(self.identity and self._binding_proof_owner_id == self.identity.user_id)

    async def verify_existing_paid_binding(self, expected: LicenseExpectation, source: bytes) -> bool:
        """Prove the supplier exposes exact device/order linkage for this account.

        This is a read-only check against an already known own paid example, not
        an enable flag. Old live samples lacking device/comment fields fail it.
        A later creation-response proof needs a separately verified contract.
        """
        self._binding_proof_owner_id = None
        identity = self.identity
        await self.reconcile_paid_license(expected, source)
        if not identity or self.identity != identity:
            raise PurchaseReviewRequired("Store identity changed during binding verification")
        self._binding_proof_owner_id = identity.user_id
        return True

    async def _cloud(self, name: str, params: dict, *, creates: bool = False):
        read_names = {"licenseGetInfo", "getUserPriceGroup", "getUserPriceItems"}
        create_names = {"createNewLicense", "createPayment", "checkStripeOrder"}
        if (name not in read_names | create_names or creates != (name in create_names)
                or creates and not self.allow_creates):
            raise DriveModsError("DriveMods creation is disabled or unsupported")
        response = await self._send_request("functions/" + name,
                                            {**params, "appVersion": APP_VERSION}, authenticated=True)
        if "result" not in response:
            raise PurchaseReviewRequired("Store function result is unknown")
        result = response["result"]
        if isinstance(result, dict) and (result.get("error") is not None or result.get("e") not in (None, "")):
            raise PurchaseReviewRequired("Store function did not confirm success")
        return result

    async def quote_device(self, source: bytes, plan: str, *, region: str | None = None) -> DeviceOffer:
        if not self.identity or plan not in _PRICES or not source or len(source) > self.max_license_bytes:
            raise DriveModsVerificationError("Invalid device quote request")
        info_result = await self._cloud("licenseGetInfo", {
            "rid": "RQ-" + str(uuid4()), "uid": self.identity.username,
            "did": base64.urlsafe_b64encode(source).decode("ascii"),
        })
        info = info_result.get("info") if isinstance(info_result, dict) else None
        if (not isinstance(info, dict) or not _text(info.get("product"))
                or not _text(info.get("device_id")) or not _text(info.get("version_software"))
                or info.get("outdated") is not False or info.get("recoverable") is not False):
            raise PurchaseReviewRequired("Device requires review before a new license")
        custom = info.get("version_custom")
        if custom is not None and not _text(custom):
            raise DriveModsVerificationError("Unknown device version format")
        bundles = info.get("bundles")
        if not isinstance(bundles, list) or not all(_text(x) for x in bundles):
            raise DriveModsVerificationError("Unknown offered packages")
        choices = [x for x in bundles if x.casefold() == plan]
        if len(choices) != 1:
            raise PurchaseReviewRequired("Requested package is not uniquely available")
        regions = info.get("regions")
        if regions is None or regions == []:
            if region is not None:
                raise PurchaseReviewRequired("Unexpected requested license region")
            selected_region = None
        elif isinstance(regions, list) and all(_text(x) for x in regions):
            selected_region = regions[0] if region is None and len(regions) == 1 else region
            if selected_region is None or regions.count(selected_region) != 1:
                raise PurchaseReviewRequired("License region must be selected explicitly")
        else:
            raise PurchaseReviewRequired("Unknown offered license regions")
        group = await self._cloud("getUserPriceGroup", {})
        items = await self._cloud("getUserPriceItems", {})
        if not isinstance(group, dict) or type(group.get("currency")) is not int or group["currency"] != 2:
            raise PurchaseReviewRequired("Supplier price is not in USD")
        if not isinstance(items, list):
            raise DriveModsVerificationError("Unknown supplier price list")
        matches = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("product"), dict):
                raise DriveModsVerificationError("Unknown supplier price item")
            product = item["product"]
            if (item.get("isActive") is True and product.get("name") == info["product"]
                    and product.get("spec") == choices[0] and product.get("region") == selected_region):
                matches.append(item)
        if len(matches) != 1 or _cents(matches[0].get("price")) != _PRICES[plan]:
            raise PurchaseReviewRequired("Supplier package price does not match the authorized amount")
        return DeviceOffer(info["product"], choices[0], selected_region, info["device_id"],
                           info["version_software"], custom, _PRICES[plan])

    async def find_marker(self, marker: str | None, record_type: int) -> list[str]:
        """Read own pages, matching exact marker locally; do not trust text search."""
        if not self.identity or record_type not in (2, 4) or marker is not None and not _text(marker):
            raise DriveModsVerificationError("Invalid reconciliation request")
        found, seen, expected_total = [], set(), None
        for page in range(1, 101):
            response = await self._send_request("functions/processQuery", {
                "type": "GET_LIST", "resource": "Record", "appVersion": APP_VERSION,
                "params": {"pagination": {"page": page, "perPage": 100},
                           "sort": {"field": "createdAt", "order": "DESC"},
                           "filter": {"createdBy": self.identity.user_id, "type_num": record_type}},
            }, authenticated=True)
            result = response.get("result")
            if (not isinstance(result, dict) or result.get("error") is not None
                    or type(result.get("total")) is not int or not 0 <= result["total"] <= 10000
                    or not isinstance(result.get("data"), list)):
                raise PurchaseReviewRequired("Store reconciliation page is unknown")
            if expected_total is not None and result["total"] != expected_total:
                raise PurchaseReviewRequired("Store records changed during reconciliation")
            expected_total = result["total"]
            for row in result["data"]:
                record_id = _object_id(row, class_name="Record")
                if (record_id in seen or _object_id(row.get("createdBy"), class_name="_User") != self.identity.user_id
                        or type(row.get("type")) is not int or row["type"] != record_type):
                    raise PurchaseReviewRequired("Store reconciliation identities are inconsistent")
                seen.add(record_id)
                if marker is None or row.get("dealerComment") == marker:
                    found.append(record_id)
            if len(seen) == expected_total:
                return found
            if not result["data"] or len(seen) > expected_total:
                raise PurchaseReviewRequired("Store reconciliation pages are incomplete")
        raise PurchaseReviewRequired("Too many records for safe reconciliation")

    async def create_license(self, source: bytes, offer: DeviceOffer, marker: str,
                             *, request_id: str | None = None):
        if (not self.identity or not isinstance(offer, DeviceOffer) or not _text(marker)
                or not isinstance(source, bytes) or not source or len(source) > self.max_license_bytes
                or not _text(offer.product) or not _text(offer.device_id) or not _text(offer.version)
                or offer.region is not None and not _text(offer.region)
                or offer.version_custom is not None and not _text(offer.version_custom)
                or offer.bundle.casefold() not in _PRICES
                or offer.cost_cents != _PRICES[offer.bundle.casefold()]):
            raise DriveModsVerificationError("Invalid license creation binding")
        request_id = request_id or "RQ-" + str(uuid4())
        if not _valid_id(request_id) or not request_id.startswith("RQ-"):
            raise DriveModsVerificationError("Invalid license request identity")
        return await self._cloud("createNewLicense", {
            "rid": request_id, "uid": self.identity.username,
            "did": base64.urlsafe_b64encode(source).decode("ascii"), "op": "GENERATE",
            "prod": offer.product, "bndl": offer.bundle, "region": offer.region, "opts": "",
            "operationId": 1, "version": offer.version, "versionCustom": offer.version_custom,
            "deviceId": offer.device_id, "recoverable": False, "outdated": False,
            "priceBase": offer.cost_cents // 100, "priceTotal": offer.cost_cents // 100,
            "currency": 2, "adminComment": None, "dealerComment": marker, "imp": None,
        }, creates=True)

    async def create_payment(self, license_record: OwnedRecord, marker: str) -> None:
        created_at = license_record.details.get("createdAt")
        if isinstance(created_at, dict) or hasattr(created_at, "keys"):
            created_at = dict(created_at)
            date_text = created_at.get("iso") if created_at.get("__type") == "Date" else None
        else:
            date_text = created_at
        try:
            if not isinstance(date_text, str):
                raise ValueError
            datetime.fromisoformat(date_text.replace("Z", "+00:00"))
        except ValueError:
            raise PurchaseReviewRequired("License creation timestamp is not verified") from None
        if (not self.identity or license_record.owner_id != self.identity.user_id
                or license_record.record_type != 2 or license_record.payment_status != 1
                or license_record.total_cents not in _PRICES.values() or not license_record.is_active
                or license_record.currency_id != 2 or license_record.details.get("dealerComment") != marker):
            raise PurchaseReviewRequired("Cannot create a payment for this license")
        name = " ".join(str(license_record.details.get(k) or "") for k in ("product", "bundle", "region")).strip()
        await self._cloud("createPayment", {
            "sum": license_record.total_cents // 100,
            "items": [{"id": license_record.record_id, "name": name, "createdAt": created_at,
                       "quantity": 1, "sum": license_record.total_cents // 100}],
            "currency": 2, "ext": False, "imp": None, "adminComment": None, "dealerComment": marker,
        }, creates=True)

    async def create_checkout(self, payment: OwnedRecord) -> CheckoutSession:
        if (not self.identity or payment.owner_id != self.identity.user_id or payment.record_type != 4
                or payment.payment_status != 1 or payment.is_active is not True
                or payment.currency_id != 2 or payment.total_cents not in _PRICES.values()):
            raise PurchaseReviewRequired("Cannot create checkout for this payment")
        previous = payment.details.get("stripePayment")
        previous_id = (previous.get("id") or "") if previous is not None else ""
        if previous_id and (not isinstance(previous_id, str) or len(previous_id) > 256):
            raise PurchaseReviewRequired("Unknown existing checkout identity")
        result = await self._cloud("checkStripeOrder", {"id": payment.record_id, "payment_id": previous_id}, creates=True)
        # Amount/currency are normal Stripe Session fields, but their actual
        # presence in DriveMods' response still needs a live authorized check.
        if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                or not result["id"].startswith("cs_") or len(result["id"]) > 256
                or not isinstance(result.get("client_secret"), str)
                or len(result["client_secret"]) > 4096 or any(ord(c) < 32 for c in result["client_secret"])
                or not result["client_secret"].startswith(result["id"] + "_secret_")
                or type(result.get("amount_total")) is not int or result["amount_total"] != payment.total_cents
                or result.get("currency") != "usd"):
            raise PurchaseReviewRequired("Checkout amount and identity are not verified")
        return CheckoutSession(result["id"], payment.record_id, payment.total_cents, "USD", result["client_secret"])


class StoreJournal:
    """Independent local write-ahead journal; no card/session secrets are stored."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS store_orders(
                reference TEXT PRIMARY KEY, binding TEXT NOT NULL, phase TEXT NOT NULL,
                license_id TEXT NOT NULL DEFAULT '',payment_id TEXT NOT NULL DEFAULT '',
                output_sha TEXT NOT NULL DEFAULT '',request_proof TEXT NOT NULL DEFAULT '')""")
            if "request_proof" not in {row["name"] for row in db.execute("PRAGMA table_info(store_orders)")}:
                db.execute("ALTER TABLE store_orders ADD COLUMN request_proof TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, reference: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM store_orders WHERE reference=?", (reference,)).fetchone()
            return {**dict(row), "binding": json.loads(row["binding"]),
                    "request_proof": json.loads(row["request_proof"]) if row["request_proof"] else None} if row else None

    def reserve(self, reference: str, binding: dict):
        text = json.dumps(binding, sort_keys=True, separators=(",", ":"))
        with self._db() as db:
            row = db.execute("SELECT binding FROM store_orders WHERE reference=?", (reference,)).fetchone()
            if row and row["binding"] != text:
                raise PurchaseReviewRequired("Existing store order binding changed")
            db.execute("INSERT OR IGNORE INTO store_orders(reference,binding,phase) VALUES(?,?,'reserved')", (reference, text))

    def move(self, reference: str, old: str, new: str, **values):
        if not set(values) <= {"license_id", "payment_id", "output_sha", "request_proof"}:
            raise ValueError("Invalid journal field")
        with self._db() as db:
            assignments = ",".join(["phase=?"] + [key + "=?" for key in values])
            changed = db.execute("UPDATE store_orders SET " + assignments + " WHERE reference=? AND phase=?",
                                 (new, *values.values(), reference, old)).rowcount
        if changed != 1:
            raise PurchaseReviewRequired("Another worker reserved this store operation")


class DriveModsStoreAdapter:
    simulation = False

    def __init__(self, client: DriveModsPurchaseClient, ledger: Ledger, journal: StoreJournal,
                 *, purchases_enabled: bool = False, executor: PaymentExecutor | None = None,
                 region: str | None = None):
        if ledger.simulation or not ledger.require_exchange_credit:
            raise ValueError("Real store requires a real ledger with confirmed exchange credits")
        self.client, self.ledger, self.journal = client, ledger, journal
        self.purchases_enabled, self.executor, self.region = purchases_enabled is True, executor, region

    @property
    def merchant_auth_verified(self) -> bool:
        return self.client.identity is not None

    @property
    def merchant_device_binding_verified(self) -> bool:
        return self.client.merchant_device_binding_verified

    @property
    def automatic_payment_ready(self) -> bool:
        # No concrete verified card executor ships with this project. Neither a
        # Parse login nor saved Link card nor purchase-enable flag proves this.
        return (self.merchant_auth_verified and self.merchant_device_binding_verified
                and self.purchases_enabled and self.client.allow_creates
                and self.executor is not None
                and getattr(self.executor, "automatic_payment_ready", False) is True)

    def _confirmed(self, invoice: dict, *, preparing=False):
        if not self.client.identity:
            raise PurchaseReviewRequired("Real store is not authenticated")
        row = self.ledger.get(invoice["id"])
        states = {"paid", "preparing"} if preparing else {
            "paid", "preparing", "prepared", "inspecting", "charging", "waiting_supplier", "action_required",
            "purchase_review", "download_pending", "downloading", "ready", "sending", "delivered",
        }
        keys = ("id", "plan", "source_path", "source_sha", "cost_cents", "txid", "exchange_deposit")
        if (row["state"] not in states or any(row.get(k) != invoice.get(k) for k in keys)
                or row["plan"] not in _PRICES or type(row["cost_cents"]) is not int
                or row["cost_cents"] != _PRICES[row["plan"]]
                or not isinstance(row.get("txid"), str) or not _TXID.fullmatch(row["txid"])
                or not _text(row.get("exchange_deposit"))
                or row["exchange_deposit"].upper().startswith(("DEMO", "SIMULATED", "TEST", "FIXTURE"))):
            raise PurchaseReviewRequired("Confirmed client payment does not match the store order")
        return row

    def _source(self, binding):
        source = Path(binding["source_path"]).read_bytes()
        if (not source or len(source) > self.client.max_license_bytes
                or hashlib.sha256(source).hexdigest() != binding["source_sha"]):
            raise PurchaseReviewRequired("Device ID source changed")
        return source

    def _bound(self, reference):
        row = self.journal.get(reference)
        if not row:
            raise PurchaseReviewRequired("Store reference is not registered")
        binding = row["binding"]
        self._confirmed(binding)
        if binding["owner_id"] != self.client.identity.user_id:
            raise PurchaseReviewRequired("Store account changed")
        self._source(binding)
        return row

    def _check_record(self, record: OwnedRecord, binding: dict, record_type: int, license_id=""):
        if (record.owner_id != binding["owner_id"] or record.record_type != record_type
                or record.is_active is not True or record.currency_id != 2
                or record.total_cents != binding["cost_cents"] or record.payment_status not in (1, 2, 3)
                or record.details.get("dealerComment") != binding["marker"]):
            raise PurchaseReviewRequired("Created store record does not match the order")
        if record_type == 2:
            offer = binding["offer"]
            if any(record.details.get(k) != offer[v] for k, v in (
                    ("product", "product"), ("bundle", "bundle"), ("region", "region"), ("deviceId", "device_id"))):
                raise PurchaseReviewRequired("Created license belongs to another device or package")
        else:
            items = record.details.get("payItems")
            if (not isinstance(items, tuple) or len(items) != 1 or not hasattr(items[0], "get")
                    or items[0].get("id") != license_id or type(items[0].get("quantity")) is not int
                    or items[0]["quantity"] != 1 or _cents(items[0].get("sum")) != binding["cost_cents"]):
                raise PurchaseReviewRequired("Created payment contains unexpected licenses")

    async def _locate(self, binding, record_type, license_id=""):
        matches = await self.client.find_marker(binding["marker"], record_type)
        if len(matches) > 1:
            raise PurchaseReviewRequired("Duplicate store records need review")
        if not matches:
            return None
        record = await self.client.get_owned_record(matches[0])
        self._check_record(record, binding, record_type, license_id)
        return record

    async def prepare(self, invoice: dict) -> StoreOrder:
        if not self.purchases_enabled or not self.client.allow_creates:
            raise PurchaseReviewRequired("Real store purchasing is not enabled")
        if not self.merchant_device_binding_verified:
            raise PurchaseReviewRequired("Supplier device and order binding has not been verified")
        if not _valid_id(invoice.get("id")):
            raise PurchaseReviewRequired("Invalid client invoice identity")
        reference = "DMCO-" + invoice["id"]
        row = self.journal.get(reference)
        confirmed = self._confirmed(invoice, preparing=row is None)
        if row is None:
            source = self._source(confirmed)
            offer = await self.client.quote_device(source, confirmed["plan"], region=self.region)
            binding = {k: confirmed[k] for k in ("id", "plan", "source_path", "source_sha", "cost_cents", "txid", "exchange_deposit")}
            binding.update(owner_id=self.client.identity.user_id, marker="crypto-order:" + confirmed["id"],
                           offer=asdict(offer), license_rid="RQ-" + str(uuid4()))
            self.journal.reserve(reference, binding)
        row = self._bound(reference)
        binding = row["binding"]
        if row["phase"] in ("reserved", "license_creating"):
            operation_phase = row["phase"]
            license_record = await self._locate(binding, 2)
            if license_record is None and row["phase"] == "reserved":
                baseline = frozenset(await self.client.find_marker(None, 2))
                request_id = binding.get("license_rid")
                if not _valid_id(request_id) or not request_id.startswith("RQ-"):
                    raise PurchaseReviewRequired("Creation request was not journaled")
                baseline_text = json.dumps(sorted(baseline), separators=(",", ":"))
                request_proof = {"rid": request_id, "source_sha256": binding["source_sha"],
                                 "owner_id": binding["owner_id"], "baseline_ids": sorted(baseline),
                                 "baseline_sha256": hashlib.sha256(baseline_text.encode()).hexdigest()}
                self.journal.move(reference, "reserved", "license_creating",
                                  request_proof=json.dumps(request_proof, sort_keys=True, separators=(",", ":")))
                operation_phase = "license_creating"
                self._bound(reference)
                await self.client.create_license(self._source(binding), DeviceOffer(**binding["offer"]), binding["marker"],
                                                  request_id=request_id)
                license_record = await self._locate(binding, 2)
            if license_record is None:
                raise PurchaseReviewRequired("License creation is uncertain; automatic retry is forbidden")
            self.journal.move(reference, operation_phase, "license_created", license_id=license_record.record_id)
            row = self._bound(reference)
        if row["phase"] in ("license_created", "payment_creating"):
            operation_phase = row["phase"]
            payment = await self._locate(binding, 4, row["license_id"])
            if payment is None and row["phase"] == "license_created":
                license_record = await self.client.get_owned_record(row["license_id"])
                self._check_record(license_record, binding, 2)
                if license_record.payment_status != 1:
                    raise PurchaseReviewRequired("Existing license payment requires reconciliation")
                self.journal.move(reference, "license_created", "payment_creating")
                operation_phase = "payment_creating"
                self._bound(reference)
                await self.client.create_payment(license_record, binding["marker"])
                payment = await self._locate(binding, 4, row["license_id"])
            if payment is None:
                raise PurchaseReviewRequired("Payment creation is uncertain; automatic retry is forbidden")
            self.journal.move(reference, operation_phase, "prepared", payment_id=payment.record_id)
        return await self.inspect(reference)

    def _order(self, row, status):
        binding = row["binding"]
        return StoreOrder(row["reference"], binding["id"], binding["source_sha"], binding["plan"],
                          "USD", binding["cost_cents"], status, row["payment_id"] if status == "paid" else "")

    def _expectation(self, row):
        b, offer = row["binding"], row["binding"]["offer"]
        return LicenseExpectation(row["license_id"], row["payment_id"], b["owner_id"], b["plan"],
                                  offer["product"], offer["bundle"], offer["region"], offer["device_id"],
                                  b["source_sha"], b["cost_cents"], b["marker"], row["output_sha"] or None)

    async def inspect(self, reference: str) -> StoreOrder:
        row = self._bound(reference)
        if not row["license_id"] or not row["payment_id"]:
            raise PurchaseReviewRequired("Store order has not been fully prepared")
        payment = await self.client.get_owned_record(row["payment_id"])
        license_record = await self.client.get_owned_record(row["license_id"])
        self._check_record(payment, row["binding"], 4, row["license_id"])
        self._check_record(license_record, row["binding"], 2)
        if payment.payment_status == license_record.payment_status == 3:
            verified = await self.client.reconcile_paid_license(self._expectation(row), self._source(row["binding"]))
            if row["phase"] != "paid":
                self.journal.move(reference, row["phase"], "paid", output_sha=verified.sha256)
            return self._order(row, "paid")
        if row["phase"] == "paid":
            raise PurchaseReviewRequired("Previously confirmed store payment changed")
        status = "unpaid" if row["phase"] == "prepared" and payment.payment_status == license_record.payment_status == 1 else "pending"
        if row["phase"] == "action_required":
            status = "action_required"
        return self._order(row, status)

    async def pay(self, reference: str, idempotency_key: str) -> StoreOrder:
        if not self.purchases_enabled or not self.client.allow_creates:
            raise PurchaseReviewRequired("Real store purchasing is not enabled")
        if not self.merchant_device_binding_verified:
            raise PurchaseReviewRequired("Supplier device and order binding has not been verified")
        row = self._bound(reference)
        if idempotency_key != "license:" + row["binding"]["id"]:
            raise PurchaseReviewRequired("Checkout idempotency key does not match the invoice")
        current = await self.inspect(reference)
        if current.status != "unpaid":
            return current
        if not self.automatic_payment_ready:
            self.journal.move(reference, "prepared", "action_required")
            return self._order(row, "action_required")
        self.journal.move(reference, "prepared", "checkout_creating")
        self._bound(reference)
        payment = await self.client.get_owned_record(row["payment_id"])
        self._check_record(payment, row["binding"], 4, row["license_id"])
        session = await self.client.create_checkout(payment)
        b = row["binding"]
        authorization = PaymentAuthorization(b["id"], row["license_id"], row["payment_id"], b["plan"], b["cost_cents"])
        try:
            result = await self.executor.start(session, authorization, idempotency_key)
        except Exception:
            raise PurchaseReviewRequired("Checkout execution is uncertain; read-only reconciliation is required") from None
        if result not in ("pending", "action_required"):
            raise PurchaseReviewRequired("Checkout executor returned an unsupported state")
        self.journal.move(reference, "checkout_creating", result)
        return await self.inspect(reference)

    async def download(self, reference: str) -> bytes:
        row = self._bound(reference)
        verified = await self.client.reconcile_paid_license(self._expectation(row), self._source(row["binding"]))
        if not row["output_sha"] or row["output_sha"] != verified.sha256:
            raise PurchaseReviewRequired("License has not been reconciled for delivery")
        return verified.content

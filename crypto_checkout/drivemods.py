"""Read-only DriveMods Parse client, based on its public web application's contract.

This module cannot create a license, invoice, checkout session or card charge.
It deliberately does not implement fulfillment.Store: purchasing is not verified.
Credentials are supplied explicitly by the caller and are never discovered from
environment files, browser storage or other applications.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import uuid4

import httpx


PARSE_URL = "https://store.drivemods.org/parse/"
APP_ID = "dmStoreApp"
APP_VERSION = "1.3.3"
_COST_CENTS = {"full": 9000, "eco": 6000}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DriveModsError(RuntimeError):
    """An intentionally sanitized error; never includes request/response data."""


class DriveModsVerificationError(DriveModsError):
    """The available records do not prove this exact paid license."""


@dataclass(frozen=True)
class ParseIdentity:
    user_id: str
    username: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class ParseSession:
    """Explicit caller-owned credential export; protect it like a password."""

    username: str
    user_id: str
    session_token: str


@dataclass(frozen=True)
class OwnedRecord:
    record_id: str
    owner_id: str
    record_type: int
    payment_status: int
    is_active: bool
    currency_id: int
    total_cents: int
    # Only selected business fields, never the full response/current user object.
    details: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class LicenseExpectation:
    """Persist these bindings before a future purchase; never infer from amount."""

    license_id: str
    payment_id: str
    owner_id: str
    plan: Literal["full", "eco"]
    product: str
    bundle: str
    region: str | None
    device_id: str = field(repr=False)
    source_sha256: str
    cost_cents: int
    # Optional additional binding to the bot order's exact dealer comment.
    dealer_comment: str | None = field(default=None, repr=False)
    license_sha256: str | None = None

    def __post_init__(self) -> None:
        if (not all(_valid_id(x) for x in (self.license_id, self.payment_id, self.owner_id))
                or self.license_id == self.payment_id
                or not isinstance(self.plan, str) or self.plan not in _COST_CENTS
                or type(self.cost_cents) is not int
                or self.cost_cents != _COST_CENTS[self.plan]
                or not isinstance(self.bundle, str) or self.bundle.casefold() != self.plan
                or not _text(self.product) or not _text(self.device_id)
                or not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256)
                or self.region is not None and not _text(self.region)
                or self.dealer_comment is not None and not _text(self.dealer_comment)
                or self.license_sha256 is not None and (
                    not isinstance(self.license_sha256, str) or not _SHA256.fullmatch(self.license_sha256))):
            raise DriveModsVerificationError("Invalid expected license binding")


@dataclass(frozen=True)
class VerifiedLicense:
    license_id: str
    payment_id: str
    owner_id: str
    plan: Literal["full", "eco"]
    cost_cents: int
    source_sha256: str
    sha256: str
    filename: str
    content: bytes = field(repr=False)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _text(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and value.strip() == value and not any(ord(c) < 32 for c in value))


def _integer(value: Any, message: str) -> int:
    if type(value) is not int:
        raise DriveModsVerificationError(message)
    return value


def _cents(value: Any) -> int:
    # JSON decimals are parsed as Decimal, never rounded from binary floats.
    if type(value) is not int and not isinstance(value, Decimal):
        raise DriveModsVerificationError("Invalid record amount")
    try:
        amount = Decimal(value) * 100
        valid = (amount.is_finite() and 0 <= amount <= 100_000_000
                 and amount == amount.to_integral_value())
    except (InvalidOperation, OverflowError):
        valid = False
    if not valid:
        raise DriveModsVerificationError("Invalid record amount")
    return int(amount)


def _object_id(value: Any, *, class_name: str | None = None) -> str:
    if not isinstance(value, dict):
        raise DriveModsVerificationError("Invalid record identity")
    if value.get("__type") not in (None, "Object", "Pointer"):
        raise DriveModsVerificationError("Invalid record identity")
    if class_name and value.get("className") not in (None, class_name):
        raise DriveModsVerificationError("Unexpected record class")
    object_id, record_id = value.get("objectId"), value.get("id")
    if object_id is not None and record_id is not None and object_id != record_id:
        raise DriveModsVerificationError("Conflicting record identities")
    identity = object_id if object_id is not None else record_id
    if not _valid_id(identity):
        raise DriveModsVerificationError("Invalid record identity")
    return identity


def _freeze(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        raise DriveModsVerificationError("Record nesting is too deep")
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v, depth + 1) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v, depth + 1) for v in value)
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


class DriveModsParseClient:
    """Only credential login and authenticated GET_ONE Record reads are exposed."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_license_bytes: int = 2 * 1024 * 1024) -> None:
        if type(max_license_bytes) is not int or not 1 <= max_license_bytes <= 16 * 1024 * 1024:
            raise ValueError("Invalid license size limit")
        self.max_license_bytes = max_license_bytes
        self._max_response_bytes = max_license_bytes * 2 + 256 * 1024
        self._session_token: str | None = None
        self._identity: ParseIdentity | None = None
        self._installation_id = str(uuid4())
        self._client = httpx.AsyncClient(base_url=PARSE_URL, timeout=20, trust_env=False,
                                         follow_redirects=False, transport=transport)

    @property
    def identity(self) -> ParseIdentity | None:
        return self._identity

    def export_session(self) -> ParseSession:
        if not self._identity or not self._session_token:
            raise DriveModsError("DriveMods login is required")
        return ParseSession(self._identity.username, self._identity.user_id, self._session_token)

    async def resume_session(self, session_token: str, user_id: str, username: str) -> ParseIdentity:
        """Verify only explicitly supplied credentials, never a browser session."""
        self._identity, self._session_token = None, None
        if (not _valid_id(user_id) or not _text(username) or not isinstance(session_token, str)
                or not 1 <= len(session_token) <= 4096 or any(ord(c) < 32 for c in session_token)):
            raise DriveModsError("Invalid saved DriveMods session")
        self._identity, self._session_token = ParseIdentity(user_id, username), session_token
        try:
            result = await self._request("users/me", {"_method": "GET"}, authenticated=True)
            if _object_id(result, class_name="_User") != user_id or result.get("username") != username:
                raise DriveModsError("Saved DriveMods session identity does not match")
        except Exception:
            self._identity, self._session_token = None, None
            raise DriveModsError("Saved DriveMods session could not be verified") from None
        return self._identity

    async def __aenter__(self) -> DriveModsParseClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self._session_token = None
        self._identity = None
        await self._client.aclose()

    async def _request(self, path: str, payload: dict, *, authenticated: bool) -> dict:
        # A typo/new caller cannot turn this reader into a mutating Cloud client.
        if path not in {"login", "users/me", "functions/processQuery"}:
            raise DriveModsError("This client does not support store mutations")
        if path == "users/me" and (not authenticated or payload != {"_method": "GET"}):
            raise DriveModsError("This client only reads the authenticated session identity")
        if path == "login" and (authenticated or set(payload) != {"username", "password", "_method"}
                                or payload.get("_method") != "GET"):
            raise DriveModsError("Unsupported login request")
        if path == "functions/processQuery" and (
                not authenticated or payload.get("type") != "GET_ONE" or payload.get("resource") != "Record"
                or set(payload) != {"type", "resource", "params", "appVersion"}
                or not isinstance(payload.get("params"), dict)
                or set(payload["params"]) != {"id"} or not _valid_id(payload["params"]["id"])
                or payload.get("appVersion") != APP_VERSION):
            raise DriveModsError("This client only reads individual store records")
        return await self._send_request(path, payload, authenticated=authenticated)

    async def _send_request(self, path: str, payload: dict, *, authenticated: bool) -> dict:
        """Shared transport; concrete clients must validate their own allowlist."""
        if authenticated and (not self._identity or not self._session_token):
            raise DriveModsError("DriveMods login is required")
        body = {**payload, "_ApplicationId": APP_ID, "_InstallationId": self._installation_id}
        if authenticated:
            body["_SessionToken"] = self._session_token
        try:
            async with self._client.stream("POST", path, json=body) as response:
                if not 200 <= response.status_code < 300:
                    raise DriveModsError("DriveMods request was rejected")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self._max_response_bytes:
                        raise DriveModsError("DriveMods response exceeds the size limit")
            result = json.loads(data, parse_float=Decimal, parse_constant=_invalid_constant,
                                object_pairs_hook=_no_duplicate_keys)
            if not isinstance(result, dict) or "error" in result or "code" in result:
                raise DriveModsError("DriveMods returned an error")
            return result
        except Exception:
            # HTTP exceptions contain request bodies/URLs; never propagate them.
            raise DriveModsError("DriveMods request failed") from None

    async def login(self, username: str, password: str) -> ParseIdentity:
        self._identity, self._session_token = None, None
        if not _text(username) or not isinstance(password, str) or not 1 <= len(password) <= 4096:
            raise DriveModsError("DriveMods login credentials are required")
        # Parse's shipped RESTController sends non-POST methods via _method.
        result = await self._request("login", {"username": username, "password": password,
                                              "_method": "GET"}, authenticated=False)
        try:
            user_id = _object_id(result, class_name="_User")
            token = result.get("sessionToken")
            if (result.get("username") != username or not isinstance(token, str)
                    or not 1 <= len(token) <= 4096 or any(ord(c) < 32 for c in token)):
                raise DriveModsVerificationError("Invalid login identity")
        except DriveModsVerificationError:
            raise DriveModsError("DriveMods login identity could not be verified") from None
        self._identity, self._session_token = ParseIdentity(user_id, username), token
        return self._identity

    async def get_owned_record(self, record_id: str) -> OwnedRecord:
        if not _valid_id(record_id):
            raise DriveModsVerificationError("Invalid store record ID")
        response = await self._request("functions/processQuery", {
            "type": "GET_ONE", "resource": "Record", "params": {"id": record_id},
            "appVersion": APP_VERSION,
        }, authenticated=True)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("error") is not None:
            raise DriveModsVerificationError("Store record response is not verified")
        raw = result.get("data")
        if not isinstance(raw, dict) or _object_id(raw, class_name="Record") != record_id:
            raise DriveModsVerificationError("Store returned another record")
        owner_id = _object_id(raw.get("createdBy"), class_name="_User")
        if not self._identity or owner_id != self._identity.user_id:
            raise DriveModsVerificationError("Store record belongs to another account")
        if type(raw.get("isActive")) is not bool:
            raise DriveModsVerificationError("Invalid active record status")
        details = {}
        for name in ("product", "bundle", "region", "deviceId", "version", "versionCustom", "createdAt",
                     "licenseType", "licFile", "licFileName", "payItems", "dealerComment"):
            if name in raw:
                details[name] = raw[name]
        if isinstance(raw.get("stripePayment"), dict):
            details["stripePayment"] = {"id": raw["stripePayment"].get("id")}
        if isinstance(details.get("payItems"), list):
            details["payItems"] = [
                {key: item[key] for key in ("id", "quantity", "sum") if key in item}
                if isinstance(item, dict) else None for item in details["payItems"]
            ]
        return OwnedRecord(record_id, owner_id, _integer(raw.get("type"), "Invalid record type"),
                           _integer(raw.get("paymentStatus"), "Invalid payment status"), raw["isActive"],
                           _integer(raw.get("currency"), "Invalid record currency"),
                           _cents(raw.get("priceTotal")), _freeze(details))

    async def reconcile_paid_license(self, expected: LicenseExpectation,
                                     source: bytes) -> VerifiedLicense:
        """Return bytes only after exact own invoice/license linkage is established.

        No broad lookup by amount, no state changes, and no optimistic fallback.
        A failed/unknown schema requires review; it never authorizes a new charge.
        """
        if (not isinstance(expected, LicenseExpectation) or not isinstance(source, bytes)
                or not source or len(source) > self.max_license_bytes
                or hashlib.sha256(source).hexdigest() != expected.source_sha256
                or not self._identity or expected.owner_id != self._identity.user_id):
            raise DriveModsVerificationError("Source or authenticated owner does not match")
        payment = await self.get_owned_record(expected.payment_id)
        license_record = await self.get_owned_record(expected.license_id)
        for record, record_type in ((payment, 4), (license_record, 2)):
            if (record.owner_id != expected.owner_id or record.record_type != record_type
                    or record.payment_status != 3 or record.is_active is not True
                    or record.currency_id != 2 or record.total_cents != expected.cost_cents):
                raise DriveModsVerificationError("Store does not prove the exact paid license")
            if expected.dealer_comment is not None and record.details.get("dealerComment") != expected.dealer_comment:
                raise DriveModsVerificationError("Store order comment does not match")
        items = payment.details.get("payItems")
        if not isinstance(items, tuple) or len(items) != 1 or not isinstance(items[0], Mapping):
            raise DriveModsVerificationError("Payment must contain only the expected license")
        item = items[0]
        if (item.get("id") != expected.license_id or type(item.get("quantity")) is not int
                or item["quantity"] != 1 or _cents(item.get("sum")) != expected.cost_cents):
            raise DriveModsVerificationError("Payment item does not match the license")
        details = license_record.details
        if (details.get("deviceId") != expected.device_id or details.get("product") != expected.product
                or details.get("bundle") != expected.bundle or details.get("region") != expected.region):
            raise DriveModsVerificationError("License device or package does not match")
        encoded = details.get("licFile")
        if isinstance(encoded, Mapping) and set(encoded) == {"__type", "base64"} and encoded["__type"] == "Bytes":
            encoded = encoded["base64"]
        if not isinstance(encoded, str) or len(encoded) > ((self.max_license_bytes + 2) // 3) * 4:
            raise DriveModsVerificationError("Missing or oversized license binary")
        try:
            # Live own records use standard Base64 without trailing '='. The
            # site's browser decoder accepts it; validate alphabet and pad bits.
            content = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        except (ValueError, binascii.Error):
            raise DriveModsVerificationError("Invalid license binary encoding") from None
        canonical = base64.b64encode(content).decode("ascii")
        if encoded not in (canonical, canonical.rstrip("=")):
            raise DriveModsVerificationError("Non-canonical license binary encoding")
        digest = hashlib.sha256(content).hexdigest()
        head = content.lstrip()[:64].lower()
        if (not content or len(content) > self.max_license_bytes or digest == expected.source_sha256
                or expected.license_sha256 is not None and digest != expected.license_sha256
                or content.startswith((b"%PDF-", b"\x89PNG\r\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                or head.startswith((b"<!doctype", b"<html", b'{"error"', b'{"message"'))):
            raise DriveModsVerificationError("Store did not return the expected license binary")
        filename = details.get("licFileName")
        if (not _text(filename) or len(filename) > 128 or not filename.lower().endswith((".bin", ".lic"))
                or any(c in filename for c in '/\\:*?"<>|') or filename in {".", ".."}):
            raise DriveModsVerificationError("Invalid license filename")
        return VerifiedLicense(expected.license_id, expected.payment_id, expected.owner_id, expected.plan,
                               expected.cost_cents, expected.source_sha256, digest, filename, content)

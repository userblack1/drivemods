import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import httpx
import pytest

from crypto_checkout.drivemods import (
    APP_ID, APP_VERSION, DriveModsError, DriveModsParseClient,
    DriveModsVerificationError, LicenseExpectation, source_order_marker,
)


SOURCE = b"\x00device-id-fixture-for-tests"
OUTPUT = b"\x00\x01signed-license-fixture-for-tests"
OWNER = "Owner00001"


def expectation(plan="full"):
    return LicenseExpectation(
        license_id="License001", payment_id="Payment001", owner_id=OWNER,
        plan=plan, product="IG19.KOR.SOP", bundle=plan.upper(), region=None,
        device_id="device-123", source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        cost_cents={"full": 9000, "eco": 6000}[plan], dealer_comment="checkout:test-order",
    )


def records(plan="full"):
    expected = expectation(plan)
    common = {
        "__type": "Object", "className": "Record",
        "createdBy": {"__type": "Pointer", "className": "_User", "objectId": OWNER},
        "currency": 2, "isActive": True, "paymentStatus": 3,
        "priceTotal": expected.cost_cents // 100, "dealerComment": expected.dealer_comment,
    }
    payment = {**deepcopy(common), "objectId": expected.payment_id, "type": 4,
               "payItems": [{"id": expected.license_id, "quantity": 1,
                             "sum": expected.cost_cents // 100}]}
    license_record = {**deepcopy(common), "objectId": expected.license_id, "type": 2,
                      "deviceId": expected.device_id, "product": expected.product,
                      "bundle": expected.bundle, "region": expected.region,
                      "licFile": base64.b64encode(OUTPUT).decode(), "licFileName": "device-license.bin"}
    return {expected.payment_id: payment, expected.license_id: license_record}


def mock_client(rows=None, *, query_reply=None, login_reply=None, max_license_bytes=2048):
    seen = []
    rows = records() if rows is None else rows

    def handler(request):
        assert request.url.host == "store.drivemods.org"
        assert request.method == "POST"
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        assert body["_ApplicationId"] == APP_ID
        if request.url.path == "/parse/login":
            assert body["_method"] == "GET"
            assert "_SessionToken" not in body
            if login_reply is not None:
                return login_reply(request)
            return httpx.Response(200, json={"objectId": OWNER, "username": "fixture-user",
                                            "sessionToken": "fixture-session-do-not-log"})
        if request.url.path == "/parse/users/me":
            assert body["_method"] == "GET"
            assert body["_SessionToken"] == "fixture-session-do-not-log"
            if query_reply is not None:
                return query_reply(request)
            return httpx.Response(200, json={"objectId": OWNER, "username": "fixture-user"})
        assert request.url.path == "/parse/functions/processQuery"
        assert body["type"] == "GET_ONE" and body["resource"] == "Record"
        assert body["_SessionToken"] == "fixture-session-do-not-log"
        assert body["appVersion"] == APP_VERSION
        if query_reply is not None:
            return query_reply(request)
        return httpx.Response(200, json={"result": {"data": rows[body["params"]["id"]]}})

    return DriveModsParseClient(transport=httpx.MockTransport(handler),
                               max_license_bytes=max_license_bytes), seen


async def logged_in(client):
    await client.login("fixture-user", "fixture-password-do-not-log")
    return client


@pytest.mark.parametrize("plan", ["full", "eco"])
def test_reads_only_exact_own_paid_records_and_returns_hashed_binary(plan):
    async def run():
        async with mock_client(records(plan))[0] as client:
            identity = await client.login("fixture-user", "fixture-password-do-not-log")
            assert identity.user_id == OWNER
            verified = await client.reconcile_paid_license(expectation(plan), SOURCE)
            assert verified.content == OUTPUT
            assert verified.sha256 == hashlib.sha256(OUTPUT).hexdigest()
            assert verified.cost_cents == {"full": 9000, "eco": 6000}[plan]
            assert "fixture-user" not in repr(identity)
            assert "signed-license-fixture" not in repr(verified)
    asyncio.run(run())


@pytest.mark.parametrize("record,field,value", [
    ("Payment001", "paymentStatus", 1), ("Payment001", "paymentStatus", 2),
    ("Payment001", "paymentStatus", 4), ("License001", "paymentStatus", 2),
    ("Payment001", "isActive", False), ("License001", "isActive", False),
    ("Payment001", "isActive", 1), ("Payment001", "currency", 1),
    ("License001", "currency", 3), ("Payment001", "priceTotal", 89.99),
    ("Payment001", "priceTotal", 90.001), ("License001", "priceTotal", 60),
    ("Payment001", "type", 5), ("License001", "type", 1),
    ("Payment001", "objectId", "Another001"),
    ("License001", "product", "ANOTHER.PRODUCT"),
    ("License001", "bundle", "ECO"), ("License001", "region", "another"),
    ("License001", "deviceId", "another-device"),
    ("License001", "dealerComment", "another-order"),
    ("Payment001", "dealerComment", "another-order"),
])
def test_wrong_or_unpaid_record_never_produces_a_license(record, field, value):
    rows = records()
    rows[record][field] = value

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(expectation(), SOURCE)
    asyncio.run(run())


@pytest.mark.parametrize("items", [
    [], [{"id": "Another001", "quantity": 1, "sum": 90}],
    [{"id": "License001", "quantity": 2, "sum": 90}],
    [{"id": "License001", "quantity": True, "sum": 90}],
    [{"id": "License001", "quantity": 1, "sum": "90"}],
    [{"id": "License001", "quantity": 1, "sum": 60}],
    [{"id": "License001", "quantity": 1, "sum": 90}] * 2,
    [{"id": "License001", "quantity": 1, "sum": 90}, {"id": "Other001", "quantity": 1, "sum": 0}],
])
def test_payment_must_prove_one_exact_license_item(items):
    rows = records()
    rows["Payment001"]["payItems"] = items

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(expectation(), SOURCE)
    asyncio.run(run())


@pytest.mark.parametrize("record", ["Payment001", "License001"])
def test_cannot_read_another_users_record_even_if_paid(record):
    rows = records()
    rows[record]["createdBy"]["objectId"] = "OtherOwner"

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError, match="another account"):
                await client.get_owned_record(record)
    asyncio.run(run())


@pytest.mark.parametrize("content", [SOURCE, b"<html>success</html>", b'{"error":"failed"}',
                                     b"%PDF-1.4", b"\x89PNG\r\nfixture", b""])
def test_returned_input_error_page_or_document_is_not_a_license(content):
    rows = records()
    rows["License001"]["licFile"] = base64.b64encode(content).decode()

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(expectation(), SOURCE)
    asyncio.run(run())


@pytest.mark.parametrize("encoded", ["not base64", "AAA===", "AB==", "data:application/octet-stream;base64,AAAA"])
def test_invalid_noncanonical_or_unexpected_file_encodings_fail_closed(encoded):
    rows = records()
    rows["License001"]["licFile"] = encoded

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(expectation(), SOURCE)
    asyncio.run(run())


def test_source_and_previous_output_hashes_bind_resumed_downloads():
    async def run():
        client, seen = mock_client()
        async with client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError, match="Source"):
                await client.reconcile_paid_license(expectation(), SOURCE + b"changed")
            assert len(seen) == 1
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(replace(expectation(), license_sha256="0" * 64), SOURCE)
    asyncio.run(run())


@pytest.mark.parametrize("padding", [True, False])
def test_canonical_standard_base64_with_or_without_padding_matches_live_encoding(padding):
    rows = records()
    content = OUTPUT
    while len(content) % 3 == 0:
        content += b"x"
    encoded = base64.b64encode(content).decode()
    assert encoded.endswith("=")
    rows["License001"]["licFile"] = encoded if padding else encoded.rstrip("=")

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            verified = await client.reconcile_paid_license(expectation(), SOURCE)
            assert verified.content == content and verified.sha256 == hashlib.sha256(content).hexdigest()
    asyncio.run(run())


def test_bytes_wrapper_decimal_amount_and_immutable_sanitized_record():
    rows = records()
    rows["License001"]["licFile"] = {"__type": "Bytes", "base64": base64.b64encode(OUTPUT).decode()}
    rows["License001"]["createdBy"]["sessionToken"] = "must-not-return"
    rows["License001"]["sessionToken"] = "must-not-return"

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            record = await client.get_owned_record("License001")
            assert "sessionToken" not in record.details and "createdBy" not in record.details
            with pytest.raises(TypeError):
                record.details["deviceId"] = "changed"
            verified = await client.reconcile_paid_license(expectation(), SOURCE)
            assert verified.content == OUTPUT
    asyncio.run(run())


@pytest.mark.parametrize("reply", [
    lambda _: httpx.Response(403, json={"error": "fixture-session-do-not-log"}),
    lambda _: httpx.Response(200, json={"error": "fixture-password-do-not-log", "code": 209}),
    lambda _: httpx.Response(200, content=b'{"result":{"data":null},"result":{"error":"secret"}}'),
    lambda _: httpx.Response(200, content=b'{"result":{"data":NaN}}'),
    lambda _: httpx.Response(302, headers={"Location": "https://example.invalid/secret"}),
])
def test_transport_errors_and_malformed_json_never_expose_credentials(reply):
    async def run():
        async with mock_client(query_reply=reply)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsError) as caught:
                await client.get_owned_record("License001")
            assert "fixture-" not in str(caught.value) + repr(caught.value)
    asyncio.run(run())


def test_failed_relogin_discards_previous_session():
    calls = 0

    def login_reply(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"objectId": OWNER, "username": "fixture-user",
                                            "sessionToken": "fixture-session-do-not-log"})
        return httpx.Response(401, json={"error": "fixture-password-do-not-log"})

    async def run():
        async with mock_client(login_reply=login_reply)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsError):
                await logged_in(client)
            assert client.identity is None
            with pytest.raises(DriveModsError, match="login is required"):
                await client.get_owned_record("License001")
    asyncio.run(run())


def test_allowlist_rejects_all_mutating_calls_without_network():
    async def run():
        client, seen = mock_client()
        async with client:
            await logged_in(client)
            for name in ("createNewLicense", "createPayment", "checkStripeOrder", "setPaymentIsPaying"):
                with pytest.raises(DriveModsError):
                    await client._request("functions/" + name, {}, authenticated=True)
            with pytest.raises(DriveModsError):
                await client._request("functions/processQuery", {"type": "UPDATE", "resource": "Record"},
                                      authenticated=True)
            assert len(seen) == 1
    asyncio.run(run())


def test_oversized_record_is_rejected_before_decoding():
    reply = lambda _: httpx.Response(200, content=b" " * 270000)

    async def run():
        async with mock_client(query_reply=reply, max_license_bytes=128)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsError):
                await client.get_owned_record("License001")
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"bundle": "ECO"}, {"cost_cents": 10000}, {"license_id": "../license"},
    {"payment_id": "License001"}, {"source_sha256": "bad"}, {"cost_cents": True},
])
def test_invalid_expected_plan_price_and_identity_are_rejected(change):
    with pytest.raises(DriveModsVerificationError):
        replace(expectation(), **change)


def test_explicit_session_export_and_verified_resume_without_password():
    async def run():
        async with mock_client()[0] as first:
            await logged_in(first)
            exported = first.export_session()
            assert exported.username == "fixture-user" and exported.user_id == OWNER
            assert "fixture" not in repr(exported)
        second, seen = mock_client()
        async with second:
            identity = await second.resume_session(exported.session_token, exported.user_id, exported.username)
            assert identity.user_id == OWNER
            assert len(seen) == 1 and seen[0][0] == "/parse/users/me"
            assert "password" not in seen[0][1]
            assert (await second.get_owned_record("Payment001")).owner_id == OWNER
    asyncio.run(run())


@pytest.mark.parametrize("reply", [
    lambda _: httpx.Response(401, json={"error": "fixture-session-do-not-log"}),
    lambda _: httpx.Response(200, json={"objectId": "OtherOwner", "username": "fixture-user"}),
    lambda _: httpx.Response(200, json={"objectId": OWNER, "username": "another-user"}),
])
def test_invalid_saved_session_is_discarded_and_errors_are_sanitized(reply):
    async def run():
        async with mock_client(query_reply=reply)[0] as client:
            with pytest.raises(DriveModsError) as caught:
                await client.resume_session("fixture-session-do-not-log", OWNER, "fixture-user")
            assert client.identity is None and "fixture" not in str(caught.value)
            with pytest.raises(DriveModsError):
                client.export_session()
    asyncio.run(run())


def marker_expectation(plan="full"):
    original = expectation(plan)
    invoice_id = "1" * 32
    return replace(original, binding_policy="sha256_marker", invoice_id=invoice_id,
                   dealer_comment=source_order_marker(invoice_id, original.source_sha256))


def marker_rows(plan="full"):
    rows = records(plan)
    expected = marker_expectation(plan)
    for row in rows.values():
        row["dealerComment"] = expected.dealer_comment
    del rows["License001"]["deviceId"]
    return rows


@pytest.mark.parametrize("plan", ["full", "eco"])
def test_source_hash_policy_accepts_omitted_device_only_with_both_full_persisted_order_markers(plan):
    async def run():
        async with mock_client(marker_rows(plan))[0] as client:
            await logged_in(client)
            verified = await client.reconcile_paid_license(marker_expectation(plan), SOURCE)
            assert verified.content == OUTPUT and verified.cost_cents == {"full": 9000, "eco": 6000}[plan]
    asyncio.run(run())


@pytest.mark.parametrize("record,field,value", [
    ("License001", "dealerComment", None), ("Payment001", "dealerComment", None),
    ("License001", "dealerComment", "crypto-order:" + "1" * 32),
    ("Payment001", "dealerComment", "crypto-order:" + "1" * 32 + ":" + "0" * 64),
    ("License001", "dealerComment", "crypto-order:" + "2" * 32 + ":" + hashlib.sha256(SOURCE).hexdigest()),
    ("License001", "deviceId", "conflicting-device"),
    ("License001", "product", "another-product"), ("License001", "bundle", "ECO"),
    ("License001", "region", "another-region"), ("License001", "paymentStatus", 2),
    ("Payment001", "currency", 1), ("Payment001", "priceTotal", 60),
    ("Payment001", "payItems", [{"id": "OtherLicense", "quantity": 1, "sum": 90}]),
])
def test_source_hash_policy_never_weakens_other_identity_payment_or_marker_checks(record, field, value):
    rows = marker_rows()
    rows[record][field] = value

    async def run():
        async with mock_client(rows)[0] as client:
            await logged_in(client)
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(marker_expectation(), SOURCE)
    asyncio.run(run())


def test_hash_marker_is_not_inferred_as_a_new_policy_by_the_default_reader():
    async def run():
        async with mock_client(marker_rows())[0] as client:
            await logged_in(client)
            expected = replace(marker_expectation(), binding_policy="strict")
            with pytest.raises(DriveModsVerificationError):
                await client.reconcile_paid_license(expected, SOURCE)
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"invoice_id": None}, {"invoice_id": "other-invoice"}, {"dealer_comment": None},
    {"dealer_comment": "crypto-order:" + "1" * 32}, {"binding_policy": "unknown"},
])
def test_hash_marker_policy_requires_exact_explicit_invoice_and_source_digest(change):
    with pytest.raises(DriveModsVerificationError):
        replace(marker_expectation(), **change)

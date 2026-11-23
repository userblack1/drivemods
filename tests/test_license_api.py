import asyncio
import json
import ssl

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
import pytest

from drivemods_bot.api import LicenseApiSession, make_bot


class Response:
    status = 200

    def __init__(self, *, enter_error=None, read_error=None, body=None):
        self.enter_error, self.read_error = enter_error, read_error
        self.body = body or {"ok": True, "result": {"message_id": 1, "date": 1794060000,
                            "chat": {"id": 11, "type": "private"}, "text": "license"}}

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        if self.read_error:
            raise self.read_error
        return json.dumps(self.body)


def setup(monkeypatch, responses):
    api = LicenseApiSession(connect_attempts=3, retry_delay=0)
    calls = []
    class HTTP:
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)
    async def create():
        return HTTP()
    monkeypatch.setattr(api, "create_session", create)
    return api, calls


def test_connection_failures_retry_but_keep_tls_verification(monkeypatch):
    api, calls = setup(monkeypatch, [Response(enter_error=aiohttp.ConnectionTimeoutError()), Response()])
    response = asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "license"))
    assert response.message_id == 1 and len(calls) == 2
    assert calls[0]["timeout"].sock_connect == 6
    assert api._connector_init["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert api._connector_init["ssl"].check_hostname


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ServerDisconnectedError()])
def test_uncertain_request_is_not_replayed(monkeypatch, error):
    api, calls = setup(monkeypatch, [Response(read_error=error)])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "license"))
    assert len(calls) == 1


def test_rejected_request_is_not_retried(monkeypatch):
    response = Response(body={"ok": False, "error_code": 400, "description": "Bad Request"})
    response.status = 400
    api, calls = setup(monkeypatch, [response])
    with pytest.raises(TelegramBadRequest):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "license"))
    assert len(calls) == 1


def test_connection_wait_is_bounded(monkeypatch):
    api, calls = setup(monkeypatch, [Response(enter_error=aiohttp.ConnectionTimeoutError()) for _ in range(3)])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "license"))
    assert len(calls) == 3


def test_application_uses_hardened_session_and_html_defaults():
    bot = make_bot("123456:TEST_TOKEN_ONLY")
    assert isinstance(bot.session, LicenseApiSession)
    assert bot.default.parse_mode == "HTML"

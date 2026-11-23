import asyncio
import json
import ssl

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
import pytest

from firmware_bot.api import FirmwareApiSession


class Response:
    status = 200

    def __init__(self, *, enter_error=None, read_error=None, body=None):
        self.enter_error = enter_error
        self.read_error = read_error
        self.body = body or {"ok": True, "result": {"message_id": 1, "date": 1794060000,
                            "chat": {"id": 11, "type": "private"}, "text": "firmware"}}

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


def setup_session(monkeypatch, responses):
    api = FirmwareApiSession(connect_attempts=3, retry_delay=0)
    calls = []

    class HTTP:
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    async def create():
        return HTTP()

    monkeypatch.setattr(api, "create_session", create)
    return api, calls


def test_failed_connections_retry_before_transmitting_and_then_deliver(monkeypatch):
    api, calls = setup_session(monkeypatch, [Response(enter_error=aiohttp.ConnectionTimeoutError("dial")),
                                            Response(enter_error=aiohttp.ConnectionTimeoutError("dial")), Response()])
    result = asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "firmware"))
    assert result.message_id == 1 and len(calls) == 3
    assert calls[0]["timeout"].sock_connect == 6
    assert api._connector_init["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert api._connector_init["ssl"].check_hostname


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ServerDisconnectedError("response lost")])
def test_uncertain_send_is_never_replayed(monkeypatch, error):
    api, calls = setup_session(monkeypatch, [Response(read_error=error)])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "firmware"))
    assert len(calls) == 1


def test_connection_retries_are_bounded(monkeypatch):
    api, calls = setup_session(monkeypatch, [Response(enter_error=aiohttp.ConnectionTimeoutError("dial")) for _ in range(3)])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "firmware"))
    assert len(calls) == 3


def test_api_rejection_is_not_retried(monkeypatch):
    response = Response(body={"ok":False,"error_code":400,"description":"Bad Request"})
    response.status = 400
    api, calls = setup_session(monkeypatch, [response])
    with pytest.raises(TelegramBadRequest):
        asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "firmware"))
    assert len(calls) == 1


def test_explicit_rate_limit_waits_before_retry_and_does_not_drop_package(monkeypatch):
    limited = Response(body={"ok": False, "error_code": 429, "description": "Too Many Requests",
                             "parameters": {"retry_after": 1}})
    limited.status = 429
    api, calls = setup_session(monkeypatch, [limited, Response()])
    result = asyncio.run(Bot("123456:TEST_TOKEN_ONLY", session=api).send_message(11, "firmware"))
    assert result.message_id == 1 and len(calls) == 2


def test_concurrent_writes_are_paced_before_transmission(monkeypatch):
    async def scenario():
        api = FirmwareApiSession(write_interval=0.025)
        times = []
        class HTTP:
            def post(self, url, **kwargs):
                times.append(asyncio.get_running_loop().time())
                return Response()
        async def create():
            return HTTP()
        monkeypatch.setattr(api, "create_session", create)
        bot = Bot("123456:TEST_TOKEN_ONLY", session=api)
        results = await asyncio.gather(*(bot.send_message(user, "synthetic") for user in (1, 2, 3)))
        assert len(results) == len(times) == 3
        assert all(b - a >= 0.02 for a, b in zip(times, times[1:]))
    asyncio.run(scenario())


def test_pacing_rechecks_deadline_when_a_timer_wakes_early(monkeypatch):
    from types import SimpleNamespace
    import firmware_bot.api as api_module
    async def scenario():
        clock=SimpleNamespace(now=100.0)
        sleeps=[];sent=[]
        async def sleep(delay):
            sleeps.append(delay)
            clock.now+=delay/2 if delay>0.002 else delay
        fake_asyncio=SimpleNamespace(Lock=asyncio.Lock, get_running_loop=lambda:SimpleNamespace(time=lambda:clock.now),
                                     sleep=sleep,TimeoutError=asyncio.TimeoutError)
        monkeypatch.setattr(api_module,'asyncio',fake_asyncio)
        api=FirmwareApiSession(write_interval=0.04)
        api._next_write=100.04
        class HTTP:
            def post(self,*args,**kwargs):
                sent.append(clock.now)
                return Response()
        async def create():return HTTP()
        monkeypatch.setattr(api,'create_session',create)
        await Bot('123456:TEST_TOKEN_ONLY',session=api).send_message(11,'synthetic')
        assert len(sleeps)>1 and sent[0]>=100.04
    asyncio.run(scenario())

"""Provider limits defer verification without losing or accepting a payment."""
import asyncio
from email.utils import formatdate

import httpx
import pytest

from crypto_checkout.demo import TEST_ADDRESS
from crypto_checkout.tron import TronReader, TronRateLimited


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def test_concurrent_history_pages_and_solidity_reads_share_one_gate():
    clock, calls = Clock(), []

    async def handler(request):
        calls.append((clock(), request.url.path))
        await asyncio.sleep(0)
        if request.method == 'GET':
            more = 'fingerprint' not in request.url.params
            return httpx.Response(200, json={'success': True, 'data': [], 'meta':
                {'links': {'next': 'unused'}, 'fingerprint': 'page2'} if more else {}})
        return httpx.Response(200, json={'block_header': {'raw_data': {'number': 999}}})

    async def scenario():
        reader = TronReader(transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep)
        try:
            await asyncio.gather(reader.height(), reader.height(),
                                 reader.candidates(TEST_ADDRESS, 0), reader.candidates(TEST_ADDRESS, 1))
        finally:
            await reader.close()
    asyncio.run(scenario())
    assert len(calls) == 6
    assert all(b[0] - a[0] >= 1.1 - 1e-9 for a, b in zip(calls, calls[1:]))


@pytest.mark.parametrize('status,header,body,delay', [
    (429, '', 'suspended for 29 s', 31),
    (429, '90', '', 91),
    (429, '', 'suspended for 120 s', 121),
    (429, 'nan', '', 31),
    (429, 'inf', '', 31),
    (429, 'invalid date', '', 31),
    (429, formatdate(1090, usegmt=True), '', 91),
    (403, '', '', 301),
])
def test_cooldown_stops_all_network_calls_and_recovers(status, header, body, delay):
    clock, calls = Clock(), []
    secret = 'synthetic-private-api-value'

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={'Retry-After': header}, json={'Error': body + secret})
        return httpx.Response(200, json={'block_header': {'raw_data': {'number': 999}}})

    async def scenario():
        reader = TronReader(secret, transport=httpx.MockTransport(handler), clock=clock,
                            wall_clock=clock, sleep=clock.sleep)
        try:
            with pytest.raises(TronRateLimited) as caught:
                await reader.height()
            assert secret not in str(caught.value)
            clock.now += delay - .01
            with pytest.raises(TronRateLimited):
                await reader.candidates(TEST_ADDRESS, 0)
            with pytest.raises(TronRateLimited):
                await reader.transfers('a' * 64)
            assert len(calls) == 1
            clock.now += .02
            assert await reader.height() == 999
        finally:
            await reader.close()
    asyncio.run(scenario())
    assert len(calls) == 2


def test_pagination_rate_limit_never_returns_partial_history():
    clock, calls = Clock(), []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={'success': True, 'data': [{'transaction_id': 'a' * 64}],
                'meta': {'links': {'next': 'unused'}, 'fingerprint': 'page2'}})
        return httpx.Response(429, json={'Error': 'suspended for 30 s'})

    async def scenario():
        reader = TronReader(transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep)
        try:
            with pytest.raises(TronRateLimited):
                await reader.candidates(TEST_ADDRESS, 0)
            with pytest.raises(TronRateLimited):
                await reader.candidates(TEST_ADDRESS, 0)
        finally:
            await reader.close()
    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize('interval', [True, -1, float('nan'), float('inf'), 301])
def test_invalid_interval_rejected(interval):
    with pytest.raises(ValueError):
        TronReader(minimum_interval=interval)

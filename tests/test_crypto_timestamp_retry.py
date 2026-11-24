"""Bounded timestamp recovery applies exclusively to allowlisted read requests."""
import asyncio
from email.utils import formatdate
import hashlib
import hmac

import httpx
import pytest

from crypto_checkout.binance import BinanceReader, BinanceReadError


def execute(handler,now):
    async def scenario():
        reader=BinanceReader('synthetic-key','synthetic-secret',transport=httpx.MockTransport(handler),
                             clock=lambda:now[0])
        try:
            return await reader._get('/sapi/v1/capital/deposit/hisrec',{'coin':'USDT'})
        finally:await reader.close()
    return asyncio.run(scenario())


def test_slow_first_request_retries_once_with_new_time_and_signature():
    now=[1000.0]
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)==1:
            now[0]+=12
            return httpx.Response(400,json={'code':-1021,'msg':'not retained'})
        assert int(request.url.params['timestamp'])==1_012_000
        unsigned,signature=request.url.query.decode().rsplit('&signature=',1)
        assert hmac.compare_digest(signature,hmac.new(b'synthetic-secret',unsigned.encode(),hashlib.sha256).hexdigest())
        return httpx.Response(200,json=[])
    assert execute(handler,now)==[]
    assert len(requests)==2 and requests[0].url.params['signature']!=requests[1].url.params['signature']
    assert all(request.method=='GET' and request.url.params['recvWindow']=='10000' for request in requests)


@pytest.mark.parametrize('clock_offset',[-30,30])
def test_https_date_recovers_bounded_clock_offset_conservatively(clock_offset):
    now=[1000.75+clock_offset]
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:
            return httpx.Response(400,json={'code':-1021},headers={'Date':formatdate(1000,usegmt=True)})
        assert int(request.url.params['timestamp'])==1_000_000
        return httpx.Response(200,json=[])
    assert execute(handler,now)==[]
    assert len(calls)==2


@pytest.mark.parametrize('date',['malformed',formatdate(500,usegmt=True)])
def test_unparseable_or_unbounded_date_does_not_poison_signing_time(date):
    now=[1000.0]
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(400,json={'code':-1021},headers={'Date':date})
        assert request.url.params['timestamp']=='1000000'
        return httpx.Response(200,json=[])
    assert execute(handler,now)==[]


def test_repeated_timestamp_error_stops_after_two_reads_and_keeps_safe_diagnostics():
    now=[1000.0]
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(400,json={'code':-1021,'msg':str(request.url)})
    with pytest.raises(BinanceReadError) as error:execute(handler,now)
    assert len(calls)==2 and error.value.error_code==-1021
    assert 'signature=' not in str(error.value)


@pytest.mark.parametrize('status,code',[(400,-2015),(401,-1021),(429,-1003),(503,-1000)])
def test_auth_rate_limit_and_other_http_errors_never_retry_inline(status,code):
    now=[1000.0]
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'code':code})
    with pytest.raises(BinanceReadError):execute(handler,now)
    assert len(calls)==1


def test_learned_time_offset_expires_and_does_not_apply_after_wall_clock_jump():
    now=[1030.0]
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(400,json={'code':-1021},headers={'Date':formatdate(1000,usegmt=True)})
        return httpx.Response(200,json=[])
    async def scenario():
        reader=BinanceReader('synthetic-key','synthetic-secret',transport=httpx.MockTransport(handler),clock=lambda:now[0])
        try:
            path='/sapi/v1/capital/deposit/hisrec'
            await reader._get(path,{})
            now[0]=1040
            await reader._get(path,{})
            assert calls[-1].url.params['timestamp']=='1010000'
            now[0]=1331
            await reader._get(path,{})
            assert calls[-1].url.params['timestamp']=='1331000'
        finally:await reader.close()
    asyncio.run(scenario())

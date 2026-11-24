"""Operational errors remain diagnosable without authenticated URLs or secrets."""
import asyncio
import json
import traceback

import httpx
import pytest

from crypto_checkout.binance import BinanceReader, BinanceReadError
from crypto_checkout.demo import TEST_ADDRESS
from crypto_checkout.exchange_observer import ExchangeObservations
from crypto_checkout import exchange_observer


def read_error(handler):
    async def scenario():
        reader=BinanceReader('SYNTHETIC-KEY-NEVER-REAL','SYNTHETIC-SECRET-NEVER-REAL',
                             transport=httpx.MockTransport(handler),clock=lambda:1000)
        try:
            with pytest.raises(BinanceReadError) as caught:
                await reader._get('/sapi/v1/account/apiRestrictions',{})
            return caught.value
        finally:
            await reader.close()
    return asyncio.run(scenario())


def assert_private(error):
    rendered=''.join(traceback.format_exception(error))+repr(error)+json.dumps(error.__dict__)
    for private in ('SYNTHETIC-KEY','SYNTHETIC-SECRET','PRIVATE-BODY','signature=',
                    'api.binance.com','X-MBX-APIKEY'):
        assert private not in rendered


@pytest.mark.parametrize('status,code',[(401,-2015),(400,-1021),(429,-1003),(418,-1003),(503,-1000)])
def test_http_failures_keep_only_status_and_numeric_exchange_code(status,code):
    def handler(request):
        return httpx.Response(status,json={'code':code,'msg':'PRIVATE-BODY '+str(request.url)})
    error=read_error(handler)
    assert error.diagnostics()=={'http_status':status,'error_code':code,'network_type':None}
    assert str(error)=='Binance read failed: HTTPStatusError'
    assert_private(error)


@pytest.mark.parametrize('body',[{'code':'PRIVATE-BODY'},{'code':True},{'code':2**100},['PRIVATE-BODY']])
def test_untrusted_error_code_values_are_not_retained(body):
    error=read_error(lambda request:httpx.Response(400,json=body))
    assert error.error_code is None
    assert_private(error)


def test_html_error_page_is_not_retained_or_misclassified_as_json_failure():
    error=read_error(lambda request:httpx.Response(502,text='<html>PRIVATE-BODY</html>'))
    assert error.http_status==502 and error.error_code is None
    assert str(error)=='Binance read failed: HTTPStatusError'
    assert_private(error)


@pytest.mark.parametrize('exception',[httpx.ConnectError,httpx.ReadTimeout,httpx.RemoteProtocolError])
def test_network_failures_keep_type_without_url_headers_or_exception_message(exception):
    def handler(request):
        raise exception('PRIVATE-BODY '+str(request.url),request=request)
    error=read_error(handler)
    assert error.diagnostics()=={'http_status':None,'error_code':None,'network_type':exception.__name__}
    assert_private(error)


def test_success_status_with_invalid_json_keeps_no_response_data():
    error=read_error(lambda request:httpx.Response(200,text='PRIVATE-BODY'))
    assert error.http_status==200 and error.error_code is None and error.network_type is None
    assert str(error)=='Binance read failed: InvalidJSON'
    assert_private(error)


def test_health_records_safe_diagnostics_and_clears_failure_after_recovery(tmp_path):
    store=ExchangeObservations(tmp_path,TEST_ADDRESS,clock=lambda:100)
    error=read_error(lambda request:httpx.Response(429,json={'code':-1003,'msg':'PRIVATE-BODY'}))
    failed=store.health(ok=False,error=error)
    assert failed['error_type']=='BinanceReadError'
    assert failed['error_details']=={'http_status':429,'error_code':-1003,'network_type':None}
    saved=(tmp_path/'exchange-health.json').read_text()
    assert 'PRIVATE-BODY' not in saved and TEST_ADDRESS not in saved and 'signature=' not in saved
    healthy=store.health(ok=True,scan_complete=True)
    assert healthy['ok'] and healthy['error_type'] is None and healthy['error_details'] is None
    assert json.loads((tmp_path/'exchange-health.json').read_text())==healthy


def test_non_binance_exception_message_is_never_saved(tmp_path):
    store=ExchangeObservations(tmp_path,TEST_ADDRESS,clock=lambda:100)
    report=store.health(ok=False,error=ValueError('PRIVATE-BODY signature=private'))
    assert report['error_type']=='ValueError' and report['error_details'] is None
    assert 'PRIVATE-BODY' not in (tmp_path/'exchange-health.json').read_text()


def test_running_observer_recovers_transient_failure_and_backoff_without_cursor_loss(tmp_path,monkeypatch,caplog):
    readers=[]
    sleeps=[]
    class Reader:
        def __init__(self,*args):
            self.windows=[]
            self.closed=False
            readers.append(self)
        async def history(self,address,start,end):
            self.windows.append((start,end))
            if len(self.windows)==1:
                raise BinanceReadError('ReadTimeout',network_type='ReadTimeout')
            return []
        async def close(self):self.closed=True
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps)==2:raise asyncio.CancelledError()
    monkeypatch.setattr(exchange_observer,'DepositHistoryReader',Reader)
    monkeypatch.setattr(exchange_observer.asyncio,'sleep',sleep)
    monkeypatch.setattr(exchange_observer.os,'umask',lambda mode:0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exchange_observer.run(TEST_ADDRESS,tmp_path,interval=30))
    assert sleeps==[60,30]
    assert readers[0].windows[0]==readers[0].windows[1]
    assert readers[0].closed
    health=json.loads((tmp_path/'exchange-health.json').read_text())
    assert health['ok'] and health['scan_complete']
    assert health['error_type'] is None and health['error_details'] is None
    assert health['payment_confirmed_for_orders'] is health['license_purchase_enabled'] is False
    assert 'ReadTimeout' in caplog.text
    assert TEST_ADDRESS not in caplog.text and 'signature=' not in caplog.text

"""Regression checks for authenticated deposit reads and independent scan retries."""
import asyncio
from decimal import Decimal
import hashlib

import httpx
import pytest

from crypto_checkout.binance import BinanceReader,DepositCredit,match_credit,validate_read_only_permissions
from crypto_checkout.demo import TEST_ADDRESS,create_synthetic_invoice,synthetic_receipt
from crypto_checkout.ledger import Ledger
from crypto_checkout.tron import address_from_bytes,parse_solid_receipt
from crypto_checkout.watcher import Watcher


def permissions(**changes):
    return {'enableReading':True,'enableWithdrawals':False,'enableSpotAndMarginTrading':False,
            'enableFutures':False,'enableMargin':False,'enableInternalTransfer':False,
            'permitsUniversalTransfer':False,'enableVanillaOptions':False,
            'enableFixApiTrade':False,'enablePortfolioMarginTrading':False,**changes}


def transfer_for(row,index=1):
    txid=hashlib.sha256(str(index).encode()).hexdigest()
    raw=synthetic_receipt(row['address'],row['amount_atoms'],txid,
                          timestamp_ms=row['created_ms']+1)
    return parse_solid_receipt(raw,txid)[0]


@pytest.mark.parametrize('change',[{'enableWithdrawals':True},{'enableSpotAndMarginTrading':True},
                                  {'enableReading':False},{'enableFixApiTrade':True},
                                  {'enablePortfolioMarginTrading':True},{'enableUnknownFutureTrade':True},
                                  {'permitsUnknownFutureTransfer':True}])
def test_credit_preflight_blocks_privileged_key_before_deposit_read(change):
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200,json=permissions(**change))
    async def scenario():
        client=BinanceReader('test-key','test-secret',transport=httpx.MockTransport(handler),clock=lambda:1000)
        transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,1,'a'*64,timestamp_ms=1000),'a'*64)[0]
        with pytest.raises(ValueError):await client.credit(transfer)
        await client.close()
    asyncio.run(scenario())
    assert requests==['/sapi/v1/account/apiRestrictions']


@pytest.mark.parametrize('missing',['enableFixApiTrade','enablePortfolioMarginTrading'])
def test_new_required_mutation_flags_cannot_be_missing(missing):
    payload=permissions()
    payload.pop(missing)
    with pytest.raises(ValueError):
        validate_read_only_permissions(payload)


def test_read_only_fix_and_disabled_unknown_permissions_are_allowed():
    validate_read_only_permissions(permissions(enableFixReadOnly=True,enableUnknownFutureTrade=False,
                                              permitsUnknownFutureTransfer=False,ipRestrict=True))
    validate_read_only_permissions(permissions(enableFixReadOnly=False))


@pytest.mark.parametrize('value',[None,0,1,'false','true'])
def test_unknown_permission_value_must_be_boolean_false(value):
    with pytest.raises(ValueError):
        validate_read_only_permissions(permissions(enableUnknownFutureTrade=value))


def test_permission_validation_is_cached_briefly_then_rechecked():
    now=[1000.0]
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('apiRestrictions'):
            return httpx.Response(200,json=permissions(enableWithdrawals=now[0]>=1061))
        return httpx.Response(200,json=[])
    async def scenario():
        client=BinanceReader('test-key','test-secret',transport=httpx.MockTransport(handler),clock=lambda:now[0])
        transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,1,'a'*64,timestamp_ms=1000),'a'*64)[0]
        assert await client.credit(transfer) is None
        assert await client.credit(transfer) is None
        now[0]=1061
        with pytest.raises(ValueError):await client.credit(transfer)
        await client.close()
    asyncio.run(scenario())
    assert sum(path.endswith('apiRestrictions') for path in requests)==2
    assert sum(path.endswith('hisrec') for path in requests)==2


def test_old_deposit_query_respects_binance_ninety_day_window():
    queries=[]
    def handler(request):
        if request.url.path.endswith('apiRestrictions'):
            return httpx.Response(200,json=permissions())
        queries.append(request.url.params)
        return httpx.Response(200,json=[])
    async def scenario():
        client=BinanceReader('test-key','test-secret',transport=httpx.MockTransport(handler),clock=lambda:200*86400)
        transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,1,'a'*64,timestamp_ms=86400000),'a'*64)[0]
        assert await client.credit(transfer) is None
        await client.close()
    asyncio.run(scenario())
    params=queries[0]
    assert 0 <= int(params['endTime'])-int(params['startTime']) < 90*86400*1000


@pytest.mark.parametrize('changes',[{'status':True},{'travelRuleStatus':False},{'travelRuleStatus':None}])
def test_credit_never_treats_booleans_or_unknown_compliance_status_as_confirmed(changes):
    transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,100000000,'a'*64),'a'*64)[0]
    row={'id':'deposit','coin':'USDT','network':'TRX','status':1,'travelRuleStatus':0,
         'address':TEST_ADDRESS,'txId':transfer.txid,'amount':'100.00',**changes}
    assert match_credit([row],transfer) is None


def test_chain_confirmed_payment_is_retried_without_indexer_history(tmp_path):
    book=Ledger(tmp_path/'book.sqlite3',simulation=False)
    row=create_synthetic_invoice(book,tmp_path)
    transfer=transfer_for(row)
    book.observe(transfer)
    class Chain:
        async def candidates(self,*args):raise AssertionError('Known deposit must not depend on history')
        async def transfers(self,txid):return [transfer]
    class Exchange:
        async def credit(self,value):
            assert value==transfer
            return DepositCredit('deposit1',value.txid,value.address,value.amount_atoms)
    watcher=Watcher(book,Chain(),Exchange())
    assert asyncio.run(watcher.scan())==1
    assert book.get(row['id'])['state']=='paid' and not watcher.last_errors


def test_shared_address_is_scanned_once_and_one_exchange_failure_does_not_block_others(tmp_path):
    book=Ledger(tmp_path/'book.sqlite3',simulation=False)
    rows=[create_synthetic_invoice(book,tmp_path,i) for i in range(2)]
    transfers={}
    for i,row in enumerate(rows):
        transfer=transfer_for(row,i)
        transfers[transfer.txid]=transfer
    scans=[]
    class Chain:
        async def candidates(self,address,since):
            scans.append((address,since))
            return list(transfers)
        async def transfers(self,txid):return [transfers[txid]]
    class Exchange:
        fail=True
        async def credit(self,value):
            if value.txid==list(transfers)[0] and self.fail:raise OSError('offline')
            return DepositCredit('deposit-'+value.txid,value.txid,value.address,value.amount_atoms)
    exchange=Exchange()
    watcher=Watcher(book,Chain(),exchange)
    asyncio.run(watcher.scan())
    assert len(scans)==1
    assert book.get(rows[0]['id'])['state']=='chain_confirmed'
    assert book.get(rows[1]['id'])['state']=='paid'
    assert watcher.last_errors==[{'stage':'deposit','error_type':'OSError'}]
    exchange.fail=False
    asyncio.run(watcher.scan())
    assert all(book.get(row['id'])['state']=='paid' for row in rows)
    assert not watcher.last_errors


def test_one_address_history_failure_does_not_block_another_address(tmp_path):
    book=Ledger(tmp_path/'book.sqlite3',simulation=False)
    for value in (41,42):book.add_address(address_from_bytes(b'\x41'+bytes([value])*20))
    rows=[create_synthetic_invoice(book,tmp_path,i,shared=False) for i in range(2)]
    transfer=transfer_for(rows[1])
    class Chain:
        async def candidates(self,address,since):
            if address==rows[0]['address']:raise OSError('offline')
            return [transfer.txid]
        async def transfers(self,txid):return [transfer]
    watcher=Watcher(book,Chain())
    asyncio.run(watcher.scan())
    assert book.get(rows[0]['id'])['state']=='awaiting_payment'
    assert book.get(rows[1]['id'])['state']=='chain_confirmed'
    assert watcher.last_errors==[{'stage':'history','error_type':'OSError'}]

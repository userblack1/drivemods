"""Old invoices/indexer limits must not strand new customer payments."""
import asyncio
from collections import Counter
import hashlib
from uuid import uuid4

import httpx
import pytest

from crypto_checkout.binance import DepositCredit
from crypto_checkout.demo import TEST_ADDRESS, synthetic_receipt
from crypto_checkout.ledger import Ledger
from crypto_checkout.runtime import ConcurrentWatcher, RateLimitedExchange
from crypto_checkout.tron import HistoryPageLimit, TronReader, parse_solid_receipt
from crypto_checkout.watcher import Watcher


def invoice(book,folder,index,created_ms):
    ident=uuid4().hex
    path=folder/ident/'device.id.bin'
    path.parent.mkdir()
    path.write_bytes(f'SYNTHETIC DEVICE {index}'.encode())
    return book.invoice(ident,1000+index,'full',path,hashlib.sha256(path.read_bytes()).hexdigest(),1000,
                        shared_address=TEST_ADDRESS,allow_amount_suffix=True,now_ms=created_ms)


def transfer(row,index):
    txid=f'{index+1:064x}'
    return parse_solid_receipt(synthetic_receipt(row['address'],row['amount_atoms'],txid,
                              timestamp_ms=row['created_ms']+1),txid)[0]


class Exchange:
    async def credit(self,value):
        return DepositCredit('SYNTHETIC-'+value.txid,value.txid,value.address,value.amount_atoms)


def make_watcher(kind,book,chain,now,exchange=None,**options):
    if kind is ConcurrentWatcher:
        exchange=RateLimitedExchange(exchange or Exchange(),minimum_interval=0)
    return kind(book,chain,exchange or Exchange(),clock=lambda:now[0]/1000,**options)


@pytest.mark.parametrize('kind',[Watcher,ConcurrentWatcher])
def test_dense_month_old_history_is_bisected_and_fresh_customer_is_paid_first(tmp_path,kind):
    book=Ledger(tmp_path/'live.sqlite3',simulation=False)
    first=invoice(book,tmp_path,1,1_000_000)
    now=[first['created_ms']+30*86400*1000]
    fresh=invoice(book,tmp_path,2,now[0]-100)
    old_tx,new_tx=transfer(first,1),transfer(fresh,2)
    class Chain:
        windows=[]
        async def candidates(self,address,start,*,max_timestamp):
            self.windows.append((start,max_timestamp))
            if start==first['created_ms'] and max_timestamp-start>=3600*1000:
                raise HistoryPageLimit('synthetic dense interval')
            return [value.txid for value in (old_tx,new_tx) if start<=value.timestamp_ms<=max_timestamp]
        async def transfers(self,txid):return [old_tx if txid==old_tx.txid else new_tx]
    chain=Chain()
    watcher=make_watcher(kind,book,chain,now)
    asyncio.run(watcher.scan())
    assert book.get(fresh['id'])['state']=='paid'
    assert book.get(first['id'])['state']=='paid'
    assert chain.windows[0][0]>first['expires_ms']  # Current tail precedes the backfill.
    assert any(end-start<3600*1000 and start==first['created_ms'] for start,end in chain.windows)
    with book.connection() as db:
        cursor=db.execute('SELECT cursor_ms FROM chain_scan_windows').fetchone()[0]
    assert cursor>first['created_ms']


@pytest.mark.parametrize('kind',[Watcher,ConcurrentWatcher])
def test_receipt_failure_keeps_cursor_and_restart_skips_only_already_verified_receipt(tmp_path,kind):
    book=Ledger(tmp_path/'live.sqlite3',simulation=False)
    rows=[invoice(book,tmp_path,index,1_000_000) for index in range(2)]
    values=[transfer(row,index) for index,row in enumerate(rows)]
    now=[1_001_000]
    class Chain:
        calls=Counter()
        fail=True
        async def candidates(self,address,start,*,max_timestamp):return [value.txid for value in values]
        async def transfers(self,txid):
            self.calls[txid]+=1
            if txid==values[1].txid and self.fail:raise TimeoutError('SYNTHETIC RPC OUTAGE')
            return [next(value for value in values if value.txid==txid)]
    chain=Chain()
    watcher=make_watcher(kind,book,chain,now)
    asyncio.run(watcher.scan())
    assert book.get(rows[0]['id'])['state']=='paid'
    assert book.get(rows[1]['id'])['state']=='awaiting_payment'
    with book.connection() as db:
        assert db.execute('SELECT cursor_ms FROM chain_scan_windows').fetchone()[0]==1_000_000
    book=Ledger(book.path,simulation=False)
    chain.fail=False
    watcher=make_watcher(kind,book,chain,now)
    asyncio.run(watcher.scan())
    assert all(book.get(row['id'])['state']=='paid' for row in rows)
    assert chain.calls[values[0].txid]==1 and chain.calls[values[1].txid]==2
    with book.connection() as db:
        assert db.execute('SELECT cursor_ms FROM chain_scan_windows').fetchone()[0]==now[0]+1


def test_pending_exchange_credit_ignores_receipt_cache_and_indexer_after_restart(tmp_path):
    book=Ledger(tmp_path/'live.sqlite3',simulation=False)
    row=invoice(book,tmp_path,1,1_000_000)
    value=transfer(row,1)
    now=[1_001_000]
    class Chain:
        failed=False
        async def candidates(self,*args,**kwargs):
            if self.failed:raise AssertionError('Known payment must not query the indexer')
            return [value.txid]
        async def transfers(self,txid):return [value]
    class FailingExchange(Exchange):
        async def credit(self,value):raise TimeoutError('SYNTHETIC EXCHANGE OUTAGE')
    chain=Chain()
    asyncio.run(make_watcher(Watcher,book,chain,now,FailingExchange()).scan())
    assert book.get(row['id'])['state']=='chain_confirmed'
    book=Ledger(book.path,simulation=False)
    chain.failed=True
    watcher=make_watcher(Watcher,book,chain,now)
    asyncio.run(watcher.scan())
    assert book.get(row['id'])['state']=='paid' and not watcher.last_errors


def test_empty_receipt_is_not_cached_and_delayed_indexer_data_is_revisited(tmp_path):
    book=Ledger(tmp_path/'live.sqlite3',simulation=False)
    row=invoice(book,tmp_path,1,1_000_000)
    value=transfer(row,1)
    now=[1_001_000]
    class Chain:
        available=False
        calls=0
        async def candidates(self,*args,**kwargs):return [value.txid]
        async def transfers(self,txid):
            self.calls+=1
            return [value] if self.available else []
    chain=Chain()
    asyncio.run(make_watcher(Watcher,book,chain,now).scan())
    assert book.get(row['id'])['state']=='awaiting_payment'
    book=Ledger(book.path,simulation=False)
    now[0]+=1000
    chain.available=True
    asyncio.run(make_watcher(Watcher,book,chain,now).scan())
    assert book.get(row['id'])['state']=='paid' and chain.calls==2


def test_history_network_failure_never_advances_durable_window(tmp_path):
    book=Ledger(tmp_path/'live.sqlite3',simulation=False)
    row=invoice(book,tmp_path,1,1_000_000)
    now=[1_001_000]
    class Chain:
        async def candidates(self,*args,**kwargs):raise OSError('SYNTHETIC OUTAGE')
    watcher=make_watcher(Watcher,book,Chain(),now)
    asyncio.run(watcher.scan())
    with book.connection() as db:
        assert db.execute('SELECT cursor_ms FROM chain_scan_windows').fetchone()[0]==row['created_ms']
    assert watcher.last_errors==[{'stage':'history','error_type':'OSError'}]


def test_tron_reader_sends_fixed_upper_timestamp_and_page_cap_is_explicit():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.params['min_timestamp']=='1000000'
        assert request.url.params['max_timestamp']=='1001000'
        return httpx.Response(200,json={'success':True,'data':[{'transaction_id':'a'*64}],
                            'meta':{'links':{'next':'not-followed'},'fingerprint':'next'}})
    async def scenario():
        reader=TronReader(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(HistoryPageLimit):
                await reader.candidates(TEST_ADDRESS,1_000_000,max_timestamp=1_001_000,max_pages=1)
        finally:await reader.close()
    asyncio.run(scenario())
    assert len(calls)==1 and calls[0].url.host=='api.trongrid.io'

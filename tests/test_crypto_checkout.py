import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from crypto_checkout.binance import BinanceReader,DepositCredit,match_credit
from crypto_checkout.demo import (TEST_ADDRESS,SimulatedDelivery,SimulatedStore,confirm_synthetic_payment,
                                 create_synthetic_invoice,run_demo,synthetic_receipt)
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger
from crypto_checkout.tron import TronReader,address_bytes,address_from_bytes,parse_solid_receipt
from crypto_checkout.watcher import Watcher
from crypto_checkout.observer import Observations,cycle


@pytest.fixture
def app(tmp_path):
    book=Ledger(tmp_path/'checkout.sqlite3',simulation=True)
    store,delivery=SimulatedStore(),SimulatedDelivery()
    return book,store,delivery,Engine(book,store,delivery)


def paid(app,tmp_path):
    book,_,_,_=app
    row=create_synthetic_invoice(book,tmp_path)
    confirm_synthetic_payment(book,row)
    return book.get(row['id'])


async def drain(engine,order_id):
    for _ in range(8):
        await engine.step(order_id)


def test_tron_base58check_rejects_typo_and_wrong_network():
    assert address_from_bytes(address_bytes(TEST_ADDRESS))==TEST_ADDRESS
    for value in (TEST_ADDRESS[:-1]+'0','0x'+'a'*40,'T'+'a'*33):
        with pytest.raises(ValueError):address_bytes(value)


@pytest.mark.parametrize('mutation', ['failed','reverted','wrong_token','wrong_signature'])
def test_fake_or_failed_receipts_never_confirm_payment(mutation):
    raw=synthetic_receipt(TEST_ADDRESS,100_010_000,'a'*64)
    if mutation=='failed':raw['result']='FAILED'
    if mutation=='reverted':raw['receipt']['result']='REVERT'
    if mutation=='wrong_token':raw['log'][0]['address']='aa'*20
    if mutation=='wrong_signature':raw['log'][0]['topics'][0]='b'*64
    assert parse_solid_receipt(raw,'a'*64)==[]


def test_pending_receipt_and_wrong_txid_are_not_success():
    assert parse_solid_receipt({},'a'*64)==[]
    raw=synthetic_receipt(TEST_ADDRESS,1,'b'*64)
    with pytest.raises(ValueError):parse_solid_receipt(raw,'a'*64)


def test_invoice_amounts_are_unique_concurrently_and_after_restart(tmp_path):
    book=Ledger(tmp_path/'x.sqlite3',simulation=True)
    with ThreadPoolExecutor(max_workers=10) as executor:
        rows=list(executor.map(lambda i:create_synthetic_invoice(book,tmp_path,i),range(10)))
    assert len({r['amount_atoms'] for r in rows})==10
    reopened=Ledger(book.path,simulation=True)
    another=create_synthetic_invoice(reopened,tmp_path,11)
    assert another['amount_atoms'] not in {r['amount_atoms'] for r in rows}


def test_unique_cent_capacity_exhaustion_stops_instead_of_reusing_old_invoices(tmp_path):
    book=Ledger(tmp_path/'x.sqlite3',simulation=True)
    rows=[create_synthetic_invoice(book,tmp_path,i) for i in range(99)]
    book.transition(rows[0]['id'],'awaiting_payment','expired')
    with pytest.raises(ValueError,match='capacity exhausted'):
        create_synthetic_invoice(book,tmp_path,100)


def test_exact_amounts_use_separate_addresses_and_never_reuse_them(tmp_path):
    book=Ledger(tmp_path/'x.sqlite3',simulation=True)
    for i in range(2):book.add_address(address_from_bytes(b'\x41'+bytes([40+i])*20))
    first=create_synthetic_invoice(book,tmp_path,1,shared=False)
    second=create_synthetic_invoice(book,tmp_path,2,shared=False)
    assert first['amount_atoms']==second['amount_atoms']==100_000_000
    assert first['address']!=second['address']
    with pytest.raises(ValueError,match='No unused'):
        create_synthetic_invoice(book,tmp_path,3,shared=False)


@pytest.mark.parametrize('kind',['wrong_amount','wrong_address','before_invoice','late_payment'])
def test_bad_or_late_transfer_never_starts_purchase(app,tmp_path,kind):
    book,store,delivery,engine=app
    row=create_synthetic_invoice(book,tmp_path)
    raw=synthetic_receipt(row['address'],row['amount_atoms'],'a'*64,timestamp_ms=row['created_ms']+1)
    if kind=='wrong_amount':raw['log'][0]['data']=f"{row['amount_atoms']-1:064x}"
    if kind=='wrong_address':raw['log'][0]['topics'][2]='0'*24+'44'*20
    if kind=='before_invoice':raw['blockTimeStamp']=row['created_ms']-1
    if kind=='late_payment':raw['blockTimeStamp']=row['expires_ms']+1
    book.observe(parse_solid_receipt(raw,'a'*64)[0])
    asyncio.run(drain(engine,row['id']))
    assert store.charges==0 and delivery.files==[]


def test_onchain_payment_alone_cannot_spend_card(app,tmp_path):
    book,store,_,engine=app
    row=create_synthetic_invoice(book,tmp_path)
    raw=synthetic_receipt(row['address'],row['amount_atoms'],'a'*64,timestamp_ms=row['created_ms']+1)
    book.observe(parse_solid_receipt(raw,'a'*64)[0])
    assert book.get(row['id'])['state']=='chain_confirmed'
    asyncio.run(drain(engine,row['id']))
    assert store.charges==0


@pytest.mark.parametrize('field,value',[('status',0),('status',6),('status',8),('coin','FAKEUSDT'),
                         ('network','ETH'),('travelRuleStatus',1),('txId','b'*64),
                         ('address','wrong'),('amount','99.99')])
def test_binance_credit_requires_correct_asset_network_amount_and_available_status(field,value):
    transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,100_010_000,'a'*64),'a'*64)[0]
    row={'id':'deposit1','coin':'USDT','network':'TRX','status':1,'travelRuleStatus':0,
         'txId':transfer.txid,'address':TEST_ADDRESS,'amount':'100.01'}
    assert match_credit([row],transfer)
    row[field]=value
    assert match_credit([row],transfer) is None


def test_one_exchange_credit_cannot_be_used_for_another_order(app,tmp_path):
    book,_,_,_=app
    first=paid(app,tmp_path)
    assert book.confirm_exchange(first['id'],DepositCredit('DEMO-DEPOSIT-1',first['txid'],first['address'],first['amount_atoms'])) is False
    second=create_synthetic_invoice(book,tmp_path,2)
    with pytest.raises(ValueError):
        book.confirm_exchange(second['id'],DepositCredit('DEMO-DEPOSIT-1',first['txid'],first['address'],first['amount_atoms']))


def test_multiple_workers_and_replayed_payment_make_one_purchase(app,tmp_path):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    async def scenario():
        for _ in range(8):await asyncio.gather(*(engine.step(row['id']) for _ in range(10)))
    asyncio.run(scenario())
    assert book.get(row['id'])['state']=='delivered'
    assert store.uploads==store.charges==len(delivery.files)==1
    assert delivery.files[0][1]=='DEMO-NOT-A-LICENSE.txt'


def test_lost_charge_response_is_reconciled_without_another_charge(app,tmp_path):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    store.timeout_after_charge=True
    asyncio.run(drain(engine,row['id']))
    assert book.get(row['id'])['state']=='purchase_review' and store.charges==1
    assert not delivery.files
    asyncio.run(engine.reconcile(row['id']))
    asyncio.run(drain(engine,row['id']))
    assert book.get(row['id'])['state']=='delivered' and store.charges==1


def test_visa_authentication_is_not_bypassed_or_charged_again(app,tmp_path):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    store.authentication=True
    asyncio.run(drain(engine,row['id']))
    assert book.get(row['id'])['state']=='action_required'
    assert store.charges==1 and not delivery.files


@pytest.mark.parametrize('change', ['price','plan','device','currency'])
def test_wrong_store_order_cannot_spend_card(app,tmp_path,change):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    prepare=store.prepare
    async def wrong(invoice):
        order=await prepare(invoice)
        return replace(order,**{'price':{'total_cents':9001},'plan':{'plan':'eco'},
                               'device':{'device_sha256':'a'*64},'currency':{'currency':'EUR'}}[change])
    store.prepare=wrong
    asyncio.run(drain(engine,row['id']))
    assert store.charges==0 and not delivery.files
    assert book.get(row['id'])['state']=='purchase_review'


def test_changed_device_id_does_not_leave_computer(app,tmp_path):
    book,store,_,engine=app
    row=paid(app,tmp_path)
    Path(row['source_path']).write_bytes(b'TAMPERED')
    asyncio.run(drain(engine,row['id']))
    assert store.uploads==store.charges==0


def test_telegram_outage_only_retries_delivery(app,tmp_path):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    delivery.fail=True
    asyncio.run(drain(engine,row['id']))
    assert store.charges==1 and book.get(row['id'])['state']=='ready'
    delivery.fail=False
    asyncio.run(drain(engine,row['id']))
    assert store.charges==1 and len(delivery.files)==1


def test_live_and_simulation_are_isolated(app,tmp_path):
    book,store,delivery,engine=app
    row=paid(app,tmp_path)
    store.simulation=False
    with pytest.raises(ValueError):asyncio.run(engine.step(row['id']))
    with pytest.raises(ValueError):Ledger(book.path,simulation=False)


def test_read_only_binance_client_signs_only_allowed_get_requests():
    requests=[]
    def handler(request):
        requests.append(request)
        query=request.url.query.decode()
        unsigned,signature=query.rsplit('&signature=',1)
        assert hmac.compare_digest(signature,hmac.new(b'test-secret',unsigned.encode(),hashlib.sha256).hexdigest())
        assert request.method=='GET' and request.headers['X-MBX-APIKEY']=='test-key'
        return httpx.Response(200,json=[])
    async def scenario():
        client=BinanceReader('test-key','test-secret',transport=httpx.MockTransport(handler),clock=lambda:2000)
        transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,1,'a'*64,timestamp_ms=1000),'a'*64)[0]
        assert await client.credit(transfer) is None
        with pytest.raises(ValueError):await client._get('/sapi/v1/capital/withdraw/apply',{})
        await client.close()
    asyncio.run(scenario())
    assert len(requests)==1


def test_tron_client_uses_solidified_receipt_and_paginates_without_following_urls():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('gettransactioninfobyid'):
            assert request.url.path.startswith('/walletsolidity/')
            return httpx.Response(200,json=synthetic_receipt(TEST_ADDRESS,1,'a'*64))
        if 'fingerprint' not in request.url.params:
            return httpx.Response(200,json={'success':True,'data':[{'transaction_id':'a'*64}],
                        'meta':{'links':{'next':'https://untrusted.invalid/'},'fingerprint':'next-page'}})
        return httpx.Response(200,json={'success':True,'data':[{'transaction_id':'b'*64}],'meta':{}})
    async def scenario():
        client=TronReader(transport=httpx.MockTransport(handler))
        assert await client.candidates(TEST_ADDRESS,1000)==['a'*64,'b'*64]
        assert len(await client.transfers('a'*64))==1
        await client.close()
    asyncio.run(scenario())
    assert all(r.url.host=='api.trongrid.io' for r in requests)


def test_end_to_end_ten_customers_and_no_real_money(tmp_path):
    result=asyncio.run(run_demo(tmp_path/'demo'))
    assert result['customers']==result['simulated_card_charges']==result['delivered_test_files']==10
    assert result['real_card_charges']==result['real_chain_payments']==result['real_licenses']==0


def test_observer_stores_real_format_once_without_approving_an_order(tmp_path):
    transfer=parse_solid_receipt(synthetic_receipt(TEST_ADDRESS,1,'a'*64),'a'*64)[0]
    class FakeReader:
        async def candidates(self,address,since):return ['a'*64]
        async def transfers(self,txid):return [transfer]
    store=Observations(tmp_path,TEST_ADDRESS)
    asyncio.run(cycle(FakeReader(),store))
    health=asyncio.run(cycle(FakeReader(),store))
    assert health['verified_events']==1 and health['ok']
    assert health['payment_confirmed_for_orders'] is health['license_purchase_enabled'] is False
    assert json.loads((tmp_path/'health.json').read_text())['mode']=='observe_only'


def test_observer_rejects_other_receiving_address_in_existing_data(tmp_path):
    Observations(tmp_path,TEST_ADDRESS)
    with pytest.raises(ValueError):
        Observations(tmp_path,address_from_bytes(b'\x41'+b'\x34'*20))


def test_recovery_never_resumes_an_uncertain_card_charge(app,tmp_path):
    book,store,_,engine=app
    row=paid(app,tmp_path)
    book.transition(row['id'],'paid','charging')
    book.recover()
    asyncio.run(drain(engine,row['id']))
    assert book.get(row['id'])['state']=='purchase_review' and store.charges==0

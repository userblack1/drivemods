"""Durable multi-customer binding and delivery, using synthetic funds/files only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from crypto_checkout.binance import DepositCredit
from crypto_checkout.demo import TEST_ADDRESS, SimulatedStore, synthetic_receipt
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger
from crypto_checkout.tron import parse_solid_receipt


def issue(book, folder, index, *, user_id=None, plan='full', content=None,
          now_ms=1_000_000, request_key=None, reuse_existing=False):
    invoice_id=uuid4().hex
    source=folder/invoice_id/'device.id.bin'
    source.parent.mkdir(parents=True)
    source.write_bytes(content if content is not None else f'SYNTHETIC DEVICE {index}'.encode())
    return book.invoice(invoice_id,1000+index if user_id is None else user_id,plan,source,
                        hashlib.sha256(source.read_bytes()).hexdigest(),1000,
                        shared_address=TEST_ADDRESS,allow_amount_suffix=True,now_ms=now_ms,
                        request_key=request_key,reuse_existing=reuse_existing)


def transfer_for(row, index, **changes):
    txid=f'{index+1:064x}'
    transfer=parse_solid_receipt(synthetic_receipt(row['address'],row['amount_atoms'],txid,
                   timestamp_ms=row['created_ms']+1),txid)[0]
    return replace(transfer,**changes)


def credit_for(row, transfer, index):
    return DepositCredit(f'SYNTHETIC-CREDIT-{index}',transfer.txid,row['address'],row['amount_atoms'])


class CapturingDelivery:
    simulation=True

    def __init__(self):
        self.sent=[]

    async def send(self, invoice, path, filename):
        await asyncio.sleep(0)
        self.sent.append((invoice['id'],invoice['user_id'],Path(path),Path(path).read_bytes()))
        return 'SYNTHETIC-MESSAGE-'+invoice['id']


def test_twenty_concurrent_customers_keep_exact_payment_and_own_license_after_restart(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    with ThreadPoolExecutor(max_workers=20) as pool:
        rows=list(pool.map(lambda index:issue(book,tmp_path,index,
                      plan='full' if index%2 else 'eco',request_key='request:'+str(index)),range(20)))
    assert len({row['id'] for row in rows})==len({row['source_path'] for row in rows})==20
    assert len({(row['address'],row['amount_atoms']) for row in rows})==20
    assert len({row['source_sha'] for row in rows})==20

    transfers=[transfer_for(row,index) for index,row in enumerate(rows)]
    def record(index):
        row,transfer=rows[index],transfers[index]
        result=book.observe(transfer)
        book.confirm_exchange(row['id'],credit_for(row,transfer,index))
        return result
    with ThreadPoolExecutor(max_workers=20) as pool:
        results=list(pool.map(record,[index for index in range(20) for _ in range(3)]))
    assert results.count('confirmed')==20
    assert results.count('duplicate')==40
    assert all(book.get(row['id'])['state']=='paid' for row in rows)

    book=Ledger(book.path,simulation=True)
    book.recover()
    store,delivery=SimulatedStore(),CapturingDelivery()
    engine=Engine(book,store,delivery)
    async def complete():
        for _ in range(8):
            await asyncio.gather(*(engine.step(row['id']) for row in rows for _ in range(3)))
    asyncio.run(complete())
    assert store.uploads==store.charges==len(delivery.sent)==20
    assert len({path for _,_,path,_ in delivery.sent})==20
    delivered={invoice_id:(user_id,content) for invoice_id,user_id,_,content in delivery.sent}
    for row in rows:
        assert delivered[row['id']]==(row['user_id'],
            ('DEMO ONLY - NOT A VALID DEVICE LICENSE\nDEMO-'+row['id']).encode())
        assert book.get_for_user(row['id'],row['user_id'])['state']=='delivered'
        assert book.observe(transfers[rows.index(row)])=='duplicate'
    asyncio.run(complete())
    assert store.charges==len(delivery.sent)==20


def test_concurrent_identical_customer_actions_share_one_invoice_and_all_request_keys_survive(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    with ThreadPoolExecutor(max_workers=20) as pool:
        rows=list(pool.map(lambda index:issue(book,tmp_path,index,user_id=42,content=b'SAME SYNTHETIC DEVICE',
                   request_key='message:'+str(index),reuse_existing=True),range(20)))
    assert len({row['id'] for row in rows})==1
    assert len(book.list_by_user(42))==1
    book=Ledger(book.path,simulation=True)
    # A retry remains the same order even if its invoice deadline has passed.
    for index in range(20):
        retry=issue(book,tmp_path,100+index,user_id=42,content=b'SAME SYNTHETIC DEVICE',
                    request_key='message:'+str(index),now_ms=99_000_000)
        assert retry['id']==rows[0]['id']
    assert len(book.list_by_user(42))==1
    with pytest.raises(ValueError,match='identity conflict'):
        issue(book,tmp_path,200,user_id=42,content=b'DIFFERENT SYNTHETIC DEVICE',request_key='message:1')
    with pytest.raises(ValueError,match='identity conflict'):
        issue(book,tmp_path,201,user_id=42,plan='eco',content=b'SAME SYNTHETIC DEVICE',request_key='message:1')


def test_shared_upload_path_cannot_overwrite_another_customers_output(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    source=tmp_path/'shared-device.id.bin'
    source.write_bytes(b'SYNTHETIC SHARED DEVICE')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    rows=[book.invoice(uuid4().hex,42+index,'full',source,digest,1000,
                       shared_address=TEST_ADDRESS,allow_amount_suffix=True,now_ms=1_000_000)
          for index in range(2)]
    for index,row in enumerate(rows):
        transfer=transfer_for(row,index)
        book.observe(transfer)
        book.confirm_exchange(row['id'],credit_for(row,transfer,index))
    store,delivery=SimulatedStore(),CapturingDelivery()
    engine=Engine(book,store,delivery)
    async def complete():
        for _ in range(8):await asyncio.gather(*(engine.step(row['id']) for row in rows))
    asyncio.run(complete())
    assert store.charges==len(delivery.sent)==2
    assert len({path for _,_,path,_ in delivery.sent})==2
    for invoice_id,user_id,path,content in delivery.sent:
        assert path.read_bytes()==content==('DEMO ONLY - NOT A VALID DEVICE LICENSE\nDEMO-'+invoice_id).encode()
        assert book.get_for_user(invoice_id,user_id)['delivery_ref']=='SYNTHETIC-MESSAGE-'+invoice_id


def test_twenty_retries_of_one_request_need_no_source_reuse_flag(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    with ThreadPoolExecutor(max_workers=20) as pool:
        rows=list(pool.map(lambda index:issue(book,tmp_path,index,user_id=42,
                    content=b'SYNTHETIC REQUEST',request_key='telegram:42:501'),range(20)))
    assert len({row['id'] for row in rows})==1
    assert len(book.list_by_user(42))==1
    assert book.audit(rows[0]['id'])==['invoice_created']


def test_delivered_device_reuse_returns_existing_license_without_new_payment_identity(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    first=issue(book,tmp_path,1,user_id=42,content=b'SAME',request_key='original')
    book.transition(first['id'],'awaiting_payment','sending')
    book.mark_delivered(first['id'],'message-1')
    repeated=issue(book,tmp_path,2,user_id=42,content=b'SAME',request_key='repeated',
                   reuse_existing=True,now_ms=first['expires_ms']+1)
    assert repeated['id']==first['id']
    assert repeated['state']=='delivered' and repeated['delivery_ref']=='message-1'
    assert len(book.list_by_user(42))==1


def test_source_reuse_never_crosses_customer_or_plan(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    first=issue(book,tmp_path,1,user_id=42,content=b'SAME',reuse_existing=True,request_key='same-key')
    second=issue(book,tmp_path,2,user_id=43,content=b'SAME',reuse_existing=True,request_key='same-key')
    third=issue(book,tmp_path,3,user_id=42,plan='eco',content=b'SAME',reuse_existing=True)
    assert len({first['id'],second['id'],third['id']})==3
    assert {row['id'] for row in book.list_by_user(42)}=={first['id'],third['id']}
    assert len(book.list_by_user(43))==1
    with pytest.raises(ValueError,match='Unknown invoice'):
        book.get_for_user(first['id'],43)


def test_expired_invoice_cents_are_never_reassigned_and_late_credit_cannot_pay_fresh_order(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    first=issue(book,tmp_path,1,user_id=42,content=b'SAME',reuse_existing=True,request_key='old')
    fresh=issue(book,tmp_path,2,user_id=42,content=b'SAME',reuse_existing=True,
                now_ms=first['expires_ms']+1,request_key='fresh')
    assert fresh['id']!=first['id'] and fresh['amount_atoms']!=first['amount_atoms']
    late=transfer_for(first,1,timestamp_ms=first['expires_ms']+1)
    assert book.observe(late)=='late_payment_review'
    assert book.get(first['id'])['state']=='payment_review'
    assert book.get(fresh['id'])['state']=='awaiting_payment'
    with pytest.raises(ValueError):
        book.confirm_exchange(first['id'],credit_for(first,late,1))
    assert book.get(fresh['id'])['txid'] is None


@pytest.mark.parametrize('kind',['one_atom_short','before_invoice','before_block','late'])
def test_bad_payment_does_not_start_any_customers_purchase(tmp_path,kind):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    rows=[issue(book,tmp_path,index) for index in range(10)]
    row=rows[5]
    changes={'one_atom_short':{'amount_atoms':row['amount_atoms']-1},
             'before_invoice':{'timestamp_ms':row['created_ms']-1},
             'before_block':{'block':row['start_block']-1},
             'late':{'timestamp_ms':row['expires_ms']+1}}[kind]
    transfer=transfer_for(row,1,**changes)
    book.observe(transfer)
    for target in rows:
        with pytest.raises(ValueError):book.confirm_exchange(target['id'],credit_for(row,transfer,1))
    assert not book.list_by_states(('paid','chain_confirmed'))


def test_one_binance_credit_is_never_bound_to_two_distinct_chain_transfers(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    first,second=[issue(book,tmp_path,index) for index in range(2)]
    one,two=transfer_for(first,1),transfer_for(second,2)
    book.observe(one)
    book.observe(two)
    book.confirm_exchange(first['id'],credit_for(first,one,7))
    with pytest.raises(ValueError,match='already bound'):
        book.confirm_exchange(second['id'],credit_for(second,two,7))
    assert book.get(first['id'])['state']=='paid'
    assert book.get(second['id'])['state']=='chain_confirmed'


def test_capacity_remains_finite_after_restarts_and_eco_has_separate_cent_pool(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    rows=[issue(book,tmp_path,index) for index in range(99)]
    book.transition(rows[0]['id'],'awaiting_payment','expired')
    book=Ledger(book.path,simulation=True)
    with pytest.raises(ValueError,match='capacity exhausted'):
        issue(book,tmp_path,100)
    eco=issue(book,tmp_path,101,plan='eco')
    assert eco['amount_atoms']==70_010_000
    assert len(book.list_by_states(('awaiting_payment','expired'),limit=1000))==100


def test_work_lists_have_stable_order_and_customer_lists_are_filtered(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    rows=[issue(book,tmp_path,index,user_id=42 if index%2 else 43,now_ms=1_000_000+index) for index in range(6)]
    book.transition(rows[3]['id'],'awaiting_payment','paid')
    assert [row['id'] for row in book.list_by_states(('awaiting_payment',),limit=2)]==[rows[0]['id'],rows[1]['id']]
    assert [row['id'] for row in book.list_by_user(42,states=('awaiting_payment',))]==[rows[5]['id'],rows[1]['id']]
    assert book.list_by_user(99)==[]


@pytest.mark.parametrize('states,limit',[(None,10),('paid',10),((),10),(('not-a-state',),10),
                                       (('paid',),0),(('paid',),10001),(('paid',),True)])
def test_invalid_worker_list_cannot_become_an_unbounded_or_arbitrary_query(tmp_path,states,limit):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    with pytest.raises(ValueError):book.list_by_states(states,limit=limit)


def test_delivery_acknowledgement_is_durable_idempotent_and_state_guarded(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    row=issue(book,tmp_path,1)
    with pytest.raises(ValueError):book.mark_delivered(row['id'],'message-123')
    book.transition(row['id'],'awaiting_payment','sending')
    with pytest.raises(ValueError):book.mark_delivered(row['id'],'')
    assert book.mark_delivered(row['id'],'message-123')
    book=Ledger(book.path,simulation=True)
    book.recover()
    assert book.get(row['id'])['delivery_ref']=='message-123'
    assert book.get(row['id'])['state']=='delivered'
    assert book.mark_delivered(row['id'],'message-123') is False
    with pytest.raises(ValueError):book.mark_delivered(row['id'],'message-456')
    assert book.audit(row['id']).count('delivered')==1


def test_restart_never_repeats_an_uncertain_card_operation_and_audits_recovery(tmp_path):
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    states=('preparing','charging','inspecting','downloading','sending')
    rows=[issue(book,tmp_path,index) for index in range(len(states))]
    for row,state in zip(rows,states):book.transition(row['id'],'awaiting_payment',state)
    book=Ledger(book.path,simulation=True)
    book.recover()
    expected=('purchase_review','purchase_review','purchase_review','download_pending','ready')
    for row,before,after in zip(rows,states,expected):
        assert book.get(row['id'])['state']==after
        assert book.audit(row['id'])[-1]=='restart_during_'+before
    book.recover()
    for row,before in zip(rows,states):
        assert book.audit(row['id']).count('restart_during_'+before)==1


def test_legacy_ledger_gets_only_additive_migration_and_keeps_existing_rows(tmp_path):
    path=tmp_path/'orders.sqlite3'
    book=Ledger(path,simulation=True)
    row=issue(book,tmp_path,1)
    # Drop only the new nullable column to exercise upgrading the previous schema.
    with sqlite3.connect(path) as db:db.execute('ALTER TABLE invoices DROP COLUMN delivery_ref')
    with ThreadPoolExecutor(max_workers=10) as pool:
        reopened=list(pool.map(lambda _:Ledger(path,simulation=True),range(10)))
    for item in reopened:
        actual=item.get(row['id'])
        assert actual['id']==row['id'] and actual['amount_atoms']==row['amount_atoms']
        assert actual['delivery_ref'] is None
        assert item.audit(row['id'])==['invoice_created']

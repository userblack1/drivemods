"""Read interruptions may resume; ambiguous charge interruptions never recharge."""
import asyncio

import pytest

from crypto_checkout.demo import SimulatedDelivery, SimulatedStore, create_synthetic_invoice, confirm_synthetic_payment
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger


class ProcessCrash(BaseException):
    pass


def prepared(tmp_path):
    book=Ledger(tmp_path/'checkout.sqlite3',simulation=True)
    store,delivery=SimulatedStore(),SimulatedDelivery()
    engine=Engine(book,store,delivery)
    row=create_synthetic_invoice(book,tmp_path)
    confirm_synthetic_payment(book,row)
    asyncio.run(engine.step(row['id']))
    assert book.get(row['id'])['state']=='prepared'
    return book,store,delivery,engine,row


async def drain(engine,invoice_id):
    for _ in range(8):await engine.step(invoice_id)


def test_crash_after_inspection_read_before_charging_resumes_exactly_one_charge(tmp_path):
    book,store,delivery,engine,row=prepared(tmp_path)
    inspect=store.inspect
    async def crash(reference):
        await inspect(reference)
        raise ProcessCrash()
    store.inspect=crash
    with pytest.raises(ProcessCrash):asyncio.run(engine.step(row['id']))
    pending=book.get(row['id'])
    assert pending['state']=='inspecting' and pending['inspection_origin']=='prepared'
    assert store.charges==0
    book=Ledger(book.path,simulation=True)
    book.recover()
    assert book.get(row['id'])['state']=='prepared'
    store.inspect=inspect
    asyncio.run(drain(Engine(book,store,delivery),row['id']))
    assert store.uploads==store.charges==len(delivery.files)==1
    assert book.get(row['id'])['state']=='delivered'


@pytest.mark.parametrize('charged',[False,True])
def test_crash_after_charging_reservation_never_retries_ambiguous_payment(tmp_path,charged):
    book,store,delivery,engine,row=prepared(tmp_path)
    pay=store.pay
    async def crash(reference,idempotency_key):
        assert book.get(row['id'])['state']=='charging'
        if charged:await pay(reference,idempotency_key)
        raise ProcessCrash()
    store.pay=crash
    with pytest.raises(ProcessCrash):asyncio.run(engine.step(row['id']))
    assert book.get(row['id'])['state']=='charging'
    book=Ledger(book.path,simulation=True)
    book.recover()
    store.pay=pay
    engine=Engine(book,store,delivery)
    asyncio.run(drain(engine,row['id']))
    assert book.get(row['id'])['state']=='purchase_review'
    assert store.charges==int(charged) and not delivery.files
    if charged:
        assert asyncio.run(engine.reconcile(row['id']))
        asyncio.run(drain(engine,row['id']))
        assert store.charges==len(delivery.files)==1


@pytest.mark.parametrize('origin',['waiting_supplier','action_required'])
def test_read_recovery_restores_post_charge_origin_without_permission_to_charge_again(tmp_path,origin):
    book,store,delivery,_,row=prepared(tmp_path)
    book.transition(row['id'],'prepared',origin)
    book.transition(row['id'],origin,'inspecting',inspection_origin=origin)
    book=Ledger(book.path,simulation=True)
    book.recover()
    assert book.get(row['id'])['state']==origin
    asyncio.run(drain(Engine(book,store,delivery),row['id']))
    assert book.get(row['id'])['state']=='purchase_review'
    assert store.charges==0 and not delivery.files


def test_legacy_unknown_inspection_origin_stays_in_review(tmp_path):
    book,store,delivery,_,row=prepared(tmp_path)
    book.transition(row['id'],'prepared','inspecting')
    book=Ledger(book.path,simulation=True)
    book.recover()
    assert book.get(row['id'])['state']=='purchase_review'
    asyncio.run(drain(Engine(book,store,delivery),row['id']))
    assert store.charges==0 and not delivery.files


def test_failed_charging_reservation_does_not_call_store_pay(tmp_path,monkeypatch):
    book,store,delivery,engine,row=prepared(tmp_path)
    transition=book.transition
    def refuse(invoice_id,before,after,**fields):
        if after=='charging':return False
        return transition(invoice_id,before,after,**fields)
    monkeypatch.setattr(book,'transition',refuse)
    assert asyncio.run(engine.step(row['id'])) is False
    assert store.charges==0 and not delivery.files

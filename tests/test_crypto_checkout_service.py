import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_checkout.checkout import CheckoutService,CheckoutUnavailable,invoice_text
from crypto_checkout.ledger import Ledger

ADDRESS='TUT5f8FYNuQT3LriVVpqEyXgXYQiRj7naw'


class Chain:
    async def height(self):
        await asyncio.sleep(0)
        return 100


def service(tmp_path, *, ready=True, cents=True):
    return CheckoutService(Ledger(tmp_path/'orders.sqlite3',simulation=False),tmp_path/'uploads',Chain(),
                           SimpleNamespace(ready=ready),address=ADDRESS,allow_unique_cents=cents)


def test_twenty_simultaneous_clients_own_distinct_invoices(tmp_path):
    s=service(tmp_path)
    async def all_customers():
        uploads=[s.upload(i+1,'device.id.bin',('source-'+str(i)).encode(),'tg:1') for i in range(20)]
        return await asyncio.gather(*(s.choose(u['id'],u['user_id'],'full') for u in uploads))
    invoices=asyncio.run(all_customers())
    assert len({i['id'] for i in invoices})==20
    assert len({i['amount_atoms'] for i in invoices})==20
    for i in invoices:
        assert s.status(i['id'],i['user_id'])['source_sha']==i['source_sha']
        assert f"{i['amount_atoms']//1_000_000}.{i['amount_atoms']%1_000_000//10_000:02d}" in invoice_text(i)
        with pytest.raises(ValueError):s.status(i['id'],i['user_id']+100)


def test_repeated_choose_survives_restart_without_second_invoice(tmp_path):
    s=service(tmp_path)
    u=s.upload(1,'device.id',b'one device','tg:10')
    first=asyncio.run(s.choose(u['id'],1,'eco'))
    other=service(tmp_path)
    async def retries():return await asyncio.gather(*(other.choose(u['id'],1,'eco') for _ in range(10)))
    assert {r['id'] for r in asyncio.run(retries())}=={first['id']}
    assert len(other.ledger.list_by_user(1))==1


@pytest.mark.parametrize('ready,cents',[(False,True),(True,False)])
def test_unready_store_or_unapproved_amounts_never_issue_payment(tmp_path,ready,cents):
    s=service(tmp_path,ready=ready,cents=cents)
    u=s.upload(1,'device.id',b'source','tg:1')
    with pytest.raises(CheckoutUnavailable):asyncio.run(s.choose(u['id'],1,'full'))
    assert not s.ledger.list_by_user(1)


def test_upload_identity_and_ownership(tmp_path):
    s=service(tmp_path)
    first=s.upload(1,'device.id',b'source','tg:1')
    assert s.upload(1,'device.id',b'source','tg:1')['id']==first['id']
    with pytest.raises(ValueError):s.upload(1,'device.id',b'changed','tg:1')
    with pytest.raises(ValueError):asyncio.run(s.choose(first['id'],2,'full'))
    with pytest.raises(ValueError):s.upload(1,'screenshot.jpg',b'photo','tg:2')


def test_customer_pending_limit_is_atomic_across_threads(tmp_path):
    s=service(tmp_path)
    uploads=[s.upload(1,'device.id',('device'+str(i)).encode(),'tg:'+str(i)) for i in range(10)]
    def choose(u):
        try:return asyncio.run(s.choose(u['id'],1,'full'))
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=10) as pool:result=list(pool.map(choose,uploads))
    assert len([r for r in result if r])==2
    assert len(s.ledger.list_by_user(1))==2

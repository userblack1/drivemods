"""Executable simulation using the real parser, ledger and purchase state machine."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from .binance import match_credit
from .fulfillment import Engine,StoreOrder
from .ledger import Ledger
from .tron import USDT,TRANSFER,address_bytes,address_from_bytes,parse_solid_receipt

# Synthetic address with no supplied private key. Never show as a payment QR.
TEST_ADDRESS=address_from_bytes(b'\x41'+b'\x12'*20)


def synthetic_receipt(address,amount,txid,*,block=1000,timestamp_ms=None):
    return {'id':txid,'blockNumber':block,'blockTimeStamp':int(time.time()*1000) if timestamp_ms is None else timestamp_ms,
            'receipt':{'result':'SUCCESS'},'log':[{
              'address':address_bytes(USDT)[1:].hex(),
              'topics':[TRANSFER,'0'*24+'23'*20,'0'*24+address_bytes(address)[1:].hex()],
              'data':f'{amount:064x}'}]}


class SimulatedStore:
    simulation=True

    def __init__(self):
        self.orders={}
        self.uploads,self.charges=0,0
        self.authentication=False
        self.timeout_after_charge=False

    async def prepare(self,invoice):
        self.uploads+=1
        order=StoreOrder('DEMO-'+invoice['id'],invoice['id'],invoice['source_sha'],
                         invoice['plan'],'USD',invoice['cost_cents'],'unpaid')
        self.orders[order.reference]=order
        await asyncio.sleep(0)
        return order

    async def inspect(self,reference):
        return self.orders[reference]

    async def pay(self,reference,idempotency_key):
        self.charges+=1
        old=self.orders[reference]
        self.orders[reference]=replace(old,status='action_required' if self.authentication else 'paid',
                                      payment_reference='DEMO-PAYMENT-'+idempotency_key)
        await asyncio.sleep(0)
        if self.timeout_after_charge:
            raise TimeoutError('SIMULATED LOST RESPONSE')
        return self.orders[reference]

    async def download(self,reference):
        return ('DEMO ONLY - NOT A VALID DEVICE LICENSE\n'+reference).encode()


class SimulatedDelivery:
    simulation=True
    def __init__(self):
        self.files=[]
        self.fail=False
    async def send(self,invoice,path,filename):
        if self.fail:
            raise ConnectionError('SIMULATED TELEGRAM OUTAGE')
        self.files.append((invoice['id'],filename))
        return 'DEMO-TELEGRAM-RECEIPT'


def create_synthetic_invoice(ledger,folder,index=1,*,plan='full',shared=True):
    order_id=uuid4().hex
    source=Path(folder)/order_id/'device.id.bin'
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_bytes(b'SYNTHETIC DEVICE ID - NEVER UPLOAD TO DRIVEMODS '+order_id.encode())
    kwargs={'shared_address':TEST_ADDRESS,'allow_amount_suffix':True} if shared else {}
    return ledger.invoice(order_id,1000+index,plan,source,hashlib.sha256(source.read_bytes()).hexdigest(),1000,**kwargs)


def confirm_synthetic_payment(ledger,invoice,index=1):
    txid=hashlib.sha256(('SYNTHETIC TRANSACTION '+str(index)+invoice['id']).encode()).hexdigest()
    raw=synthetic_receipt(invoice['address'],invoice['amount_atoms'],txid,timestamp_ms=invoice['created_ms']+1)
    transfer=parse_solid_receipt(raw,txid)[0]
    ledger.observe(transfer)
    credit=match_credit([{'id':'DEMO-DEPOSIT-'+str(index),'coin':'USDT','network':'TRX','status':1,
                         'travelRuleStatus':0,'address':transfer.address,'txId':txid,
                         'amount':f'{transfer.amount_atoms/1_000_000:.6f}'}],transfer)
    ledger.confirm_exchange(invoice['id'],credit)
    return transfer


async def run_demo(out: Path):
    out=Path(out)
    run=out/('run-'+uuid4().hex)
    run.mkdir(parents=True)
    ledger=Ledger(run/'invoices.sqlite3',simulation=True)
    store,delivery=SimulatedStore(),SimulatedDelivery()
    engine=Engine(ledger,store,delivery)
    invoices=[create_synthetic_invoice(ledger,run,index=i+1,plan='eco' if i%2 else 'full') for i in range(10)]
    # Before payment, every purchase is blocked.
    for invoice in invoices:
        assert not await engine.step(invoice['id'])
    assert store.charges==0
    for index,invoice in enumerate(invoices):
        transfer=confirm_synthetic_payment(ledger,invoice,index+1)
        assert ledger.observe(transfer)=='duplicate'
    for _ in range(8):
        await asyncio.gather(*(engine.step(invoice['id']) for invoice in invoices for _ in range(2)))
    result={'simulation':True,'real_chain_payments':0,'real_card_charges':0,'real_licenses':0,
            'customers':len(invoices),'simulated_store_uploads':store.uploads,
            'simulated_card_charges':store.charges,'delivered_test_files':len(delivery.files),
            'duplicate_payments_ignored':len(invoices),
            'orders':[{'customer':i+1,'plan':invoice['plan'].upper(),
                       'invoice_usdt':f"{invoice['amount_atoms']/1_000_000:.2f}",
                       'state':ledger.get(invoice['id'])['state'],'events':ledger.audit(invoice['id'])}
                      for i,invoice in enumerate(invoices)]}
    assert store.charges==10 and len(delivery.files)==10
    assert all(row['state']=='delivered' for row in result['orders'])
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='orders'},ensure_ascii=False,indent=2))
    print('Report: '+str((out/'report.json').resolve()))
    return result

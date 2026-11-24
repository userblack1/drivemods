"""Continuous read-only address observer. Cannot approve orders or buy licenses."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

from .tron import TronReader,address_bytes


class Observations:
    def __init__(self,folder: Path,address: str):
        address_bytes(address)
        self.folder,self.address=Path(folder),address
        self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'observations.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS config(address TEXT PRIMARY KEY,since_ms INTEGER NOT NULL)')
            previous=db.execute('SELECT address FROM config').fetchall()
            if previous and previous[0][0]!=address:
                raise ValueError('Use separate data for another receiving address')
            db.execute('INSERT OR IGNORE INTO config VALUES(?,?)',(address,int((time.time()-86400)*1000)))
            db.execute('''CREATE TABLE IF NOT EXISTS transfers(txid TEXT NOT NULL,log_index INTEGER NOT NULL,
                    amount_atoms INTEGER NOT NULL,block INTEGER NOT NULL,timestamp_ms INTEGER NOT NULL,
                    receipt_sha TEXT NOT NULL,PRIMARY KEY(txid,log_index))''')

    def since(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT since_ms FROM config').fetchone()[0]

    def seen(self,txid):
        with sqlite3.connect(self.path) as db:
            return bool(db.execute('SELECT 1 FROM transfers WHERE txid=?',(txid,)).fetchone())

    def save(self,transfer):
        if transfer.address!=self.address:
            raise ValueError('Unexpected recipient')
        with sqlite3.connect(self.path) as db:
            return db.execute('INSERT OR IGNORE INTO transfers VALUES(?,?,?,?,?,?)',
                     (transfer.txid,transfer.log_index,transfer.amount_atoms,transfer.block,
                      transfer.timestamp_ms,transfer.receipt_sha256)).rowcount==1

    def health(self,*,ok,error=None):
        with sqlite3.connect(self.path) as db:
            total=db.execute('SELECT count(*) FROM transfers').fetchone()[0]
        report={'mode':'observe_only','network':'TRON mainnet','asset':'USDT','ok':ok,
                'checked_at_utc':datetime.now(timezone.utc).isoformat(),
                'verified_events':total,'payment_confirmed_for_orders':False,
                'license_purchase_enabled':False,'error_type':error}
        temp=self.folder/'health.part'
        temp.write_text(json.dumps(report,indent=2),encoding='utf-8')
        temp.replace(self.folder/'health.json')
        return report


async def cycle(reader,store):
    for txid in await reader.candidates(store.address,store.since()):
        if store.seen(txid):
            continue
        for transfer in await reader.transfers(txid):
            if transfer.address==store.address:
                store.save(transfer)
    return store.health(ok=True)


async def run(address,folder,*,once=False,interval=30):
    os.umask(0o077)
    store=Observations(folder,address)
    reader=TronReader(os.getenv('TRONGRID_API_KEY',''))
    failures=0
    try:
        while True:
            try:
                result=await cycle(reader,store)
                failures=0
                if once:
                    print(json.dumps(result,ensure_ascii=False))
                    return
            except Exception as exc:
                failures+=1
                store.health(ok=False,error=type(exc).__name__)
                logging.warning('Read-only observer deferred: %s',type(exc).__name__)
                if once:
                    raise RuntimeError('Observer check failed: '+type(exc).__name__) from None
            await asyncio.sleep(min(interval*2**min(failures,3),300))
    finally:
        await reader.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    address=os.environ['CRYPTO_RECEIVING_ADDRESS']
    folder=Path(os.environ['CRYPTO_DATA_DIR'])
    asyncio.run(run(address,folder,once=args.once))


if __name__=='__main__':
    main()

"""Poll read-only chain data and, when configured, exchange credit status."""
import asyncio
import logging
import time

from .ledger import Ledger
from .history import HistoryWindows
from .tron import HistoryPageLimit


class Watcher:
    def __init__(self, ledger: Ledger, tron, exchange=None, *, clock=time.time, windows_per_cycle=4):
        if ledger.simulation:
            raise ValueError('A live watcher must not write to a simulation ledger')
        self.ledger,self.tron,self.exchange=ledger,tron,exchange
        if type(windows_per_cycle) is not int or not 1 <= windows_per_cycle <= 100:
            raise ValueError('Invalid chain scan window bound')
        self.clock,self.windows_per_cycle=clock,windows_per_cycle
        self.history=HistoryWindows(ledger)
        self._rpc_slots=asyncio.Semaphore(1)
        self._scan_lock=asyncio.Lock()
        self.last_errors=[]

    def _deferred(self,stage,error):
        # No transaction IDs, addresses, credentials or exception URLs in logs.
        self.last_errors.append({'stage':stage,'error_type':type(error).__name__})
        logging.getLogger(__name__).warning('Crypto scan deferred: %s %s',stage,type(error).__name__)

    async def scan(self):
        async with self._scan_lock:
            rows=self.ledger.list_by_states(('awaiting_payment','chain_confirmed'),limit=10000)
            self.last_errors=[]
            histories,known,work={},{},set()
            for row in rows:
                if row['state']=='chain_confirmed' and row['txid']:
                    known[(row['txid'],row['address'])]=True
                elif row['state']=='awaiting_payment':
                    address=row['address']
                    histories[address]=min(histories.get(address,row['created_ms']),row['created_ms'])
            receipts,pair_tasks={},{}

            async def read_receipt(txid):
                async with self._rpc_slots:
                    return await self.tron.transfers(txid)

            async def process(txid,address,force):
                if not force and self.history.receipt_seen(txid,address):
                    return True
                try:
                    if txid not in receipts:
                        receipts[txid]=asyncio.create_task(read_receipt(txid))
                    transfers=await receipts[txid]
                except Exception as exc:
                    self._deferred('receipt',exc)
                    return False
                own=[value for value in transfers if value.address==address]
                for transfer in own:
                    try:
                        self.ledger.observe(transfer)
                    except Exception as exc:
                        self._deferred('deposit',exc)
                        return False
                    try:
                        with self.ledger.connection() as db:
                            target=db.execute('SELECT id,state FROM invoices WHERE txid=? AND log_index=?',
                                              (transfer.txid,transfer.log_index)).fetchone()
                        if target and target['state']=='chain_confirmed' and self.exchange:
                            credit=await self.exchange.credit(transfer)
                            if credit:
                                self.ledger.confirm_exchange(target['id'],credit)
                    except Exception as exc:
                        # The chain binding is already durable. Exchange outages
                        # retry from the known TXID independently of this cursor.
                        self._deferred('deposit',exc)
                if own:
                    try:
                        self.history.remember_receipt(txid,address,own[0].receipt_sha256)
                    except Exception as exc:
                        self._deferred('receipt',exc)
                        return False
                return True

            def payment(txid,address,force=False):
                pair=(txid,address)
                work.add(pair)
                if pair not in pair_tasks:
                    pair_tasks[pair]=asyncio.create_task(process(txid,address,force))
                return pair_tasks[pair]

            async def scan_window(address,start,end):
                async with self._rpc_slots:
                    ids=await self.tron.candidates(address,start,max_timestamp=end)
                results=await asyncio.gather(*(payment(txid,address) for txid in ids))
                return all(results)

            async def history(address,first):
                now=int(self.clock()*1000)
                try:
                    window=self.history.plan(address,first,now)
                    # New customers must not wait behind months of old unpaid
                    # invoices or a dense historical interval being bisected.
                    tail=max(first,now-3600*1000)
                    if window.start_ms>tail or window.end_ms<now:
                        try:
                            await scan_window(address,tail,now)
                        except Exception as exc:
                            self._deferred('history',exc)
                    for _ in range(self.windows_per_cycle):
                        window=self.history.plan(address,first,now)
                        try:
                            complete=await scan_window(address,window.start_ms,window.end_ms)
                        except HistoryPageLimit as exc:
                            self.history.shrink(window)
                            self._deferred('history',exc)
                            continue
                        if not complete:
                            return
                        self.history.complete(window)
                        if window.end_ms==window.sweep_end_ms:
                            break
                except Exception as exc:
                    self._deferred('history',exc)

            # Reserve known payment jobs first, even when a history scan is
            # unavailable or its durable receipt cache contains the same TXID.
            known_jobs=[payment(txid,address,True) for txid,address in known]
            await asyncio.gather(*known_jobs,*(history(address,first) for address,first in histories.items()))
            return len(work)

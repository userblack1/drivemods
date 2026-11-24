"""Poll read-only chain data and, when configured, exchange credit status."""
import logging

from .ledger import Ledger


class Watcher:
    def __init__(self, ledger: Ledger, tron, exchange=None):
        if ledger.simulation:
            raise ValueError('A live watcher must not write to a simulation ledger')
        self.ledger,self.tron,self.exchange=ledger,tron,exchange
        self.last_errors=[]

    def _deferred(self,stage,error):
        # No transaction IDs, addresses, credentials or exception URLs in logs.
        self.last_errors.append({'stage':stage,'error_type':type(error).__name__})
        logging.getLogger(__name__).warning('Crypto scan deferred: %s %s',stage,type(error).__name__)

    async def scan(self):
        with self.ledger.connection() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM invoices WHERE state IN ('awaiting_payment','chain_confirmed')")]
        self.last_errors=[]
        work=set()
        histories={}
        for row in rows:
            if row['state']=='chain_confirmed' and row['txid']:
                # A known payment remains verifiable even if the indexer stops
                # returning it. Retry the exchange confirmation from the TXID.
                work.add((row['txid'],row['address']))
            elif row['state']=='awaiting_payment':
                address=row['address']
                histories[address]=min(histories.get(address,row['created_ms']),row['created_ms'])
        for address,since in histories.items():
            try:
                # One complete query per shared address, anchored to its oldest
                # waiting invoice; no cursor skips data after a transient error.
                work.update((txid,address) for txid in await self.tron.candidates(address,since))
            except Exception as exc:
                self._deferred('history',exc)
        receipts={}
        for txid,address in sorted(work):
            try:
                if txid not in receipts:
                    receipts[txid]=await self.tron.transfers(txid)
            except Exception as exc:
                self._deferred('receipt',exc)
                continue
            for transfer in receipts[txid]:
                if transfer.address!=address:
                    continue
                try:
                    self.ledger.observe(transfer)
                    with self.ledger.connection() as db:
                        target=db.execute('SELECT id,state FROM invoices WHERE txid=? AND log_index=?',
                                          (transfer.txid,transfer.log_index)).fetchone()
                    if target and target['state']=='chain_confirmed' and self.exchange:
                        credit=await self.exchange.credit(transfer)
                        if credit:
                            self.ledger.confirm_exchange(target['id'],credit)
                except Exception as exc:
                    self._deferred('deposit',exc)
        return len(work)

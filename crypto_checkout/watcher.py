"""Poll read-only chain data and, when configured, exchange credit status."""
from .ledger import Ledger


class Watcher:
    def __init__(self, ledger: Ledger, tron, exchange=None):
        if ledger.simulation:
            raise ValueError('A live watcher must not write to a simulation ledger')
        self.ledger,self.tron,self.exchange=ledger,tron,exchange

    async def scan(self):
        with self.ledger.connection() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM invoices WHERE state IN ('awaiting_payment','chain_confirmed')")]
        seen=set()
        for row in rows:
            # Re-query from invoice creation; indexer outages never advance a lossy cursor.
            for txid in await self.tron.candidates(row['address'],row['created_ms']):
                key=(txid,row['address'])
                if key in seen:
                    continue
                seen.add(key)
                transfers=await self.tron.transfers(txid)
                for transfer in transfers:
                    if transfer.address!=row['address']:
                        continue
                    self.ledger.observe(transfer)
                    with self.ledger.connection() as db:
                        target=db.execute('SELECT id,state FROM invoices WHERE txid=? AND log_index=?',
                                          (transfer.txid,transfer.log_index)).fetchone()
                    if target and target['state']=='chain_confirmed' and self.exchange:
                        credit=await self.exchange.credit(transfer)
                        if credit:
                            self.ledger.confirm_exchange(target['id'],credit)
        return len(seen)

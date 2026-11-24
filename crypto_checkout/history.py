"""Durable bounded indexer sweeps; receipts and credit checks remain separate."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanWindow:
    address: str
    start_ms: int
    end_ms: int
    sweep_end_ms: int


class HistoryWindows:
    def __init__(self, ledger, *, window_ms=6*3600*1000):
        if type(window_ms) is not int or not 1 <= window_ms <= 86400*1000:
            raise ValueError('Invalid chain scan window')
        self.ledger,self.window_ms=ledger,window_ms
        with ledger.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS chain_scan_windows(
                address TEXT PRIMARY KEY,first_ms INTEGER NOT NULL,cursor_ms INTEGER NOT NULL,
                sweep_end_ms INTEGER NOT NULL,window_ms INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS chain_receipt_cache(
                txid TEXT NOT NULL,address TEXT NOT NULL,receipt_sha TEXT NOT NULL,
                PRIMARY KEY(txid,address))''')

    def plan(self,address,first_ms,now_ms):
        if type(first_ms) is not int or type(now_ms) is not int or not 0 < first_ms <= now_ms:
            raise ValueError('Invalid chain observation interval')
        with self.ledger.connection() as db:
            db.execute('INSERT OR IGNORE INTO chain_scan_windows VALUES(?,?,?,?,?)',
                       (address,first_ms,first_ms,now_ms,self.window_ms))
            row=db.execute('SELECT * FROM chain_scan_windows WHERE address=?',(address,)).fetchone()
            first,cursor,end,size=row['first_ms'],row['cursor_ms'],row['sweep_end_ms'],row['window_ms']
            if now_ms < end:
                raise ValueError('Local clock moved before chain observation interval')
            if first_ms < first:
                first,cursor,end=first_ms,first_ms,now_ms
            elif cursor > end:
                # Revisit the complete bounded interval to recover delayed
                # indexer entries. Fresh payments also get a separate tail scan.
                cursor,end=first,now_ms
            db.execute('UPDATE chain_scan_windows SET first_ms=?,cursor_ms=?,sweep_end_ms=? WHERE address=?',
                       (first,cursor,end,address))
            return ScanWindow(address,cursor,min(end,cursor+size-1),end)

    def complete(self,window):
        with self.ledger.connection() as db:
            changed=db.execute('''UPDATE chain_scan_windows SET cursor_ms=?,window_ms=min(?,window_ms*2)
                WHERE address=? AND cursor_ms=? AND sweep_end_ms=?''',
                (window.end_ms+1,self.window_ms,window.address,window.start_ms,window.sweep_end_ms)).rowcount
            if not changed:
                raise ValueError('Chain scan cursor changed')

    def shrink(self,window):
        size=window.end_ms-window.start_ms+1
        if size<=1:
            raise ValueError('Chain history is too dense for a one-millisecond window')
        with self.ledger.connection() as db:
            changed=db.execute('''UPDATE chain_scan_windows SET window_ms=?
                WHERE address=? AND cursor_ms=? AND sweep_end_ms=?''',
                (max(1,size//2),window.address,window.start_ms,window.sweep_end_ms)).rowcount
            if not changed:
                raise ValueError('Chain scan cursor changed')

    def receipt_seen(self,txid,address):
        with self.ledger.connection() as db:
            return bool(db.execute('SELECT 1 FROM chain_receipt_cache WHERE txid=? AND address=?',
                                   (txid,address)).fetchone())

    def remember_receipt(self,txid,address,digest):
        with self.ledger.connection() as db:
            old=db.execute('SELECT receipt_sha FROM chain_receipt_cache WHERE txid=? AND address=?',
                           (txid,address)).fetchone()
            if old and old['receipt_sha']!=digest:
                raise ValueError('Solidified receipt identity changed')
            db.execute('INSERT OR IGNORE INTO chain_receipt_cache VALUES(?,?,?)',(txid,address,digest))

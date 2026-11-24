"""Read-only Binance deposit observer, deliberately separate from orders and cards.

Only history since this data directory's first start is scanned. A persistent
full-sweep cursor and fixed query windows keep old pending deposits eligible for
later status updates, without silently dropping history at a moving cutoff.
"""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from .binance import BinanceReader
from .tron import address_bytes


WINDOW_MS = 89 * 86400 * 1000
KNOWN_STATUSES = {0, 1, 2, 6, 7, 8}


@dataclass(frozen=True)
class ObservedDeposit:
    deposit_id: str
    txid: str
    address: str
    amount_atoms: int
    inserted_ms: int
    status: int
    travel_rule_status: int | None

    @property
    def available(self):
        return self.status == 1 and self.travel_rule_status == 0


def own_deposit(row, address, start_ms, end_ms):
    """Return a normalized own deposit; no foreign deposit details are retained."""
    if not isinstance(row, dict):
        raise ValueError('Invalid Binance deposit history entry')
    if row.get('coin') != 'USDT' or row.get('network') != 'TRX' or row.get('address') != address:
        return None
    if row.get('addressTag', ''):
        raise ValueError('Unexpected deposit tag')
    deposit_id, txid = row.get('id'), row.get('txId')
    if (not isinstance(deposit_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', deposit_id)
            or not isinstance(txid, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', txid)):
        raise ValueError('Invalid Binance deposit identity')
    inserted, status, travel = row.get('insertTime'), row.get('status'), row.get('travelRuleStatus')
    if type(inserted) is not int or not start_ms <= inserted <= end_ms:
        raise ValueError('Deposit outside requested history window')
    if type(status) is not int or status not in KNOWN_STATUSES:
        raise ValueError('Unrecognized Binance deposit status')
    if travel is not None and (type(travel) is not int or travel not in {0, 1}):
        raise ValueError('Unrecognized deposit compliance status')
    try:
        if not isinstance(row.get('amount'), str):
            raise ValueError('Invalid deposit amount')
        atoms = Decimal(row['amount']) * 1_000_000
        if not atoms.is_finite() or atoms != atoms.to_integral_value() or not 0 < atoms <= 2**63 - 1:
            raise ValueError('Invalid deposit amount')
    except InvalidOperation:
        raise ValueError('Invalid deposit amount') from None
    return ObservedDeposit(deposit_id, txid.lower(), address, int(atoms), inserted, status, travel)


class DepositHistoryReader(BinanceReader):
    """The parent transport permits only deposit-history and key-rights GETs."""

    async def history(self, address, start_ms, end_ms, *, page_size=1000, max_pages=100):
        address_bytes(address)
        if (type(start_ms) is not int or type(end_ms) is not int
                or not 0 <= start_ms <= end_ms or end_ms - start_ms >= 90 * 86400 * 1000
                or type(page_size) is not int or not 1 <= page_size <= 1000
                or type(max_pages) is not int or not 1 <= max_pages <= 1000):
            raise ValueError('Invalid bounded deposit history request')
        # Explicitly repeat permission validation for each complete history scan.
        await self.check_read_only()
        result, seen_ids, seen_txs = [], set(), set()
        for page in range(max_pages):
            rows = await self._get('/sapi/v1/capital/deposit/hisrec', {
                'coin': 'USDT', 'startTime': start_ms, 'endTime': end_ms,
                'offset': page * page_size, 'limit': page_size})
            if not isinstance(rows, list) or len(rows) > page_size:
                raise ValueError('Invalid Binance history page')
            for row in rows:
                # IDs also guard pages containing only unrelated networks or
                # addresses, so a server ignoring offset cannot loop forever.
                identity = row.get('id') if isinstance(row, dict) else None
                if not isinstance(identity, str) or not identity or identity in seen_ids:
                    raise ValueError('Overlapping or unidentified Binance history pages')
                seen_ids.add(identity)
                deposit = own_deposit(row, address, start_ms, end_ms)
                if deposit:
                    if deposit.txid in seen_txs:
                        raise ValueError('Ambiguous Binance transaction identity')
                    seen_txs.add(deposit.txid)
                    result.append(deposit)
            if len(rows) < page_size:
                return result
        raise ValueError('Binance history page limit reached; scan not complete')


class ExchangeObservations:
    def __init__(self, folder, address, *, clock=time.time):
        address_bytes(address)
        self.folder, self.address, self.clock = Path(folder), address, clock
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'exchange-observations.sqlite3'
        now = int(clock() * 1000)
        with self.database() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS config(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), address TEXT NOT NULL,
                first_started_ms INTEGER NOT NULL, cursor_ms INTEGER NOT NULL,
                sweep_end_ms INTEGER NOT NULL)''')
            db.execute('INSERT OR IGNORE INTO config VALUES(1,?,?,?,?)', (address, now, now, now))
            if db.execute('SELECT address FROM config').fetchone()[0] != address:
                raise ValueError('Use separate exchange data for another receiving address')
            db.execute('''CREATE TABLE IF NOT EXISTS deposits(
                deposit_id TEXT PRIMARY KEY, txid TEXT NOT NULL UNIQUE, amount_atoms INTEGER NOT NULL,
                inserted_ms INTEGER NOT NULL, status INTEGER NOT NULL, travel_rule_status INTEGER,
                observed_ms INTEGER NOT NULL)''')

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA busy_timeout=10000')
            with db:
                yield db
        finally:
            db.close()

    def window(self):
        now = int(self.clock() * 1000)
        with self.database() as db:
            first, cursor, end = db.execute(
                'SELECT first_started_ms,cursor_ms,sweep_end_ms FROM config WHERE singleton=1').fetchone()
            if now < first or now < end:
                raise ValueError('Local clock moved before exchange observation interval')
            if cursor > end:
                cursor, end = first, now
                db.execute('UPDATE config SET cursor_ms=?,sweep_end_ms=? WHERE singleton=1', (cursor, end))
            return cursor, min(end, cursor + WINDOW_MS - 1), end

    def save_window(self, deposits, start_ms, end_ms, sweep_end_ms):
        now = int(self.clock() * 1000)
        with self.database() as db:
            cursor, actual_end = db.execute(
                'SELECT cursor_ms,sweep_end_ms FROM config WHERE singleton=1').fetchone()
            if cursor != start_ms or actual_end != sweep_end_ms:
                raise ValueError('Exchange observation cursor changed')
            for deposit in deposits:
                if deposit.address != self.address or not start_ms <= deposit.inserted_ms <= end_ms:
                    raise ValueError('Unexpected observed deposit recipient or interval')
                old = db.execute('SELECT txid,amount_atoms,inserted_ms FROM deposits WHERE deposit_id=?',
                                 (deposit.deposit_id,)).fetchone()
                if old and old != (deposit.txid, deposit.amount_atoms, deposit.inserted_ms):
                    raise ValueError('Binance deposit identity changed')
                db.execute('''INSERT INTO deposits VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(deposit_id) DO UPDATE SET status=excluded.status,
                    travel_rule_status=excluded.travel_rule_status, observed_ms=excluded.observed_ms''',
                    (deposit.deposit_id, deposit.txid, deposit.amount_atoms, deposit.inserted_ms,
                     deposit.status, deposit.travel_rule_status, now))
            db.execute('UPDATE config SET cursor_ms=? WHERE singleton=1', (end_ms + 1,))

    def health(self, *, ok, scan_complete=False, error=None):
        with self.database() as db:
            total, available = db.execute('''SELECT count(*),coalesce(sum(
                CASE WHEN status=1 AND travel_rule_status=0 THEN 1 ELSE 0 END),0) FROM deposits''').fetchone()
            first = db.execute('SELECT first_started_ms FROM config').fetchone()[0]
        report = {'mode': 'observe_only', 'source': 'Binance authenticated deposit history',
                  'asset': 'USDT', 'network': 'TRX', 'ok': bool(ok), 'scan_complete': bool(scan_complete),
                  'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                  'first_started_ms': first, 'observed_deposits': total, 'available_deposits': available,
                  'payment_confirmed_for_orders': False, 'license_purchase_enabled': False,
                  'error_type': error}
        temp = self.folder / 'exchange-health.part'
        temp.write_text(json.dumps(report, indent=2), encoding='utf-8')
        temp.replace(self.folder / 'exchange-health.json')
        return report


async def cycle(reader, store, *, windows_per_cycle=8):
    if type(windows_per_cycle) is not int or not 1 <= windows_per_cycle <= 100:
        raise ValueError('Invalid exchange scan window bound')
    complete = False
    for _ in range(windows_per_cycle):
        start, end, sweep_end = store.window()
        deposits = await reader.history(store.address, start, end)
        store.save_window(deposits, start, end, sweep_end)
        if end == sweep_end:
            complete = True
            break
    return store.health(ok=True, scan_complete=complete)


async def run(address, folder, *, once=False, interval=30):
    os.umask(0o077)
    store = ExchangeObservations(folder, address)
    reader = DepositHistoryReader(os.getenv('BINANCE_READ_API_KEY', ''), os.getenv('BINANCE_READ_API_SECRET', ''))
    failures = 0
    try:
        while True:
            try:
                result = await cycle(reader, store)
                failures = 0
                if once:
                    print(json.dumps(result))
                    return result
            except Exception as exc:
                failures += 1
                store.health(ok=False, error=type(exc).__name__)
                logging.warning('Exchange observer deferred: %s', type(exc).__name__)
                if once:
                    raise RuntimeError('Exchange observer check failed: ' + type(exc).__name__) from None
            await asyncio.sleep(min(interval * 2 ** min(failures, 3), 300))
    finally:
        await reader.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(os.environ['CRYPTO_RECEIVING_ADDRESS'],
                    Path(os.environ['CRYPTO_EXCHANGE_DATA_DIR']), once=args.once))


if __name__ == '__main__':
    main()

"""Durable order/payment binding, isolated from the running bot's database."""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time

from .tron import Transfer, address_bytes

PRICES = {'full': (100_000_000, 9000), 'eco': (70_000_000, 6000)}


class Ledger:
    def __init__(self, path: Path, *, simulation: bool, require_exchange_credit=True):
        self.path, self.simulation = Path(path), simulation
        self.require_exchange_credit = require_exchange_credit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invoices(
                    id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,plan TEXT NOT NULL,
                    source_path TEXT NOT NULL,source_sha TEXT NOT NULL,
                    address TEXT NOT NULL,amount_atoms INTEGER NOT NULL,cost_cents INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL,expires_ms INTEGER NOT NULL,start_block INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'awaiting_payment',
                    txid TEXT,log_index INTEGER,supplier_ref TEXT,payment_ref TEXT,
                    output_path TEXT,output_sha TEXT,error TEXT,exchange_deposit TEXT UNIQUE,
                    UNIQUE(address,amount_atoms),UNIQUE(txid,log_index));
                CREATE TABLE IF NOT EXISTS deposit_addresses(address TEXT PRIMARY KEY,invoice_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS deposits(
                    txid TEXT NOT NULL,log_index INTEGER NOT NULL,invoice_id TEXT,
                    amount_atoms INTEGER NOT NULL,block INTEGER NOT NULL,receipt_sha TEXT NOT NULL,
                    classification TEXT NOT NULL,PRIMARY KEY(txid,log_index));
                CREATE TABLE IF NOT EXISTS audit(
                    seq INTEGER PRIMARY KEY,invoice_id TEXT NOT NULL,event TEXT NOT NULL,at_ms INTEGER NOT NULL);
            ''')
            marker = db.execute("SELECT value FROM metadata WHERE key='simulation'").fetchone()
            if marker and marker[0] != str(simulation):
                raise ValueError('Simulation and real invoices cannot share a ledger')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('simulation',?)", (str(simulation),))
            policy = db.execute("SELECT value FROM metadata WHERE key='require_exchange_credit'").fetchone()
            if policy and policy[0] != str(require_exchange_credit):
                raise ValueError('Changing the credit policy requires a separate ledger')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('require_exchange_credit',?)",(str(require_exchange_credit),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, invoice_id, event):
        db.execute('INSERT INTO audit(invoice_id,event,at_ms) VALUES(?,?,?)', (invoice_id,event,int(time.time()*1000)))

    def get(self, invoice_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
            if not row:
                raise ValueError('Unknown invoice')
            return dict(row)

    def add_address(self, address):
        address_bytes(address)
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO deposit_addresses(address) VALUES(?)', (address,))

    def invoice(self, invoice_id, user_id, plan, source: Path, digest, start_block, *,
                shared_address=None, allow_amount_suffix=False, ttl=1800, now_ms=None):
        import hashlib
        if not re.fullmatch(r'[0-9a-f]{32}', invoice_id) or plan not in PRICES:
            raise ValueError('Invalid order or plan')
        source = Path(source).resolve()
        if not source.is_file() or not 0 < source.stat().st_size <= 1_048_576 or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError('Missing or changed device ID')
        if type(start_block) is not int or start_block <= 0 or type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid chain anchor or customer')
        if not 60 <= ttl <= 86400:
            raise ValueError('Invalid invoice lifetime')
        now = int(time.time()*1000) if now_ms is None else now_ms
        amount, cost = PRICES[plan]
        with self.connection() as db:
            existing = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
            if existing:
                if (existing['user_id'],existing['plan'],existing['source_sha']) != (user_id,plan,digest):
                    raise ValueError('Order identity conflict')
                return dict(existing)
            if shared_address:
                address_bytes(shared_address)
                if not allow_amount_suffix:
                    raise ValueError('One shared address needs explicit approval of unique amounts')
                used = {r[0] for r in db.execute('SELECT amount_atoms FROM invoices WHERE address=?',(shared_address,))}
                # 0.01..0.99 USDT. Never re-use even an expired invoice amount.
                amount = next((amount+i*10_000 for i in range(1,100) if amount+i*10_000 not in used),None)
                if amount is None:
                    raise ValueError('Unique amount capacity exhausted; add dedicated addresses')
                address = shared_address
            else:
                row = db.execute('SELECT address FROM deposit_addresses WHERE invoice_id IS NULL ORDER BY address LIMIT 1').fetchone()
                if not row:
                    raise ValueError('No unused receiving address; no invoice issued')
                address = row[0]
                db.execute('UPDATE deposit_addresses SET invoice_id=? WHERE address=?',(invoice_id,address))
            db.execute('''INSERT INTO invoices(id,user_id,plan,source_path,source_sha,address,amount_atoms,cost_cents,
                       created_ms,expires_ms,start_block) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (invoice_id,user_id,plan,str(source),digest,address,amount,cost,now,now+ttl*1000,start_block))
            self._event(db,invoice_id,'invoice_created')
        return self.get(invoice_id)

    def observe(self, transfer: Transfer):
        if not isinstance(transfer, Transfer):
            raise ValueError('Only verified chain transfers are accepted')
        with self.connection() as db:
            if db.execute('SELECT 1 FROM deposits WHERE txid=? AND log_index=?',(transfer.txid,transfer.log_index)).fetchone():
                return 'duplicate'
            row = db.execute('SELECT * FROM invoices WHERE address=? AND amount_atoms=?', (transfer.address,transfer.amount_atoms)).fetchone()
            classification = 'unmatched'
            if row:
                if transfer.timestamp_ms < row['created_ms'] or transfer.block < row['start_block']:
                    classification = 'before_invoice'
                elif transfer.timestamp_ms > row['expires_ms']:
                    classification = 'late_payment_review'
                    if row['state'] == 'awaiting_payment':
                        db.execute("UPDATE invoices SET state='payment_review',error='late_payment' WHERE id=?",(row['id'],))
                elif row['state'] != 'awaiting_payment':
                    classification = 'additional_payment_review'
                else:
                    classification = 'confirmed'
                    next_state = 'chain_confirmed' if self.require_exchange_credit else 'paid'
                    db.execute('UPDATE invoices SET state=?,txid=?,log_index=? WHERE id=?', (next_state,transfer.txid,transfer.log_index,row['id']))
                self._event(db,row['id'],classification)
            db.execute('INSERT INTO deposits VALUES(?,?,?,?,?,?,?)',
                       (transfer.txid,transfer.log_index,row['id'] if row else None,transfer.amount_atoms,
                        transfer.block,transfer.receipt_sha256,classification))
        return classification

    def confirm_exchange(self, invoice_id, credit):
        from .binance import DepositCredit
        if not isinstance(credit,DepositCredit):
            raise ValueError('A verified Binance deposit is required')
        with self.connection() as db:
            row = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
            if not row or (credit.txid,credit.address,credit.amount_atoms) != (row['txid'],row['address'],row['amount_atoms']):
                raise ValueError('Exchange credit does not match the chain payment')
            if row['exchange_deposit'] == credit.deposit_id:
                return False
            if row['state'] != 'chain_confirmed' or not credit.deposit_id:
                raise ValueError('Order is not awaiting exchange confirmation')
            db.execute("UPDATE invoices SET state='paid',exchange_deposit=? WHERE id=?",(credit.deposit_id,invoice_id))
            self._event(db,invoice_id,'binance_credit_confirmed')
        return True

    def transition(self, invoice_id, before, after, **fields):
        allowed = {'supplier_ref','payment_ref','output_path','output_sha','error'}
        if set(fields)-allowed:
            raise ValueError('Unsupported fields')
        with self.connection() as db:
            changed = db.execute('UPDATE invoices SET state=?'+''.join(','+k+'=?' for k in fields)+' WHERE id=? AND state=?',
                                 (after,*fields.values(),invoice_id,before)).rowcount
            if changed:
                self._event(db,invoice_id,after)
            return bool(changed)

    def recover(self):
        with self.connection() as db:
            # Ambiguous external writes must be reconciled, never repeated.
            for state in ('preparing','charging','inspecting'):
                db.execute("UPDATE invoices SET state='purchase_review',error=? WHERE state=?",('restart_during_'+state,state))
            db.execute("UPDATE invoices SET state='ready' WHERE state='sending'")
            db.execute("UPDATE invoices SET state='download_pending' WHERE state='downloading'")

    def audit(self, invoice_id):
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT event FROM audit WHERE invoice_id=? ORDER BY seq',(invoice_id,))]

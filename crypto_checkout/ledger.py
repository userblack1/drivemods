"""Durable order/payment binding, isolated from the running bot's database."""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time

from .tron import Transfer, address_bytes

PRICES = {'full': (100_000_000, 9000), 'eco': (70_000_000, 6000)}
INVOICE_STATES = frozenset({
    'awaiting_payment','chain_confirmed','paid','preparing','prepared','inspecting',
    'charging','waiting_supplier','action_required','purchase_review','download_pending',
    'downloading','ready','sending','delivered','payment_review','expired','canceled',
})


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
                CREATE TABLE IF NOT EXISTS invoice_requests(
                    user_id INTEGER NOT NULL,request_key TEXT NOT NULL,invoice_id TEXT NOT NULL,
                    PRIMARY KEY(user_id,request_key),FOREIGN KEY(invoice_id) REFERENCES invoices(id));
                CREATE INDEX IF NOT EXISTS invoices_user_created ON invoices(user_id,created_ms);
                CREATE INDEX IF NOT EXISTS invoices_state_created ON invoices(state,created_ms);
            ''')
            # executescript commits the transaction started by connection().
            # Reserve migrations too, including concurrently starting workers.
            db.execute('BEGIN IMMEDIATE')
            columns={row['name'] for row in db.execute('PRAGMA table_info(invoices)')}
            if 'delivery_ref' not in columns:
                db.execute('ALTER TABLE invoices ADD COLUMN delivery_ref TEXT')
            if 'inspection_origin' not in columns:
                db.execute('ALTER TABLE invoices ADD COLUMN inspection_origin TEXT')
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

    def get_for_user(self, invoice_id, user_id):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid customer')
        with self.connection() as db:
            row=db.execute('SELECT * FROM invoices WHERE id=? AND user_id=?',
                           (invoice_id,user_id)).fetchone()
            if not row:
                raise ValueError('Unknown invoice')
            return dict(row)

    @staticmethod
    def _list_options(states, limit):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('Invalid invoice list limit')
        if states is None:
            return None
        if isinstance(states,str):
            raise ValueError('Pass invoice states as a collection')
        states=tuple(states)
        if not states or any(not isinstance(state,str) or state not in INVOICE_STATES for state in states):
            raise ValueError('Invalid invoice states')
        return tuple(dict.fromkeys(states))

    def list_by_user(self, user_id, *, states=None, limit=100):
        """Newest customer orders first; never expose another customer's order."""
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid customer')
        states=self._list_options(states,limit)
        clause=' AND state IN ('+','.join('?' for _ in states)+')' if states else ''
        values=(user_id,*(states or ()),limit)
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM invoices WHERE user_id=?'+clause+' ORDER BY created_ms DESC,id DESC LIMIT ?',values)]

    def list_by_states(self, states, *, limit=100):
        """Oldest work first, bounded for one worker cycle; no network I/O under the lock."""
        states=self._list_options(states,limit)
        if states is None:
            raise ValueError('Explicit work states are required')
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM invoices WHERE state IN ('+','.join('?' for _ in states)+')'
                ' ORDER BY created_ms,id LIMIT ?',(*states,limit))]

    def add_address(self, address):
        address_bytes(address)
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO deposit_addresses(address) VALUES(?)', (address,))

    def invoice(self, invoice_id, user_id, plan, source: Path, digest, start_block, *,
                shared_address=None, allow_amount_suffix=False, ttl=1800, now_ms=None,
                request_key=None, reuse_existing=False, max_pending_per_user=None,
                receiving_wallet=None):
        """Atomically allocate a never-reused payment identity.

        A request_key identifies one customer action permanently, including after
        a restart. reuse_existing additionally deduplicates this customer's same
        plan/device while payment or fulfillment is pending, or already delivered.
        A shared address has only 99 safe cents per plan for its entire lifetime;
        exhaustion raises rather than reassigning an old customer's payment.
        """
        import hashlib
        if receiving_wallet is not None and shared_address is not None:
            raise ValueError('Choose one receiving address strategy')
        if not isinstance(invoice_id,str) or not re.fullmatch(r'[0-9a-f]{32}', invoice_id) or plan not in PRICES:
            raise ValueError('Invalid order or plan')
        source = Path(source).resolve()
        if not source.is_file() or not 0 < source.stat().st_size <= 1_048_576 or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError('Missing or changed device ID')
        if type(start_block) is not int or start_block <= 0 or type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid chain anchor or customer')
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise ValueError('Invalid invoice lifetime')
        now = int(time.time()*1000) if now_ms is None else now_ms
        if type(now) is not int or now <= 0:
            raise ValueError('Invalid invoice timestamp')
        if request_key is not None and (not isinstance(request_key,str)
                or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}',request_key)):
            raise ValueError('Invalid customer request key')
        if type(reuse_existing) is not bool:
            raise ValueError('Invalid source reuse policy')
        if max_pending_per_user is not None and (type(max_pending_per_user) is not int or not 1 <= max_pending_per_user <= 100):
            raise ValueError('Invalid pending invoice limit')
        amount, cost = PRICES[plan]
        with self.connection() as db:
            def existing_order(row):
                if (row['user_id'],row['plan'],row['source_sha']) != (user_id,plan,digest):
                    raise ValueError('Order identity conflict')
                if request_key is not None:
                    mapped=db.execute('SELECT invoice_id FROM invoice_requests WHERE user_id=? AND request_key=?',
                                      (user_id,request_key)).fetchone()
                    if mapped and mapped['invoice_id'] != row['id']:
                        raise ValueError('Customer request identity conflict')
                    db.execute('INSERT OR IGNORE INTO invoice_requests VALUES(?,?,?)',
                               (user_id,request_key,row['id']))
                return dict(row)

            existing = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
            if existing:
                return existing_order(existing)
            if request_key is not None:
                existing=db.execute('''SELECT i.* FROM invoices i JOIN invoice_requests r ON r.invoice_id=i.id
                                       WHERE r.user_id=? AND r.request_key=?''',(user_id,request_key)).fetchone()
                if existing:
                    return existing_order(existing)
            if reuse_existing:
                existing=db.execute('''SELECT * FROM invoices WHERE user_id=? AND plan=? AND source_sha=?
                    AND state NOT IN ('expired','canceled')
                    AND (state!='awaiting_payment' OR expires_ms>=?) ORDER BY created_ms DESC,id DESC LIMIT 1''',
                    (user_id,plan,digest,now)).fetchone()
                if existing:
                    return existing_order(existing)
            if max_pending_per_user is not None:
                pending = db.execute("SELECT count(*) FROM invoices WHERE user_id=? AND "
                    "(state='chain_confirmed' OR (state='awaiting_payment' AND expires_ms>=?))",
                    (user_id, now)).fetchone()[0]
                if pending >= max_pending_per_user:
                    raise ValueError('Pending customer invoice limit reached')
            if receiving_wallet is not None:
                address = receiving_wallet.allocate(db,invoice_id)
                address_bytes(address)
                if db.execute('SELECT 1 FROM invoices WHERE address=?',(address,)).fetchone():
                    raise ValueError('A receiving address must never be reused')
                db.execute('INSERT INTO deposit_addresses(address,invoice_id) VALUES(?,?)',(address,invoice_id))
            elif shared_address:
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
            if request_key is not None:
                db.execute('INSERT INTO invoice_requests VALUES(?,?,?)',(user_id,request_key,invoice_id))
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
            if db.execute('SELECT 1 FROM invoices WHERE exchange_deposit=? AND id!=?',
                          (credit.deposit_id,invoice_id)).fetchone():
                raise ValueError('Exchange deposit is already bound to another order')
            db.execute("UPDATE invoices SET state='paid',exchange_deposit=? WHERE id=?",(credit.deposit_id,invoice_id))
            self._event(db,invoice_id,'binance_credit_confirmed')
        return True

    def transition(self, invoice_id, before, after, **fields):
        allowed = {'supplier_ref','payment_ref','output_path','output_sha','error','inspection_origin'}
        if set(fields)-allowed:
            raise ValueError('Unsupported fields')
        if fields.get('inspection_origin') not in {None,'prepared','waiting_supplier','action_required'}:
            raise ValueError('Invalid inspection origin')
        with self.connection() as db:
            changed = db.execute('UPDATE invoices SET state=?'+''.join(','+k+'=?' for k in fields)+' WHERE id=? AND state=?',
                                 (after,*fields.values(),invoice_id,before)).rowcount
            if changed:
                self._event(db,invoice_id,after)
            return bool(changed)

    def mark_delivered(self, invoice_id, message_ref):
        """Persist the Telegram acknowledgement without ever reopening a purchase."""
        if not isinstance(message_ref,str) or not 1 <= len(message_ref) <= 256 or any(
                ord(char)<32 or ord(char)==127 for char in message_ref):
            raise ValueError('A delivery acknowledgement is required')
        with self.connection() as db:
            row=db.execute('SELECT state,delivery_ref FROM invoices WHERE id=?',(invoice_id,)).fetchone()
            if not row:
                raise ValueError('Unknown invoice')
            if row['state']=='delivered' and row['delivery_ref']==message_ref:
                return False
            if row['state']!='sending':
                raise ValueError('Order is not awaiting delivery acknowledgement')
            db.execute("UPDATE invoices SET state='delivered',delivery_ref=?,error=NULL WHERE id=?",
                       (message_ref,invoice_id))
            self._event(db,invoice_id,'delivered')
            return True

    def recover(self):
        with self.connection() as db:
            # Ambiguous external writes must be reconciled, never repeated.
            for state in ('preparing','charging'):
                ids=[row['id'] for row in db.execute('SELECT id FROM invoices WHERE state=?',(state,))]
                db.execute("UPDATE invoices SET state='purchase_review',error=? WHERE state=?",('restart_during_'+state,state))
                for invoice_id in ids:
                    self._event(db,invoice_id,'restart_during_'+state)
            for row in db.execute("SELECT id,inspection_origin FROM invoices WHERE state='inspecting'").fetchall():
                origin=row['inspection_origin']
                if origin in {'prepared','waiting_supplier','action_required'}:
                    db.execute('UPDATE invoices SET state=?,inspection_origin=NULL WHERE id=?',(origin,row['id']))
                else:
                    db.execute("UPDATE invoices SET state='purchase_review',inspection_origin=NULL,"
                               "error='restart_during_inspecting' WHERE id=?",(row['id'],))
                self._event(db,row['id'],'restart_during_inspecting')
            for before,after in (('sending','ready'),('downloading','download_pending')):
                ids=[row['id'] for row in db.execute('SELECT id FROM invoices WHERE state=?',(before,))]
                db.execute('UPDATE invoices SET state=? WHERE state=?',(after,before))
                for invoice_id in ids:
                    self._event(db,invoice_id,'restart_during_'+before)

    def audit(self, invoice_id):
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT event FROM audit WHERE invoice_id=? ORDER BY seq',(invoice_id,))]

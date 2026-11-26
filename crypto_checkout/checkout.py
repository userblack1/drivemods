"""Local uploads and per-customer crypto invoices; no store upload before payment.

This module does not start a Telegram poller or expose checkout by itself.
Its readiness gate must pass before a real payment address is presented.
"""
import hashlib
from pathlib import Path
import re
from uuid import uuid4

from .ledger import Ledger
from .tron import address_bytes


class CheckoutUnavailable(RuntimeError):
    pass


class CheckoutService:
    def __init__(self, ledger: Ledger, folder, tron, readiness, *, address,
                 allow_unique_cents=False, max_pending_per_user=2, receiving_wallet=None):
        address_bytes(address)
        self.ledger, self.folder, self.tron, self.readiness = ledger, Path(folder), tron, readiness
        self.address, self.allow_unique_cents = address, allow_unique_cents
        self.max_pending_per_user = max_pending_per_user
        self.receiving_wallet = receiving_wallet
        self.folder.mkdir(parents=True, exist_ok=True)
        with ledger.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,request_key TEXT NOT NULL,
                source_path TEXT NOT NULL,source_sha TEXT NOT NULL,
                UNIQUE(user_id,request_key))''')

    def upload(self, user_id, filename, content, request_key):
        if (type(user_id) is not int or user_id <= 0 or not isinstance(filename,str)
                or Path(filename).suffix.lower() not in {'.bin','.id'}
                or not isinstance(content,bytes) or not 0 < len(content) <= 1_048_576
                or not isinstance(request_key,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}',request_key)):
            raise ValueError('Invalid device upload')
        digest=hashlib.sha256(content).hexdigest()
        # Serialize upload identity and file creation across callbacks/workers.
        with self.ledger.connection() as db:
            row=db.execute('SELECT * FROM uploads WHERE user_id=? AND request_key=?',
                           (user_id,request_key)).fetchone()
            if row:
                if row['source_sha'] != digest:
                    raise ValueError('Upload identity changed')
                return dict(row)
            upload_id=uuid4().hex
            folder=self.folder/upload_id
            folder.mkdir(mode=0o700)
            source=folder/'device.id.bin'
            with source.open('xb') as stream:
                stream.write(content)
            source.chmod(0o600)
            db.execute('INSERT INTO uploads VALUES(?,?,?,?,?)',
                       (upload_id,user_id,request_key,str(source.resolve()),digest))
            return dict(db.execute('SELECT * FROM uploads WHERE id=?',(upload_id,)).fetchone())

    def owned_upload(self, upload_id, user_id):
        with self.ledger.connection() as db:
            row=db.execute('SELECT * FROM uploads WHERE id=? AND user_id=?',(upload_id,user_id)).fetchone()
            if not row:
                raise ValueError('Unknown device upload')
            return dict(row)

    async def choose(self, upload_id, user_id, plan):
        upload=self.owned_upload(upload_id,user_id)
        if plan not in {'full','eco'}:
            raise ValueError('Unknown package')
        # Do not take money for a pipeline which cannot purchase unattended.
        readiness=self.readiness() if callable(self.readiness) else self.readiness
        if readiness.ready is not True:
            raise CheckoutUnavailable('Automatic checkout is not connected')
        if not self.allow_unique_cents and self.receiving_wallet is None:
            raise CheckoutUnavailable('Shared-address invoice amounts have not been enabled')
        key='upload:'+upload_id+':'+plan
        with self.ledger.connection() as db:
            existing=db.execute('SELECT invoice_id FROM invoice_requests WHERE user_id=? AND request_key=?',
                                (user_id,key)).fetchone()
            if existing:
                existing_id=existing[0]
            else:
                existing_id=None
        if existing_id:
            return self.ledger.get_for_user(existing_id,user_id)
        anchor=await self.tron.height()
        return self.ledger.invoice(uuid4().hex,user_id,plan,Path(upload['source_path']),upload['source_sha'],anchor,
                                   shared_address=self.address if self.receiving_wallet is None else None,
                                   allow_amount_suffix=self.allow_unique_cents,receiving_wallet=self.receiving_wallet,
                                   request_key=key,reuse_existing=True,
                                   max_pending_per_user=self.max_pending_per_user)

    def status(self, invoice_id, user_id):
        return self.ledger.get_for_user(invoice_id,user_id)


def invoice_text(invoice):
    if invoice['state'] != 'awaiting_payment':
        raise ValueError('This invoice is not awaiting payment')
    atoms=invoice['amount_atoms']
    if type(atoms) is not int or atoms <= 0 or atoms % 10_000:
        raise ValueError('Invalid cent-denominated invoice')
    amount=f'{atoms//1_000_000}.{atoms%1_000_000//10_000:02d}'
    confirmation=('После подтверждения перевода сетью TRON заказ продолжится автоматически. '
                  if invoice.get('payment_policy')=='tron_wallet' else
                  'После подтверждения сети и зачисления Binance заказ продолжится автоматически. ')
    return (f"Заказ {invoice['id']} · {invoice['plan'].upper()}\n"
            f"К оплате ровно {amount} USDT\nСеть: TRON (TRC20)\n"
            f"Адрес: {invoice['address']}\n"
            "Платите только сумму этого счёта с учётом комиссии отправителя. "
            "На адрес должна поступить указанная сумма.\n"
            +confirmation+
            "Чек присылать не нужно. Статус можно проверить по номеру заказа.")

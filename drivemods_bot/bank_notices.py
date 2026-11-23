"""Capture an authenticated phone's bank notices, never authorize a payment.

This receiver has its own database and no access to orders, bank credentials,
the store, or the Telegram bot token. Even accepted notices remain unverified.
"""
import argparse
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import UUID, uuid4

from aiohttp import web


SPACE = r"[ \u00a0\u202f]"
NUMBER = rf"[0-9]+(?:{SPACE}[0-9]{{3}})*(?:[,.][0-9]{{1,2}})?"
CREDIT = re.compile(
    rf"Пополнение:{SPACE}*(?P<amount>{NUMBER}){SPACE}+KGS\n"
    rf"(?P<sender>[^\n:]{{1,120}})\nКарта:{SPACE}*\*(?P<card>[0-9]{{4}})\n"
    rf"Доступно:{SPACE}*(?P<balance>{NUMBER}){SPACE}+KGS",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CreditNotice:
    amount_minor: int
    currency: str
    card_last4: str


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_credit(text, expected_card):
    """Recognize only the tested MBANK format; neither sender nor balance is saved."""
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        return None
    match = CREDIT.fullmatch(normalize(text))
    if not match or match['card'] != expected_card:
        return None
    value = re.sub(SPACE, '', match['amount']).replace(',', '.')
    amount = int(Decimal(value) * 100)
    if not 0 < amount <= 100_000_000:
        return None
    return CreditNotice(amount, 'KGS', match['card'])


class NoticeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS phone_notices (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id TEXT UNIQUE NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
                    event_id TEXT UNIQUE, kind TEXT NOT NULL, amount_minor INTEGER,
                    currency TEXT, card_last4 TEXT, received_at REAL NOT NULL
                );
            ''')

    def connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def capture(self, text, kind, event_id, credit):
        fingerprint = hashlib.sha256((kind + '\n' + normalize(text)).encode()).hexdigest()
        with self.connect() as con:
            if event_id:
                existing = con.execute('SELECT * FROM phone_notices WHERE event_id=?', (event_id,)).fetchone()
                if existing and existing['fingerprint'] != fingerprint:
                    raise ValueError('event_id_conflict')
            existing = con.execute('SELECT * FROM phone_notices WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing:
                return dict(existing), True
            notice_id = uuid4().hex
            con.execute('''INSERT INTO phone_notices
                (notice_id,fingerprint,event_id,kind,amount_minor,currency,card_last4,received_at)
                VALUES(?,?,?,?,?,?,?,?)''',
                (notice_id, fingerprint, event_id, kind,
                 credit.amount_minor if credit else None, credit.currency if credit else None,
                 credit.card_last4 if credit else None, time.time()))
            return dict(con.execute('SELECT * FROM phone_notices WHERE notice_id=?', (notice_id,)).fetchone()), False

    def after(self, seq):
        with self.connect() as con:
            return [dict(row) for row in con.execute(
                'SELECT seq,notice_id,kind,amount_minor,currency,card_last4,received_at '
                'FROM phone_notices WHERE seq>? ORDER BY seq LIMIT 100', (seq,))]


def create_app(store, write_token, read_token, expected_card):
    if (not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', write_token)
            or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', read_token)
            or hmac.compare_digest(write_token, read_token)
            or not re.fullmatch(r'[0-9]{4}', expected_card)):
        raise ValueError('Invalid receiver credentials/configuration')

    @web.middleware
    async def no_cache(request, handler):
        response = await handler(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app = web.Application(client_max_size=8192, middlewares=[no_cache])
    recent = []

    def authorized(request, token):
        supplied = request.headers.get('Authorization', '')
        return hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode())

    async def capture(request):
        if not authorized(request, write_token):
            return web.json_response({'error': 'unauthorized'}, status=401)
        now = time.monotonic()
        recent[:] = [t for t in recent if now - t < 60]
        if len(recent) >= 60:
            return web.json_response({'error': 'rate_limited'}, status=429, headers={'Retry-After': '60'})
        recent.append(now)
        if request.content_type != 'application/json':
            return web.json_response({'error': 'json_required'}, status=415)
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            return web.json_response({'error': 'invalid_json'}, status=400)
        if not isinstance(payload, dict) or set(payload) - {'text', 'kind', 'event_id'}:
            return web.json_response({'error': 'invalid_fields'}, status=400)
        text, kind = payload.get('text'), payload.get('kind', 'notification')
        if (not isinstance(text, str) or not 1 <= len(text) <= 4096
                or not isinstance(kind, str) or kind not in {'notification', 'transport_test'}):
            return web.json_response({'error': 'invalid_notice'}, status=400)
        try:
            text.encode('utf-8')
        except UnicodeError:
            return web.json_response({'error': 'invalid_notice'}, status=400)
        event_id = payload.get('event_id')
        if event_id is not None:
            try:
                event_id = str(UUID(event_id))
            except (ValueError, TypeError, AttributeError):
                return web.json_response({'error': 'invalid_event_id'}, status=400)
        credit = parse_credit(text, expected_card) if kind == 'notification' else None
        result = {'mode': 'capture_only', 'payment_confirmed': False, 'automatic_purchase': False}
        if kind == 'notification' and credit is None:
            return web.json_response({**result, 'status': 'ignored_unrecognized_notification'})
        try:
            row, duplicate = store.capture(text, kind, event_id, credit)
        except ValueError:
            return web.json_response({'error': 'event_id_conflict'}, status=409)
        return web.json_response({**result, 'status': 'duplicate' if duplicate else 'captured',
                                  'notice_id': row['notice_id'], 'amount_minor': row['amount_minor'],
                                  'currency': row['currency'], 'bank_reference_present': False})

    async def events(request):
        if request.remote not in {'127.0.0.1', '::1'} or not authorized(request, read_token):
            return web.json_response({'error': 'unauthorized'}, status=401)
        try:
            seq = int(request.query.get('after', '0'))
            if not 0 <= seq < 2**63 - 1:
                raise ValueError
        except ValueError:
            return web.json_response({'error': 'invalid_cursor'}, status=400)
        return web.json_response({'mode': 'capture_only', 'events': store.after(seq)})

    async def health(request):
        return web.json_response({'ok': True, 'mode': 'capture_only'})

    app.router.add_post('/capture', capture)
    app.router.add_get('/internal/events', events)
    app.router.add_get('/healthz', health)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    os.umask(0o077)
    store = NoticeStore(Path(os.environ['BANK_NOTICE_DB']))
    app = create_app(store, os.environ['BANK_NOTICE_WRITE_TOKEN'], os.environ['BANK_NOTICE_READ_TOKEN'],
                     os.environ['BANK_NOTICE_EXPECTED_CARD'])
    web.run_app(app, host='127.0.0.1', port=args.port, access_log=None, print=None)


if __name__ == '__main__':
    main()

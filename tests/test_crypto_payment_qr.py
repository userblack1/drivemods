"""Validate legacy QR helper and the current address-only private bot invoices."""
import asyncio
from io import BytesIO
import time

from PIL import Image
import pytest
import zxingcpp

from crypto_checkout.payment_qr import payment_details, payment_qr
from crypto_checkout.tron import address_from_bytes
from test_crypto_bot import app, callback, document, message, outputs, upload_row


def invoice(**changes):
    return {'id': 'a' * 32, 'address': address_from_bytes(bytes.fromhex('41' + '12' * 20)),
            'state': 'awaiting_payment', 'plan': 'full', 'amount_atoms': 100_000_000,
            'expires_ms': 2_000_000, **changes}


def decoded(data):
    image = Image.open(BytesIO(data))
    result = zxingcpp.read_barcode(image)
    assert result is not None
    return result.text


@pytest.mark.parametrize('plan,atoms,amount', [('full', 100_000_000, '100.00'),
    ('eco', 70_000_000, '70.00'), ('full', 100_010_000, '100.01'), ('eco', 70_990_000, '70.99')])
def test_png_decodes_to_same_invoice_address_and_amount_matches_ledger(plan, atoms, amount):
    row = invoice(plan=plan, amount_atoms=atoms)
    png = payment_qr(row, now_ms=1_000_000)
    assert png.startswith(b'\x89PNG\r\n\x1a\n')
    assert decoded(png) == row['address']
    assert payment_details(row, now_ms=1_000_000) == (row['address'], amount)


@pytest.mark.parametrize('changes', [{'state': 'paid'}, {'state': 'delivered'}, {'expires_ms': 999_999},
    {'expires_ms': 1_000_000}, {'amount_atoms': 99_000_000}, {'amount_atoms': True},
    {'plan': 'eco'}, {'address': 'T' * 34}, {'id': '../../x'}, {'amount_atoms': 100_000_001}])
def test_invalid_or_expired_invoices_never_get_payment_images(changes):
    with pytest.raises(ValueError):
        payment_qr(invoice(**changes), now_ms=1_000_000)


def test_private_bot_text_and_copy_buttons_are_bound_to_invoice_and_replay(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def scenario():
        await dispatcher.feed_update(bot, message(document=document()))
        uploaded = upload_row(book)
        await dispatcher.feed_update(bot, callback(uploaded['id']))
        await dispatcher.feed_update(bot, message(text='/status', message_id=31))
        await dispatcher.feed_update(bot, callback(uploaded['id'], seq=32))
    asyncio.run(scenario())
    row = book.list_by_user(1001)[0]
    payments = [m for m in session.methods if m.__api_method__ == 'sendMessage' and row['id'] in m.text]
    assert len(payments) == 3 and len(book.list_by_user(1001)) == 1
    assert not any(m.__api_method__ == 'sendPhoto' for m in session.methods)
    for payment in payments:
        assert payment.chat_id == 1001
        assert row['address'] in payment.text
        assert '100.01 USDT' in payment.text and row['id'] in payment.text
        assert 'вставьте его в поле адреса получателя' in payment.text
        assert 'QR' not in payment.text
        assert payment.reply_markup.inline_keyboard[0][0].copy_text.text == row['address']
        assert payment.reply_markup.inline_keyboard[0][1].copy_text.text == '100.01'
    assert row['state'] == 'awaiting_payment'


def test_message_delivery_failure_can_retry_same_invoice_with_status(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    original = session.make_request
    failed = []
    async def fail_once(bot, method, timeout=None):
        if method.__api_method__ == 'sendMessage' and method.reply_markup and method.reply_markup.inline_keyboard[0][0].copy_text and not failed:
            failed.append(True)
            session.methods.append(method)
            raise TimeoutError('synthetic text delivery outage')
        return await original(bot, method, timeout)
    session.make_request = fail_once
    async def scenario():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id']))
        await dispatcher.feed_update(bot, message(text='/status', message_id=31))
    asyncio.run(scenario())
    row = book.list_by_user(1001)[0]
    assert len(book.list_by_user(1001)) == 1 and row['state'] == 'awaiting_payment'
    fallback = [m for m in session.methods if m.__api_method__ == 'sendMessage'][-1]
    assert row['address'] in fallback.text and row['id'] in fallback.text
    assert fallback.reply_markup.inline_keyboard[0][1].copy_text.text == '100.01'


def test_expired_status_does_not_issue_another_qr(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def scenario():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id']))
        row = book.list_by_user(1001)[0]
        with book.connection() as db:
            db.execute('UPDATE invoices SET expires_ms=? WHERE id=?', (int(time.time()*1000)-1, row['id']))
        session.methods.clear()
        await dispatcher.feed_update(bot, message(text='/status', message_id=31))
    asyncio.run(scenario())
    assert not any(m.__api_method__ == 'sendPhoto' for m in session.methods)
    assert 'Срок счёта истёк' in outputs(session)[-1]


@pytest.mark.parametrize('customers', [10, 30])
def test_simultaneous_text_invoices_and_reverse_payments_stay_bound_after_restart(tmp_path, customers):
    from test_receiving_wallet import wallet_fixture
    from crypto_checkout.demo import synthetic_receipt
    from crypto_checkout.ledger import Ledger
    from crypto_checkout.runtime import ConcurrentWatcher
    from crypto_checkout.tron import parse_solid_receipt
    book, _, checkout, session, bot, dispatcher = app(tmp_path)
    checkout.receiving_wallet = wallet_fixture()[2]
    checkout.allow_unique_cents = False
    async def customer(index):
        user = 1001 + index
        await dispatcher.feed_update(bot, message(user=user, message_id=index+1, document=document()))
        with book.connection() as db:
            uploaded = dict(db.execute('SELECT * FROM uploads WHERE user_id=?', (user,)).fetchone())
        await dispatcher.feed_update(bot, callback(uploaded['id'], user=user,
            plan='full' if index % 2 == 0 else 'eco', seq=20+index))
    async def scenario():
        await asyncio.gather(*(customer(i) for i in range(customers)))
    asyncio.run(scenario())
    payments = [m for m in session.methods if m.__api_method__ == 'sendMessage' and m.reply_markup
                and m.reply_markup.inline_keyboard[0][0].copy_text]
    assert len(payments) == customers
    assert not any(m.__api_method__ == 'sendPhoto' for m in session.methods)
    addresses = set()
    rows = []
    for payment in payments:
        row = book.list_by_user(payment.chat_id)[0]
        rows.append(row)
        assert row['payment_policy'] == 'tron_wallet'
        assert row['address'] in payment.text
        assert row['address'] not in addresses
        addresses.add(row['address'])
        amount = '100.00' if row['plan'] == 'full' else '70.00'
        assert amount + ' USDT' in payment.text
        assert payment.reply_markup.inline_keyboard[0][0].copy_text.text == row['address']
        assert payment.reply_markup.inline_keyboard[0][1].copy_text.text == amount
        assert row['state'] == 'awaiting_payment'
    # Same-amount payments arrive in reverse order. Only the invoice address,
    # confirmed receipt and unique transaction/log bind a payment to a user.
    transfers = {}
    for i, row in reversed(list(enumerate(rows))):
        txid = f'{i+1:064x}'
        transfers[row['address']] = parse_solid_receipt(synthetic_receipt(row['address'], row['amount_atoms'],
            txid, block=1001, timestamp_ms=row['created_ms']+1), txid)[0]
    class Incoming:
        async def candidates(self, address, start, *, max_timestamp):
            return [transfers[address].txid] * 2
        async def transfers(self, txid):
            await asyncio.sleep(0)
            return [t for t in transfers.values() if t.txid == txid]
    now = max(row['created_ms'] for row in rows)/1000 + 2
    watcher = ConcurrentWatcher(book, Incoming(), clock=lambda: now)
    asyncio.run(watcher.scan())
    assert not watcher.last_errors
    reopened = Ledger(book.path, simulation=False)
    for row in rows:
        saved = reopened.get(row['id'])
        assert saved['state'] == 'paid' and saved['user_id'] == row['user_id']
        assert saved['txid'] == transfers[row['address']].txid
        assert reopened.observe(transfers[row['address']]) == 'duplicate'
    with reopened.connection() as db:
        assert db.execute('SELECT count(*) FROM deposits').fetchone()[0] == customers

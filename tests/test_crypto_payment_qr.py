"""Decode generated payment images and verify the actual private bot response."""
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


def test_private_bot_photo_and_copy_buttons_are_bound_to_invoice_and_replay(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def scenario():
        await dispatcher.feed_update(bot, message(document=document()))
        uploaded = upload_row(book)
        await dispatcher.feed_update(bot, callback(uploaded['id']))
        await dispatcher.feed_update(bot, message(text='/status', message_id=31))
        await dispatcher.feed_update(bot, callback(uploaded['id'], seq=32))
    asyncio.run(scenario())
    row = book.list_by_user(1001)[0]
    photos = [m for m in session.methods if m.__api_method__ == 'sendPhoto']
    assert len(photos) == 3 and len(book.list_by_user(1001)) == 1
    for photo in photos:
        assert photo.chat_id == 1001
        assert decoded(photo.photo.data) == row['address']
        assert '100.01 USDT' in photo.caption and row['id'] in photo.caption
        assert 'может не подставиться автоматически' in photo.caption
        assert len(photo.caption) <= 1024
        assert photo.reply_markup.inline_keyboard[0][0].copy_text.text == row['address']
        assert photo.reply_markup.inline_keyboard[0][1].copy_text.text == '100.01'
        assert photo.protect_content is not True  # Save/import in banking app on the same phone.
    assert row['state'] == 'awaiting_payment'


def test_photo_delivery_failure_repeats_same_invoice_as_text(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    original = session.make_request
    async def fail_photo(bot, method, timeout=None):
        if method.__api_method__ == 'sendPhoto':
            session.methods.append(method)
            raise TimeoutError('synthetic photo delivery outage')
        return await original(bot, method, timeout)
    session.make_request = fail_photo
    async def scenario():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id']))
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


def test_ten_simultaneous_wallet_customers_get_their_own_qr_and_fixed_plan_amount(tmp_path):
    from test_receiving_wallet import wallet_fixture
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
        await asyncio.gather(*(customer(i) for i in range(10)))
    asyncio.run(scenario())
    photos = [m for m in session.methods if m.__api_method__ == 'sendPhoto']
    assert len(photos) == 10
    addresses = set()
    for photo in photos:
        row = book.list_by_user(photo.chat_id)[0]
        assert row['payment_policy'] == 'tron_wallet'
        assert decoded(photo.photo.data) == row['address']
        assert row['address'] not in addresses
        addresses.add(row['address'])
        amount = '100.00' if row['plan'] == 'full' else '70.00'
        assert amount + ' USDT' in photo.caption
        assert photo.reply_markup.inline_keyboard[0][1].copy_text.text == amount
        assert row['state'] == 'awaiting_payment'

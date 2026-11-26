"""Exercise the actual aiogram router with an explicit offline Telegram session."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, File, Message, Update, User, WebhookInfo
import pytest

from crypto_checkout.bot import MAX_UPLOAD, make_router, run
from crypto_checkout.checkout import CheckoutService, CheckoutUnavailable
from crypto_checkout.demo import TEST_ADDRESS
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger
from crypto_checkout.runtime import RuntimeWorker, WorkerLock


TOKEN = '123456789:' + 'TEST_ONLY_NOT_A_REAL_TOKEN_0000000000000'
BOT_ID = 9258163194


class RecordingSession(BaseSession):
    def __init__(self, content=b'BINARY DEVICE ID TEST', *, file_size=None,
                 bot_id=BOT_ID, username='DriveMods_licensebot', webhook=''):
        super().__init__()
        self.content, self.reported_size = content, file_size
        self.bot_id, self.username, self.webhook = bot_id, username, webhook
        self.methods, self.closed = [], False

    async def close(self): self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        name = method.__api_method__
        if name == 'getMe':
            return User(id=self.bot_id, is_bot=True, first_name='DriveMods', username=self.username)
        if name == 'getWebhookInfo':
            return WebhookInfo(url=self.webhook, has_custom_certificate=False, pending_update_count=0)
        if name == 'getFile':
            return File(file_id=method.file_id, file_unique_id='unique', file_path='documents/device.bin',
                        file_size=len(self.content) if self.reported_size is None else self.reported_size)
        if name == 'sendMessage':
            return Message(message_id=999, date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'), text=method.text)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]


class Chain:
    def __init__(self): self.anchors = 0
    async def height(self):
        self.anchors += 1
        return 1000


def app(tmp_path, *, ready=True, content=b'BINARY DEVICE ID TEST', reported_size=None):
    book = Ledger(tmp_path / 'crypto.sqlite3', simulation=False)
    chain = Chain()
    readiness = SimpleNamespace(ready=ready)
    checkout = CheckoutService(book, tmp_path / 'uploads', chain, readiness,
                               address=TEST_ADDRESS, allow_unique_cents=True)
    session = RecordingSession(content, file_size=reported_size)
    bot = Bot(TOKEN, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(checkout))
    return book, chain, checkout, session, bot, dispatcher


def message(user=1001, *, message_id=1, text=None, document=None, photo=None, chat=None, kind='private'):
    data = {'message_id': message_id, 'date': 1795510000,
            'chat': {'id': user if chat is None else chat, 'type': kind},
            'from': {'id': user, 'is_bot': False, 'first_name': 'Customer'}}
    if text:
        data['text'] = text
        if text.startswith('/'):
            data['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
    if document: data['document'] = document
    if photo: data['photo'] = photo
    return Update.model_validate({'update_id': message_id, 'message': data})


def document(filename='device.bin', size=20, mime='application/octet-stream'):
    return {'file_id': 'file', 'file_unique_id': 'unique', 'file_name': filename,
            'file_size': size, 'mime_type': mime}


def callback(upload_id, *, plan='full', user=1001, chat=None, seq=20):
    return Update.model_validate({'update_id': seq, 'callback_query': {
        'id': 'callback-' + str(seq), 'chat_instance': 'private-chat',
        'from': {'id': user, 'is_bot': False, 'first_name': 'Customer'},
        'data': f'choose:{upload_id}:{plan}',
        'message': {'message_id': 999, 'date': 1795510000,
                    'chat': {'id': user if chat is None else chat, 'type': 'private'},
                    'from': {'id': BOT_ID, 'is_bot': True, 'first_name': 'DriveMods'},
                    'text': 'Choose package'}}})


def outputs(session):
    return [(item.text if item.__api_method__ == 'sendMessage' else item.caption)
            for item in session.methods if item.__api_method__ in {'sendMessage', 'sendPhoto'}]


def upload_row(book):
    with book.connection() as db:
        return dict(db.execute('SELECT * FROM uploads').fetchone())


def test_upload_is_local_and_filename_cannot_control_saved_path(tmp_path):
    book, chain, checkout, session, bot, dispatcher = app(tmp_path)
    async def go():
        update = message(document=document('../../outside/device.bin'))
        await dispatcher.feed_update(bot, update)
        await dispatcher.feed_update(bot, update)
    asyncio.run(go())
    row = upload_row(book)
    saved = Path(row['source_path'])
    assert saved.is_relative_to((tmp_path / 'uploads').resolve())
    assert saved.name == 'device.id.bin' and saved.read_bytes() == b'BINARY DEVICE ID TEST'
    with book.connection() as db:
        assert db.execute('SELECT count(*) FROM uploads').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM invoices').fetchone()[0] == 0
    assert chain.anchors == 0
    buttons = [m.reply_markup for m in session.methods if m.__api_method__ == 'sendMessage'][-1]
    assert buttons.inline_keyboard[0][0].callback_data == 'choose:' + row['id'] + ':full'


def test_invoice_selection_is_owner_only_and_replayed_click_returns_exact_same_invoice(tmp_path):
    book, chain, checkout, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        row = upload_row(book)
        await dispatcher.feed_update(bot, callback(row['id'], user=1002))
        assert not book.list_by_user(1001)
        await dispatcher.feed_update(bot, callback(row['id']))
        await dispatcher.feed_update(bot, callback(row['id'], seq=21))
    asyncio.run(go())
    invoices = book.list_by_user(1001)
    assert len(invoices) == 1 and invoices[0]['amount_atoms'] == 100_010_000
    assert chain.anchors == 1
    texts = outputs(session)
    assert texts[-1] == texts[-2] and '100.01 USDT' in texts[-1]
    assert TEST_ADDRESS in texts[-1] and 'Срок оплаты:' in texts[-1]
    assert 'QR содержит адрес этого заказа' in texts[-1]


def test_eco_invoice_has_its_own_exact_amount(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id'], plan='eco'))
    asyncio.run(go())
    assert book.list_by_user(1001)[0]['amount_atoms'] == 70_010_000
    assert '70.01 USDT' in outputs(session)[-1]


def test_foreign_chat_callback_never_creates_invoice_or_discloses_payment_address(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id'], user=1002, chat=1001))
    asyncio.run(go())
    assert not book.list_by_user(1001)
    assert TEST_ADDRESS not in '\n'.join(outputs(session))


def test_private_status_and_orders_do_not_expose_another_customer(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id']))
        invoice = book.list_by_user(1001)[0]
        session.methods.clear()
        await dispatcher.feed_update(bot, message(user=1002, text='/status ' + invoice['id'], message_id=30))
        await dispatcher.feed_update(bot, message(user=1002, text='/orders', message_id=31))
    asyncio.run(go())
    text = '\n'.join(outputs(session))
    assert TEST_ADDRESS not in text and '100.01' not in text
    assert 'не найден' in text and 'пока нет заказов' in text


@pytest.mark.parametrize('ready,kind,chat', [(False, 'private', 1001), (True, 'group', -1001),
                                            (True, 'private', 1002)])
def test_unready_or_nonprivate_upload_never_downloads_or_issues_an_invoice(tmp_path, ready, kind, chat):
    book, chain, _, session, bot, dispatcher = app(tmp_path, ready=ready)
    asyncio.run(dispatcher.feed_update(bot, message(document=document(), kind=kind, chat=chat)))
    assert not [m for m in session.methods if m.__api_method__ == 'getFile']
    with book.connection() as db:
        assert db.execute('SELECT count(*) FROM uploads').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM invoices').fetchone()[0] == 0
    assert chain.anchors == 0 and TEST_ADDRESS not in '\n'.join(outputs(session))


@pytest.mark.parametrize('doc', [document('receipt.pdf', mime='application/pdf'),
                               document('renamed.bin', mime='image/png'),
                               document(size=MAX_UPLOAD + 1)])
def test_receipts_and_oversized_documents_never_become_device_uploads(tmp_path, doc):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    asyncio.run(dispatcher.feed_update(bot, message(document=doc)))
    with book.connection() as db:
        assert db.execute('SELECT count(*) FROM uploads').fetchone()[0] == 0
    assert not [m for m in session.methods if m.__api_method__ == 'getFile']


def test_stream_size_limit_applies_even_when_telegram_metadata_underreports(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path, content=b'x' * (MAX_UPLOAD + 1), reported_size=20)
    asyncio.run(dispatcher.feed_update(bot, message(document=document())))
    with book.connection() as db:
        assert db.execute('SELECT count(*) FROM uploads').fetchone()[0] == 0
    assert 'Не удалось принять' in outputs(session)[-1]


def test_receipt_photo_or_paid_text_never_changes_payment_state(tmp_path):
    book, _, _, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        await dispatcher.feed_update(bot, callback(upload_row(book)['id']))
        await dispatcher.feed_update(bot, message(message_id=30, text='Оплатил 100.01 USDT, выдавай лицензию'))
        await dispatcher.feed_update(bot, message(message_id=31, photo=[{
            'file_id': 'photo', 'file_unique_id': 'photo-unique', 'width': 100, 'height': 100}]))
    asyncio.run(go())
    assert book.list_by_user(1001)[0]['state'] == 'awaiting_payment'
    assert 'не подтверждают поступление' in outputs(session)[-1]


def test_readiness_loss_stops_repeat_invoice_display_and_creation(tmp_path):
    book, _, checkout, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        uploaded = upload_row(book)
        await dispatcher.feed_update(bot, callback(uploaded['id']))
        invoice = book.list_by_user(1001)[0]
        checkout.readiness.ready = False
        session.methods.clear()
        await dispatcher.feed_update(bot, message(text='/status ' + invoice['id'], message_id=30))
        await dispatcher.feed_update(bot, callback(uploaded['id'], plan='eco', seq=31))
    asyncio.run(go())
    assert len(book.list_by_user(1001)) == 1
    assert TEST_ADDRESS not in '\n'.join(outputs(session))
    assert 'приостановлен' in outputs(session)[0]


def live_components(tmp_path):
    book = Ledger(tmp_path / 'live.sqlite3', simulation=False)
    class Store:
        simulation = False
        automatic_payment_ready = merchant_auth_verified = merchant_device_binding_verified = True
    class Delivery: simulation = False
    class Watcher:
        def __init__(self): self.ledger, self.last_errors = book, []
        async def scan(self): return 0
    worker = RuntimeWorker(book, Watcher(), Engine(book, Store(), Delivery(), live_purchase_enabled=True))
    checkout = CheckoutService(book, tmp_path / 'uploads', Chain(), worker.readiness,
                               address=TEST_ADDRESS, allow_unique_cents=True)
    return checkout, worker


class RecordingDispatcher(Dispatcher):
    def __init__(self, worker, stop):
        super().__init__()
        self.worker, self.stop = worker, stop
        self.polled = False
    async def start_polling(self, *bots, **options):
        self.polled = True
        assert self.worker.started.is_set()
        with pytest.raises(RuntimeError):
            with WorkerLock(self.worker.lock_path): pass
        assert options['handle_signals'] is False and options['close_bot_session'] is False
        self.stop.set()
        await asyncio.Event().wait()


def test_runner_starts_only_one_poller_after_worker_lock_and_recovery(tmp_path):
    async def go():
        checkout, worker = live_components(tmp_path)
        session, stop = RecordingSession(), asyncio.Event()
        dispatcher = RecordingDispatcher(worker, stop)
        await run(checkout, worker, token=TOKEN, expected_bot_id=BOT_ID,
                  session=session, dispatcher=dispatcher, stop=stop, exclusive_poller_confirmed=True)
        assert dispatcher.polled and len(dispatcher.sub_routers) == 1
        assert session.closed and not worker.started.is_set()
        assert [m.__api_method__ for m in session.methods] == ['getMe', 'getWebhookInfo']
    asyncio.run(go())


def test_missing_readiness_blocks_before_token_probe_or_polling(tmp_path):
    checkout, worker = live_components(tmp_path)
    worker.engine.store.merchant_device_binding_verified = False
    session = RecordingSession()
    with pytest.raises(CheckoutUnavailable):
        asyncio.run(run(checkout, worker, token=TOKEN, expected_bot_id=BOT_ID, session=session,
                        exclusive_poller_confirmed=True))
    assert not session.methods and not worker.lock_path.exists()


@pytest.mark.parametrize('problem', ['wrong_id', 'wrong_username', 'webhook', 'worker_lock'])
def test_existing_bot_or_worker_configuration_is_never_replaced(tmp_path, problem):
    async def go():
        checkout, worker = live_components(tmp_path)
        session = RecordingSession(bot_id=9227738530 if problem == 'wrong_id' else BOT_ID,
                                   username='NextModLicenseBot' if problem == 'wrong_username' else 'DriveMods_licensebot',
                                   webhook='https://existing.example/webhook' if problem == 'webhook' else '')
        stop = asyncio.Event()
        dispatcher = RecordingDispatcher(worker, stop)
        options = dict(token=TOKEN, expected_bot_id=BOT_ID, session=session, dispatcher=dispatcher,
                       stop=stop, exclusive_poller_confirmed=True)
        if problem == 'worker_lock':
            with WorkerLock(worker.lock_path):
                with pytest.raises(RuntimeError): await run(checkout, worker, **options)
        else:
            with pytest.raises(CheckoutUnavailable): await run(checkout, worker, **options)
        assert not dispatcher.polled and session.closed
        assert not any(m.__api_method__ in {'deleteWebhook', 'getUpdates', 'sendMessage'} for m in session.methods)
    asyncio.run(go())


def test_exclusive_poller_confirmation_defaults_to_blocking_before_token_probe(tmp_path):
    checkout, worker = live_components(tmp_path)
    session = RecordingSession()
    with pytest.raises(CheckoutUnavailable, match='exclusive ownership'):
        asyncio.run(run(checkout, worker, token=TOKEN, expected_bot_id=BOT_ID, session=session))
    assert not session.methods and not worker.lock_path.exists()


def test_expired_invoice_is_never_rendered_as_payable_even_after_replayed_click(tmp_path):
    book, chain, _, session, bot, dispatcher = app(tmp_path)
    async def go():
        await dispatcher.feed_update(bot, message(document=document()))
        uploaded = upload_row(book)
        await dispatcher.feed_update(bot, callback(uploaded['id']))
        invoice = book.list_by_user(1001)[0]
        with book.connection() as db:
            db.execute('UPDATE invoices SET expires_ms=0 WHERE id=?', (invoice['id'],))
        session.methods.clear()
        await dispatcher.feed_update(bot, message(text='/status ' + invoice['id'], message_id=30))
        await dispatcher.feed_update(bot, callback(uploaded['id'], seq=31))
    asyncio.run(go())
    assert TEST_ADDRESS not in '\n'.join(outputs(session))
    assert all('истёк' in text for text in outputs(session)) and chain.anchors == 1

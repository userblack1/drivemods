"""Worker concurrency, recovery and actual Bot API transport boundary checks."""
import asyncio
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from crypto_checkout.binance import DepositCredit
from crypto_checkout.demo import (
    SimulatedDelivery, SimulatedStore, TEST_ADDRESS, confirm_synthetic_payment,
    create_synthetic_invoice, synthetic_receipt,
)
from crypto_checkout.fulfillment import Engine
from crypto_checkout.ledger import Ledger
from crypto_checkout.runtime import (
    ConcurrentWatcher, ProductionReadiness, RateLimitedExchange, RuntimeWorker,
    TelegramDelivery, WorkerLock,
)
from crypto_checkout.tron import parse_solid_receipt


TEST_TOKEN = '123456789:' + 'TEST_ONLY_NOT_A_REAL_TOKEN_0000000000000'


class NoopWatcher:
    def __init__(self, ledger):
        self.ledger, self.last_errors = ledger, []
        self.scans = 0
    async def scan(self):
        self.scans += 1
        return 0


def simulation(tmp_path, **worker_options):
    book = Ledger(tmp_path / 'simulation.sqlite3', simulation=True)
    store, delivery = SimulatedStore(), SimulatedDelivery()
    engine = Engine(book, store, delivery)
    watcher = NoopWatcher(book)
    worker = RuntimeWorker(book, watcher, engine, **worker_options)
    return book, store, delivery, engine, watcher, worker


def test_os_lock_excludes_another_process_and_remains_reusable(tmp_path):
    path = tmp_path / 'worker.lock'
    script = '''
import sys
from crypto_checkout.runtime import WorkerLock
try:
    with WorkerLock(sys.argv[1]): pass
except RuntimeError:
    sys.exit(17)
'''
    with WorkerLock(path):
        result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, timeout=20)
        assert result.returncode == 17 and not result.stdout and not result.stderr
    with WorkerLock(path):
        assert path.is_file()


def test_second_worker_cannot_recover_first_workers_inflight_charge(tmp_path):
    book, store, delivery, engine, _, worker = simulation(tmp_path)
    invoice = create_synthetic_invoice(book, tmp_path)
    confirm_synthetic_payment(book, invoice)
    book.transition(invoice['id'], 'paid', 'charging')
    async def go():
        stop = asyncio.Event()
        stop.set()
        with WorkerLock(worker.lock_path):
            with pytest.raises(RuntimeError):
                await worker.run(stop)
        assert book.get(invoice['id'])['state'] == 'charging'
        await worker.run(stop)
    asyncio.run(go())
    assert book.get(invoice['id'])['state'] == 'purchase_review' and store.charges == 0


def test_readiness_requires_every_explicit_live_capability(tmp_path):
    book = Ledger(tmp_path / 'live.sqlite3', simulation=False)
    class Store:
        simulation = False
    class Delivery:
        simulation = False
    store = Store()
    result = ProductionReadiness.inspect(book, store, Delivery())
    assert result.ready is False
    assert result.missing == ('automatic_payment_ready', 'merchant_auth_verified',
                              'merchant_device_binding_verified')
    store.automatic_payment_ready = True
    store.merchant_auth_verified = True
    store.merchant_device_binding_verified = True
    assert ProductionReadiness.inspect(book, store, Delivery()).ready
    store.automatic_payment_ready = 1
    assert not ProductionReadiness.inspect(book, store, Delivery()).ready


def test_live_flag_alone_cannot_start_a_purchase_worker(tmp_path):
    book = Ledger(tmp_path / 'live.sqlite3', simulation=False)
    class Store: simulation = False
    class Delivery: simulation = False
    worker = RuntimeWorker(book, NoopWatcher(book), Engine(book, Store(), Delivery(), live_purchase_enabled=True))
    with pytest.raises(ValueError, match='prerequisites'):
        asyncio.run(worker.run())
    assert not worker.lock_path.exists()


def test_concurrent_chain_reads_are_bounded_and_bad_receipt_does_not_block_siblings(tmp_path):
    book = Ledger(tmp_path / 'live.sqlite3', simulation=False)
    invoices = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(6)]
    transfers = {}
    for i, invoice in enumerate(invoices):
        txid = f'{i + 1:064x}'
        transfers[txid] = parse_solid_receipt(synthetic_receipt(
            invoice['address'], invoice['amount_atoms'], txid,
            timestamp_ms=invoice['created_ms'] + 1), txid)[0]
    class Chain:
        active = maximum = history_calls = receipt_calls = 0
        async def candidates(self, *args, **kwargs):
            self.history_calls += 1
            return list(transfers)
        async def transfers(self, txid):
            self.receipt_calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0.01)
                if txid == list(transfers)[0]: raise ConnectionError('UNTRUSTED PRIVATE ERROR')
                return [transfers[txid]]
            finally:
                self.active -= 1
    class Exchange:
        async def credit(self, transfer):
            return DepositCredit('deposit-' + transfer.txid, transfer.txid,
                                 transfer.address, transfer.amount_atoms)
    chain = Chain()
    watcher = ConcurrentWatcher(book, chain, RateLimitedExchange(Exchange(), minimum_interval=0), concurrency=3)
    assert asyncio.run(watcher.scan()) == 6
    assert chain.maximum == 3 and chain.history_calls == 1 and chain.receipt_calls == 6
    assert book.get(invoices[0]['id'])['state'] == 'awaiting_payment'
    assert all(book.get(row['id'])['state'] == 'paid' for row in invoices[1:])
    assert watcher.last_errors == [{'stage': 'receipt', 'error_type': 'ConnectionError'}]


def test_known_chain_confirmed_deposit_is_verified_without_indexer(tmp_path):
    book = Ledger(tmp_path / 'live.sqlite3', simulation=False)
    invoice = create_synthetic_invoice(book, tmp_path)
    txid = 'a' * 64
    transfer = parse_solid_receipt(synthetic_receipt(TEST_ADDRESS, invoice['amount_atoms'], txid,
                                   timestamp_ms=invoice['created_ms'] + 1), txid)[0]
    book.observe(transfer)
    class Chain:
        async def candidates(self, *args, **kwargs): raise AssertionError('Not needed')
        async def transfers(self, value):
            assert value == txid
            return [transfer]
    class Exchange:
        async def credit(self, value):
            return DepositCredit('deposit', value.txid, value.address, value.amount_atoms)
    watcher = ConcurrentWatcher(book, Chain(), RateLimitedExchange(Exchange(), minimum_interval=0))
    asyncio.run(watcher.scan())
    assert book.get(invoice['id'])['state'] == 'paid'


def test_exchange_reads_are_serialized_and_spaced():
    now, calls, waits = [100.0], [], []
    class Reader:
        async def credit(self, transfer):
            calls.append((transfer, now[0]))
            await asyncio.sleep(0)
            return transfer
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    async def go():
        limited = RateLimitedExchange(Reader(), minimum_interval=2, clock=lambda: now[0], sleep=sleep)
        assert await asyncio.gather(*(limited.credit(i) for i in range(3))) == [0, 1, 2]
    asyncio.run(go())
    assert [at for _, at in calls] == [100, 102, 104] and waits == [2, 2]


def test_ten_orders_have_one_purchase_each_and_visa_pay_calls_are_serialized(tmp_path):
    book, store, delivery, engine, _, worker = simulation(tmp_path)
    invoices = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(10)]
    for i, invoice in enumerate(invoices): confirm_synthetic_payment(book, invoice, index=i)
    original_pay = store.pay
    original_prepare = store.prepare
    counts = {'active': 0, 'maximum': 0, 'quota_violations': 0}
    prepared_order = []
    async def prepare(invoice):
        # This models the actual supplier's one-unpaid-license account quota.
        if any(order.status != 'paid' for order in store.orders.values()):
            counts['quota_violations'] += 1
            raise RuntimeError('Only one unpaid supplier order is permitted')
        prepared_order.append(invoice['id'])
        return await original_prepare(invoice)
    async def pay(*args):
        counts['active'] += 1
        counts['maximum'] = max(counts['maximum'], counts['active'])
        try:
            await asyncio.sleep(0.001)
            return await original_pay(*args)
        finally:
            counts['active'] -= 1
    store.pay = pay
    store.prepare = prepare
    async def go():
        for _ in range(40):
            await worker.fulfillment_once()
            await worker.wait_idle()
            if all(book.get(row['id'])['state'] == 'delivered' for row in invoices): break
    asyncio.run(go())
    assert counts['maximum'] == 1 and store.charges == 10 and len(delivery.files) == 10
    assert counts['quota_violations'] == 0
    assert prepared_order == [row['id'] for row in sorted(invoices, key=lambda row: (row['created_ms'], row['id']))]
    assert all(book.get(row['id'])['state'] == 'delivered' for row in invoices)
    assert all(book.get(row['id'])['delivery_ref'] == 'DEMO-TELEGRAM-RECEIPT' for row in invoices)
    assert all('supplier_slot_reserved' in book.audit(row['id']) and
               'supplier_slot_released_after_paid' in book.audit(row['id']) for row in invoices)


def test_supplier_reservation_is_created_before_scheduling_and_not_a_queue_of_paid_tasks(tmp_path):
    book, store, _, _, _, worker = simulation(tmp_path)
    invoices = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(6)]
    for i, invoice in enumerate(invoices): confirm_synthetic_payment(book, invoice, index=i)
    oldest = min(invoices, key=lambda row: (row['created_ms'], row['id']))
    async def go():
        assert await asyncio.gather(*(worker.fulfillment_once() for _ in range(3))) == [1, 0, 0]
        with book.connection() as db:
            assert db.execute("SELECT value FROM metadata WHERE key='supplier_slot_invoice'").fetchone()[0] == oldest['id']
        assert len(worker._jobs) == 1
        await worker.wait_idle()
    asyncio.run(go())
    assert store.uploads == 1 and book.get(oldest['id'])['state'] == 'prepared'
    assert sum(book.get(row['id'])['state'] == 'paid' for row in invoices) == 5


def test_existing_prepared_order_has_priority_and_releases_slot_only_after_paid(tmp_path):
    book, store, delivery, engine, watcher, worker = simulation(tmp_path)
    waiting, prepared = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(2)]
    for i, invoice in enumerate((waiting, prepared)): confirm_synthetic_payment(book, invoice, index=i)
    # Ensure the unrelated waiting customer really has the earlier FIFO date.
    with book.connection() as db:
        db.execute('UPDATE invoices SET created_ms=created_ms-100 WHERE id=?', (waiting['id'],))
    async def go():
        await engine.step(prepared['id'])
        assert book.get(prepared['id'])['state'] == 'prepared'
        # No process-local reservation survives; derive it from persisted state.
        restarted = RuntimeWorker(book, watcher, engine)
        assert await restarted.fulfillment_once() == 1
        await restarted.wait_idle()
        assert book.get(prepared['id'])['state'] == 'download_pending'
        assert book.get(waiting['id'])['state'] == 'paid' and store.uploads == 1
        assert await restarted.fulfillment_once() == 2  # paid file + next supplier owner
        await restarted.wait_idle()
        assert book.get(waiting['id'])['state'] == 'prepared' and store.uploads == 2
    asyncio.run(go())
    assert store.charges == 1


def test_unknown_prepare_write_holds_slot_after_restart_and_never_creates_another(tmp_path):
    book, store, delivery, engine, watcher, worker = simulation(tmp_path)
    invoices = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(2)]
    for i, invoice in enumerate(invoices): confirm_synthetic_payment(book, invoice, index=i)
    original_prepare = store.prepare
    async def lost_prepare(invoice):
        await original_prepare(invoice)
        raise TimeoutError('Lost response after supplier created an unpaid license')
    store.prepare = lost_prepare
    async def go():
        await worker.fulfillment_once()
        await worker.wait_idle()
        owner = next(row for row in book.list_by_states(('purchase_review',)))
        assert owner['supplier_ref'] is None
        restarted = RuntimeWorker(book, watcher, engine, clock=lambda: 10000)
        book.recover()
        for _ in range(10):
            await restarted.fulfillment_once()
            await restarted.wait_idle()
            await restarted.verification_once()
        with book.connection() as db:
            assert db.execute("SELECT value FROM metadata WHERE key='supplier_slot_invoice'").fetchone()[0] == owner['id']
        assert restarted._supplier_slot_state == 'purchase_review'
    asyncio.run(go())
    assert store.uploads == 1 and store.charges == 0 and watcher.scans == 10
    assert len(book.list_by_states(('paid',))) == 1 and not delivery.files


def test_authentication_challenge_holds_supplier_slot_but_other_paid_files_and_scans_continue(tmp_path):
    now = [100.0]
    book, store, delivery, engine, watcher, worker = simulation(tmp_path, clock=lambda: now[0])
    pending, ready, queued = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(3)]
    for i, invoice in enumerate((pending, ready, queued)): confirm_synthetic_payment(book, invoice, index=i)
    with book.connection() as db:
        db.execute('UPDATE invoices SET created_ms=created_ms-100 WHERE id=?', (pending['id'],))
    async def go():
        for _ in range(3): await engine.step(ready['id'])
        assert book.get(ready['id'])['state'] == 'ready'
        store.authentication = True
        for _ in range(8):
            now[0] += 301
            await worker.fulfillment_once()
            await worker.wait_idle()
            await worker.verification_once()
        assert book.get(pending['id'])['state'] == 'action_required'
        assert book.get(queued['id'])['state'] == 'paid'
        assert book.get(ready['id'])['state'] == 'delivered'
        assert worker._supplier_slot_state == 'action_required'
    asyncio.run(go())
    assert store.uploads == 2 and store.charges == 2 and watcher.scans == 8
    assert [invoice_id for invoice_id, _ in delivery.files] == [ready['id']]


def test_post_purchase_state_without_payment_proof_blocks_new_supplier_creation(tmp_path):
    book, store, delivery, engine, _, worker = simulation(tmp_path)
    corrupt, queued = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(2)]
    for i, invoice in enumerate((corrupt, queued)): confirm_synthetic_payment(book, invoice, index=i)
    async def go():
        for _ in range(3): await engine.step(corrupt['id'])
        with book.connection() as db:
            db.execute('UPDATE invoices SET payment_ref=NULL WHERE id=?', (corrupt['id'],))
        assert await worker.fulfillment_once() == 0
    asyncio.run(go())
    assert store.uploads == 1 and not delivery.files and book.get(queued['id'])['state'] == 'paid'


def test_lost_supplier_slot_owner_is_not_silently_reassigned(tmp_path):
    book, store, _, _, _, worker = simulation(tmp_path)
    invoice = create_synthetic_invoice(book, tmp_path)
    confirm_synthetic_payment(book, invoice)
    with book.connection() as db:
        db.execute("INSERT INTO metadata VALUES('supplier_slot_invoice',?)", ('f' * 32,))
    assert asyncio.run(worker.fulfillment_once()) == 0
    assert worker._supplier_slot_state == 'unknown_owner' and store.uploads == 0


@pytest.mark.parametrize('interrupted', ['preparing', 'inspecting', 'charging'])
def test_interrupted_supplier_operation_holds_slot_across_recovery(tmp_path, interrupted):
    book, store, _, engine, watcher, worker = simulation(tmp_path)
    owner, queued = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(2)]
    for i, invoice in enumerate((owner, queued)): confirm_synthetic_payment(book, invoice, index=i)
    with book.connection() as db:
        db.execute('UPDATE invoices SET created_ms=created_ms-100 WHERE id=?', (owner['id'],))
    async def go():
        if interrupted == 'preparing':
            book.transition(owner['id'], 'paid', 'preparing')
        else:
            await engine.step(owner['id'])
            book.transition(owner['id'], 'prepared', interrupted)
        assert await worker.fulfillment_once() == 0
        book.recover()
        restarted = RuntimeWorker(book, watcher, engine)
        await restarted.fulfillment_once()
        await restarted.wait_idle()
        assert book.get(owner['id'])['state'] == 'purchase_review'
        assert book.get(queued['id'])['state'] == 'paid'
        with book.connection() as db:
            assert db.execute("SELECT value FROM metadata WHERE key='supplier_slot_invoice'").fetchone()[0] == owner['id']
    asyncio.run(go())
    assert store.uploads == (0 if interrupted == 'preparing' else 1) and store.charges == 0


def test_ready_delivery_and_verification_continue_while_another_purchase_waits(tmp_path):
    async def go():
        book, store, delivery, engine, watcher, worker = simulation(
            tmp_path, payment_interval=0.01, fulfillment_interval=0.01, shutdown_timeout=0.1)
        buying, ready = [create_synthetic_invoice(book, tmp_path, index=i) for i in range(2)]
        for i, invoice in enumerate((buying, ready)): confirm_synthetic_payment(book, invoice, index=i)
        for _ in range(3): await engine.step(ready['id'])
        assert book.get(ready['id'])['state'] == 'ready'
        started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_pay = store.pay
        async def held_pay(*args):
            started.set()
            await release.wait()
            return await original_pay(*args)
        store.pay = held_pay
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert book.get(ready['id'])['state'] == 'delivered' and watcher.scans >= 2
        release.set()
        stop.set()
        await task
    asyncio.run(go())


def test_uncertain_charge_is_only_reconciled_and_never_charged_again(tmp_path):
    book, store, delivery, engine, _, worker = simulation(tmp_path)
    invoice = create_synthetic_invoice(book, tmp_path)
    confirm_synthetic_payment(book, invoice)
    store.timeout_after_charge = True
    async def go():
        for _ in range(8):
            await worker.fulfillment_once()
            await worker.wait_idle()
    asyncio.run(go())
    assert book.get(invoice['id'])['state'] == 'delivered'
    assert store.charges == 1 and len(delivery.files) == 1


def test_delivery_outage_retries_with_backoff_and_never_repurchases(tmp_path):
    now = [100.0]
    book, store, delivery, engine, _, worker = simulation(tmp_path, clock=lambda: now[0])
    invoice = create_synthetic_invoice(book, tmp_path)
    confirm_synthetic_payment(book, invoice)
    delivery.fail = True
    async def go():
        for _ in range(4):
            await worker.fulfillment_once()
            await worker.wait_idle()
        assert book.get(invoice['id'])['state'] == 'ready'
        assert await worker.fulfillment_once() == 0
        now[0] += 6
        delivery.fail = False
        await worker.fulfillment_once()
        await worker.wait_idle()
    asyncio.run(go())
    assert book.get(invoice['id'])['state'] == 'delivered'
    assert store.charges == 1 and len(delivery.files) == 1


def test_shutdown_during_a_charge_recovers_by_inspection_without_repeating_it(tmp_path):
    async def go():
        book, store, delivery, engine, watcher, worker = simulation(
            tmp_path, payment_interval=0.01, fulfillment_interval=0.01, shutdown_timeout=0.01)
        invoice = create_synthetic_invoice(book, tmp_path)
        confirm_synthetic_payment(book, invoice)
        charge_started, stop = asyncio.Event(), asyncio.Event()
        original_pay = store.pay
        async def lose_response(*args):
            await original_pay(*args)
            charge_started.set()
            await asyncio.Event().wait()
        store.pay = lose_response
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(charge_started.wait(), timeout=2)
        stop.set()
        await task
        assert book.get(invoice['id'])['state'] == 'charging' and store.charges == 1
        restarted = RuntimeWorker(book, watcher, engine)
        stopped = asyncio.Event()
        stopped.set()
        await restarted.run(stopped)
        assert book.get(invoice['id'])['state'] == 'purchase_review'
        for _ in range(5):
            await restarted.fulfillment_once()
            await restarted.wait_idle()
        assert book.get(invoice['id'])['state'] == 'delivered'
        assert store.charges == 1 and len(delivery.files) == 1
    asyncio.run(go())


def delivery_invoice(tmp_path):
    path = tmp_path / 'device.license.bin'
    path.write_bytes(b'BINARY-TEST-ONLY')
    return {'id': 'a' * 32, 'user_id': 1001, 'plan': 'full', 'output_path': str(path),
            'output_sha': hashlib.sha256(path.read_bytes()).hexdigest()}, path


def test_telegram_upload_only_targets_invoice_user_and_token_is_redacted_in_http_logs(tmp_path, caplog):
    invoice, path = delivery_invoice(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'POST' and request.url.host == 'api.telegram.org'
        assert request.url.path.endswith('/sendDocument')
        assert b'name="chat_id"\r\n\r\n1001' in request.content
        assert b'BINARY-TEST-ONLY' in request.content and b'filename="device.license.bin"' in request.content
        return httpx.Response(200, json={'ok': True, 'result': {
            'message_id': 123, 'chat': {'id': 1001, 'type': 'private'}, 'document': {'file_id': 'file'}}})
    async def go():
        delivery = TelegramDelivery(TEST_TOKEN, transport=httpx.MockTransport(handler), minimum_interval=0)
        try:
            assert TEST_TOKEN not in repr(delivery)
            assert await delivery.send(invoice, path, 'device.license.bin') == '123'
        finally:
            await delivery.close()
    with caplog.at_level(logging.INFO, logger='httpx'):
        asyncio.run(go())
    assert len(calls) == 1 and TEST_TOKEN not in caplog.text and '[REDACTED]' in caplog.text


@pytest.mark.parametrize('change', [{'user_id': -1001}, {'user_id': True}, {'id': 'broken'},
                                  {'output_sha': '0' * 64}, {'output_path': 'other-file'}])
def test_invalid_delivery_never_calls_telegram(tmp_path, change):
    invoice, path = delivery_invoice(tmp_path)
    invoice.update(change)
    calls = []
    async def go():
        delivery = TelegramDelivery(TEST_TOKEN, transport=httpx.MockTransport(lambda req: calls.append(req)))
        try:
            with pytest.raises(ValueError): await delivery.send(invoice, path, 'device.license.bin')
        finally:
            await delivery.close()
    asyncio.run(go())
    assert not calls


@pytest.mark.parametrize('kind', ['wrong_chat', 'group_chat', 'no_document', 'network'])
def test_telegram_failure_never_discloses_token_or_confirms_wrong_recipient(tmp_path, kind):
    invoice, path = delivery_invoice(tmp_path)
    def handler(request):
        if kind == 'network': raise httpx.ReadTimeout('PRIVATE KEY SHOULD NEVER APPEAR', request=request)
        result = {'message_id': 123, 'chat': {'id': 1001, 'type': 'private'}, 'document': {'file_id': 'file'}}
        if kind == 'wrong_chat': result['chat']['id'] = 1002
        if kind == 'group_chat': result['chat']['type'] = 'group'
        if kind == 'no_document': result.pop('document')
        return httpx.Response(200, json={'ok': True, 'result': result})
    async def go():
        delivery = TelegramDelivery(TEST_TOKEN, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RuntimeError) as exc:
                await delivery.send(invoice, path, 'device.license.bin')
            assert TEST_TOKEN not in str(exc.value) and 'PRIVATE KEY' not in str(exc.value)
        finally:
            await delivery.close()
    asyncio.run(go())


def test_telegram_rate_limit_is_observed_before_the_next_upload(tmp_path, monkeypatch):
    invoice, path = delivery_invoice(tmp_path)
    requests, delays = [], []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={'ok': False, 'parameters': {'retry_after': 2},
                                           'description': 'UNTRUSTED RESPONSE'})
        return httpx.Response(200, json={'ok': True, 'result': {
            'message_id': 124, 'chat': {'id': 1001, 'type': 'private'}, 'document': {'file_id': 'file'}}})
    async def fake_sleep(delay): delays.append(delay)
    async def go():
        delivery = TelegramDelivery(TEST_TOKEN, transport=httpx.MockTransport(handler), minimum_interval=0)
        try:
            with pytest.raises(RuntimeError) as exc:
                await delivery.send(invoice, path, 'device.license.bin')
            assert 'UNTRUSTED' not in str(exc.value)
            monkeypatch.setattr('crypto_checkout.runtime.asyncio.sleep', fake_sleep)
            assert await delivery.send(invoice, path, 'device.license.bin') == '124'
        finally:
            await delivery.close()
    asyncio.run(go())
    assert len(requests) == 2 and len(delays) == 1 and 1 < delays[0] <= 2

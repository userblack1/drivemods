"""Independent payment verification and serialized purchase/delivery workers.

Adapters are explicit; importing this module neither logs in nor starts a bot.
There is no second Telegram update poller and no fallback to simulated purchases.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time

import httpx

from .watcher import Watcher


WORK_STATES = ('paid', 'prepared', 'waiting_supplier', 'action_required',
               'purchase_review', 'download_pending', 'ready')
CARD_STATES = {'paid', 'prepared', 'waiting_supplier', 'action_required', 'purchase_review'}


@dataclass(frozen=True)
class ProductionReadiness:
    ready: bool
    missing: tuple[str, ...]

    @classmethod
    def inspect(cls, ledger, store, delivery):
        checks = {
            'live_ledger': ledger.simulation is False,
            'exchange_credit_required': ledger.require_exchange_credit is True,
            'live_store': getattr(store, 'simulation', None) is False,
            'automatic_payment_ready': getattr(store, 'automatic_payment_ready', None) is True,
            'merchant_auth_verified': getattr(store, 'merchant_auth_verified', None) is True,
            'merchant_device_binding_verified': getattr(store, 'merchant_device_binding_verified', None) is True,
            'live_delivery': getattr(delivery, 'simulation', None) is False,
        }
        missing = tuple(name for name, valid in checks.items() if not valid)
        return cls(not missing, missing)


class WorkerLock:
    """One worker per ledger across processes; never unlink a live lock inode."""

    def __init__(self, path):
        self.path, self._stream = Path(path), None

    def __enter__(self):
        if self._stream is not None or self.path.is_symlink():
            raise RuntimeError('Crypto worker lock is unavailable')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            if os.name != 'nt':
                os.chmod(self.path, 0o600)
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            stream.close()
            raise RuntimeError('Another crypto worker is running or lock access failed') from None
        self._stream = stream
        return self

    def __exit__(self, *args):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


class RateLimitedExchange:
    """Serialize authenticated deposit reads with bounded start frequency."""

    def __init__(self, reader, *, minimum_interval=1.0, clock=time.monotonic, sleep=asyncio.sleep):
        if not isinstance(minimum_interval, (int, float)) or not 0 <= minimum_interval <= 300:
            raise ValueError('Invalid exchange request interval')
        self.reader, self.interval, self.clock, self.sleep = reader, minimum_interval, clock, sleep
        self._lock, self._next = asyncio.Lock(), 0.0

    async def credit(self, transfer):
        async with self._lock:
            delay = self._next - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self._next = self.clock() + self.interval
            return await self.reader.credit(transfer)


class ConcurrentWatcher(Watcher):
    """Independent bounded RPC jobs; one failing invoice cannot block siblings."""

    def __init__(self, ledger, tron, exchange=None, *, concurrency=4):
        super().__init__(ledger, tron, exchange)
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError('Invalid verification concurrency')
        if exchange is not None and not isinstance(exchange, RateLimitedExchange):
            self.exchange = RateLimitedExchange(exchange)
        self._rpc_slots = asyncio.Semaphore(concurrency)
        self._scan_lock = asyncio.Lock()

    async def scan(self):
        async with self._scan_lock:
            rows = self.ledger.list_by_states(('awaiting_payment', 'chain_confirmed'), limit=1000)
            self.last_errors = []
            histories, work = {}, set()
            for row in rows:
                if row['state'] == 'chain_confirmed' and row['txid']:
                    work.add((row['txid'], row['address']))
                elif row['state'] == 'awaiting_payment':
                    address = row['address']
                    histories[address] = min(histories.get(address, row['created_ms']), row['created_ms'])

            async def history(address, since):
                try:
                    async with self._rpc_slots:
                        ids = await self.tron.candidates(address, since)
                    work.update((txid, address) for txid in ids)
                except Exception as exc:
                    self._deferred('history', exc)
            await asyncio.gather(*(history(address, since) for address, since in histories.items()))
            addresses_by_tx = {}
            for txid, address in work:
                addresses_by_tx.setdefault(txid, set()).add(address)

            async def receipt(txid, addresses):
                try:
                    async with self._rpc_slots:
                        transfers = await self.tron.transfers(txid)
                except Exception as exc:
                    self._deferred('receipt', exc)
                    return
                for transfer in transfers:
                    if transfer.address not in addresses:
                        continue
                    try:
                        self.ledger.observe(transfer)
                        with self.ledger.connection() as db:
                            target = db.execute('SELECT id,state FROM invoices WHERE txid=? AND log_index=?',
                                                (transfer.txid, transfer.log_index)).fetchone()
                        if target and target['state'] == 'chain_confirmed' and self.exchange:
                            credit = await self.exchange.credit(transfer)
                            if credit:
                                self.ledger.confirm_exchange(target['id'], credit)
                    except Exception as exc:
                        self._deferred('deposit', exc)
            await asyncio.gather(*(receipt(txid, addresses) for txid, addresses in addresses_by_tx.items()))
            return len(work)


class _TokenLogFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self._token = token

    def filter(self, record):
        record.msg = record.getMessage().replace(self._token, '[REDACTED]')
        record.args = ()
        return True


class TelegramDelivery:
    """Upload only the verified invoice's file to that invoice's private user."""
    simulation = False

    def __init__(self, token, *, transport=None, minimum_interval=1.0):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9]{6,14}:[A-Za-z0-9_-]{25,}', token):
            raise ValueError('A Telegram bot token must be configured privately')
        if not isinstance(minimum_interval, (int, float)) or not 0 <= minimum_interval <= 300:
            raise ValueError('Invalid Telegram delivery interval')
        self._client = httpx.AsyncClient(base_url='https://api.telegram.org/bot' + token + '/',
                                         timeout=httpx.Timeout(60, connect=15), transport=transport,
                                         follow_redirects=False, trust_env=False)
        self._lock, self._next, self._interval = asyncio.Lock(), 0.0, minimum_interval
        # httpx emits full request URLs at INFO even when callers never log.
        self._log_filter = _TokenLogFilter(token)
        logging.getLogger('httpx').addFilter(self._log_filter)

    def __repr__(self):
        return 'TelegramDelivery(token=[REDACTED])'

    async def close(self):
        try:
            await self._client.aclose()
        finally:
            logging.getLogger('httpx').removeFilter(self._log_filter)

    async def send(self, invoice, path, filename):
        user_id, invoice_id = invoice.get('user_id'), invoice.get('id')
        if (type(user_id) is not int or not 0 < user_id < 2**53
                or not isinstance(invoice_id, str) or not re.fullmatch(r'[0-9a-f]{32}', invoice_id)
                or invoice.get('plan') not in {'full', 'eco'} or filename != 'device.license.bin'):
            raise ValueError('Invalid private license delivery destination')
        path = Path(path)
        if path.resolve() != Path(invoice.get('output_path', '')).resolve():
            raise ValueError('Delivery file differs from the invoice')
        content = path.read_bytes()
        if (not 0 < len(content) <= 2_000_000
                or hashlib.sha256(content).hexdigest() != invoice.get('output_sha')):
            raise ValueError('Delivery license is missing or changed')
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + self._interval
            try:
                response = await self._client.post('sendDocument', data={
                    'chat_id': str(user_id),
                    'caption': 'Готовая лицензия ' + invoice['plan'].upper() + '. Заказ ' + invoice_id,
                    'protect_content': 'true'}, files={
                    'document': (filename, content, 'application/octet-stream')})
                if response.status_code == 429:
                    try:
                        wait = response.json().get('parameters', {}).get('retry_after')
                        if type(wait) is int and 0 < wait <= 86400:
                            self._next = max(self._next, time.monotonic() + wait)
                    except Exception:
                        pass
                response.raise_for_status()
                result = response.json()
                message = result.get('result') if isinstance(result, dict) else None
                if (result.get('ok') is not True or not isinstance(message, dict)
                        or type(message.get('message_id')) is not int or message['message_id'] <= 0
                        or message.get('chat', {}).get('id') != user_id
                        or message.get('chat', {}).get('type') != 'private'
                        or not isinstance(message.get('document', {}).get('file_id'), str)
                        or not message['document']['file_id']):
                    raise ValueError('Telegram did not confirm the intended private document delivery')
                return str(message['message_id'])
            except Exception as exc:
                # HTTP exceptions contain the bot token in their request URL.
                raise RuntimeError('Telegram document delivery failed: ' + type(exc).__name__) from None


class RuntimeWorker:
    """One restart-safe worker, with verification independent from purchase I/O."""

    def __init__(self, ledger, watcher, engine, *, lock_path=None, health_path=None,
                 payment_interval=10, fulfillment_interval=1, delivery_concurrency=4,
                 shutdown_timeout=10, clock=time.monotonic):
        if engine.ledger is not ledger or watcher.ledger is not ledger:
            raise ValueError('Worker components must share exactly one ledger')
        if any(not isinstance(value, (int, float)) or not 0 < value <= 300
               for value in (payment_interval, fulfillment_interval, shutdown_timeout)):
            raise ValueError('Invalid worker polling interval')
        if type(delivery_concurrency) is not int or not 1 <= delivery_concurrency <= 32:
            raise ValueError('Invalid delivery concurrency')
        self.ledger, self.watcher, self.engine = ledger, watcher, engine
        self.lock_path = Path(lock_path) if lock_path else ledger.path.with_suffix('.worker.lock')
        self.health_path = Path(health_path) if health_path else ledger.path.parent / 'runtime-health.json'
        self.payment_interval, self.fulfillment_interval = payment_interval, fulfillment_interval
        self.shutdown_timeout, self.clock = shutdown_timeout, clock
        self._visa_lock, self._delivery_slots = asyncio.Lock(), asyncio.Semaphore(delivery_concurrency)
        self._jobs, self._retry_at, self._failures = {}, {}, {}
        self._payment_ok, self._payment_error, self._work_error = None, None, None
        self.started = asyncio.Event()

    def readiness(self):
        result = ProductionReadiness.inspect(self.ledger, self.engine.store, self.engine.delivery)
        missing = result.missing
        if self.engine.live_purchase_enabled is not True:
            missing += ('live_purchase_enabled',)
        return ProductionReadiness(not missing, missing)

    def health(self):
        report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                  'mode': 'simulation' if self.ledger.simulation else 'live',
                  'payment_scan_ok': self._payment_ok, 'payment_error_type': self._payment_error,
                  'worker_error_type': self._work_error, 'active_jobs': len(self._jobs),
                  'live_purchase_enabled': bool(self.engine.live_purchase_enabled),
                  'automatic_purchase_ready': self.readiness().ready,
                  'readiness_missing': list(self.readiness().missing)}
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        part = self.health_path.with_suffix('.part')
        part.write_text(json.dumps(report, indent=2), encoding='utf-8')
        part.replace(self.health_path)
        return report

    async def verification_once(self):
        try:
            result = await self.watcher.scan()
            self._payment_ok = not bool(self.watcher.last_errors)
            self._payment_error = self.watcher.last_errors[0]['error_type'] if self.watcher.last_errors else None
            return result
        except Exception as exc:
            self._payment_ok, self._payment_error = False, type(exc).__name__
            logging.getLogger(__name__).warning('Payment scan deferred: %s', type(exc).__name__)
            return None

    async def _invoice_job(self, invoice_id):
        try:
            before = self.ledger.get(invoice_id)
            gate = self._visa_lock if before['state'] in CARD_STATES else self._delivery_slots
            async with gate:
                before = self.ledger.get(invoice_id)
                if before['state'] == 'purchase_review':
                    if before['supplier_ref']:
                        await self.engine.reconcile(invoice_id)
                else:
                    await self.engine.step(invoice_id)
            after = self.ledger.get(invoice_id)
            if before['state'] == after['state']:
                failures = self._failures.get(invoice_id, 0) + 1
                self._failures[invoice_id] = failures
                self._retry_at[invoice_id] = self.clock() + min(5 * 2 ** min(failures - 1, 6), 300)
            else:
                self._failures.pop(invoice_id, None)
                self._retry_at.pop(invoice_id, None)
        except Exception as exc:
            failures = self._failures.get(invoice_id, 0) + 1
            self._failures[invoice_id] = failures
            self._retry_at[invoice_id] = self.clock() + min(5 * 2 ** min(failures - 1, 6), 300)
            self._work_error = type(exc).__name__
            logging.getLogger(__name__).warning('License worker job deferred: %s', type(exc).__name__)

    async def fulfillment_once(self):
        started = 0
        for row in self.ledger.list_by_states(WORK_STATES, limit=1000):
            invoice_id = row['id']
            if invoice_id in self._jobs or self._retry_at.get(invoice_id, 0) > self.clock():
                continue
            task = asyncio.create_task(self._invoice_job(invoice_id))
            self._jobs[invoice_id] = task
            task.add_done_callback(lambda completed, key=invoice_id: self._jobs.pop(key, None))
            started += 1
        return started

    async def wait_idle(self):
        if self._jobs:
            await asyncio.gather(*list(self._jobs.values()))

    async def _wait(self, stop, interval):
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass

    async def _verification_loop(self, stop):
        while not stop.is_set():
            await self.verification_once()
            await self._wait(stop, self.payment_interval)

    async def _fulfillment_loop(self, stop):
        while not stop.is_set():
            try:
                await self.fulfillment_once()
                self.health()
            except Exception as exc:
                self._work_error = type(exc).__name__
                logging.getLogger(__name__).warning('License worker scheduler deferred: %s', type(exc).__name__)
            await self._wait(stop, self.fulfillment_interval)

    async def run(self, stop=None):
        stop = stop if stop is not None else asyncio.Event()
        self.started.clear()
        if not self.ledger.simulation and self.engine.live_purchase_enabled and not self.readiness().ready:
            raise ValueError('Live purchase prerequisites are not verified')
        os.umask(0o077)
        # Recover only after the cross-process lock is held. A second instance
        # must never turn the first instance's in-flight charge into a retry.
        with WorkerLock(self.lock_path):
            self.ledger.recover()
            self.started.set()
            loops = [asyncio.create_task(self._verification_loop(stop)),
                     asyncio.create_task(self._fulfillment_loop(stop))]
            try:
                await stop.wait()
            finally:
                for task in loops:
                    task.cancel()
                await asyncio.gather(*loops, return_exceptions=True)
                try:
                    await asyncio.wait_for(self.wait_idle(), timeout=self.shutdown_timeout)
                except (TimeoutError, asyncio.CancelledError):
                    for task in list(self._jobs.values()):
                        task.cancel()
                    await asyncio.gather(*list(self._jobs.values()), return_exceptions=True)
                finally:
                    self.started.clear()

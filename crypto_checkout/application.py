"""Explicit production assembly and read-only preflight; no simulated fallback.

The default executor is deliberately absent. Flags cannot fabricate a verified
merchant card integration. --check creates no ledger, uploads, orders or charges.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from io import StringIO
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from aiogram import Bot
from dotenv.parser import parse_stream

from .binance import BinanceReadError
from .bot import run as run_bot
from .checkout import CheckoutService, CheckoutUnavailable
from .drivemods import LicenseExpectation, _cents
from .exchange_observer import DepositHistoryReader
from .fulfillment import Engine
from .ledger import Ledger
from .runtime import ConcurrentWatcher, RuntimeWorker, TelegramDelivery, _TokenLogFilter
from .store_adapter import DriveModsPurchaseClient, DriveModsStoreAdapter, StoreJournal
from .tron import TronReader, address_bytes


ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION_DIRS = ('/var/lib/nextmod-license-bot', '/var/lib/drivemods-license-bot',
                    '/var/lib/nextmode-sw', '/var/lib/nextmode-sw-redirect',
                    '/var/lib/drivemods-crypto-observer', '/var/lib/drivemods-binance-observer',
                    '/var/lib/drivemods-bank-notices', '/var/lib/drivemods-bank-notice-forwarder')
_OWN_DATABASES = {'crypto-orders.sqlite3', 'store-journal.sqlite3'}


class ConfigurationError(ValueError):
    """Safe setting-name-only errors; never include rejected values."""


def _boolean(values, name):
    value = values.get(name) or ''
    if not isinstance(value, str) or value.strip().casefold() not in {'', 'false', '0', 'true', '1'}:
        raise ConfigurationError('Invalid boolean setting: ' + name)
    return value.strip().casefold() in {'true', '1'}


def _string(values, name):
    value = values.get(name) or ''
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ConfigurationError('Invalid setting: ' + name)
    return value


def _path(value, root):
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    # Reject redirecting components before resolve() hides them.
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ConfigurationError('Configuration paths must not use symbolic links')
    return path.resolve()


def validate_data_dir(path, root=ROOT):
    """Validate without creating a directory or opening any database for writes."""
    path, root = Path(path).resolve(), Path(root).resolve()
    if path in {root, root / 'data', root / 'data/crypto-checkout', Path(path.anchor)}:
        raise ConfigurationError('CRYPTO_DATA_DIR must be a dedicated application directory')
    forbidden = [root / 'data/new-license-bot', root / 'data/crypto-checkout/live',
                 root / 'data/crypto-checkout/observer', *(Path(x).resolve() for x in _PRODUCTION_DIRS)]
    if any(path == old or path.is_relative_to(old) or old.is_relative_to(path) for old in forbidden):
        raise ConfigurationError('CRYPTO_DATA_DIR overlaps an existing service data directory')
    if path.exists() and not path.is_dir():
        raise ConfigurationError('CRYPTO_DATA_DIR is not a directory')
    if path.exists():
        if any(child.suffix in {'.db', '.sqlite', '.sqlite3'} and child.name not in _OWN_DATABASES
               for child in path.iterdir()):
            raise ConfigurationError('CRYPTO_DATA_DIR contains another application database')
        existing = path / 'crypto-orders.sqlite3'
        if existing.exists():
            if existing.is_symlink():
                raise ConfigurationError('Crypto ledger must not be a symbolic link')
            try:
                # immutable forbids WAL/shared-memory side effects in --check.
                with closing(sqlite3.connect(existing.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                    metadata = dict(db.execute('SELECT key,value FROM metadata WHERE key IN '
                                               "('simulation','require_exchange_credit')"))
                if metadata != {'simulation': 'False', 'require_exchange_credit': 'True'}:
                    raise ValueError('Wrong ledger policy')
            except Exception:
                raise ConfigurationError('Existing crypto ledger identity or policy is not verified') from None
    return path


def _read_private(path, *, max_bytes=65536):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes
            or os.name != 'nt' and path.stat().st_mode & 0o077):
        raise ConfigurationError('Private configuration file is unavailable or not owner-restricted')
    return path.read_bytes()


def _private_json(path):
    try:
        value = json.loads(_read_private(path).decode('utf-8-sig'))
    except Exception:
        raise ConfigurationError('Private JSON configuration is unavailable or malformed') from None
    if not isinstance(value, dict):
        raise ConfigurationError('Private JSON configuration must be an object')
    return value


@dataclass(frozen=True)
class ApplicationSettings:
    data_dir: Path
    bot_token: str = field(default='', repr=False)
    binance_key: str = field(default='', repr=False)
    binance_secret: str = field(default='', repr=False)
    trongrid_key: str = field(default='', repr=False)
    address: str = field(default='', repr=False)
    session_file: Path | None = field(default=None, repr=False)
    binding_proof_file: Path | None = field(default=None, repr=False)
    expected_bot_id: int = 9258163194
    expected_username: str = 'DriveMods_licensebot'
    store_creates_enabled: bool = False
    live_purchases_enabled: bool = False
    unique_cents_enabled: bool = False
    exclusive_poller_confirmed: bool = False
    region: str | None = None

    @classmethod
    def from_mapping(cls, values, *, root=ROOT):
        root = Path(root).resolve()
        data = _path(_string(values, 'CRYPTO_DATA_DIR'), root) or root / 'data/crypto-checkout/application'
        data = validate_data_dir(data, root)
        token = _string(values, 'CRYPTO_BOT_TOKEN')
        if token and not re.fullmatch(r'[0-9]{6,14}:[A-Za-z0-9_-]{25,}', token):
            raise ConfigurationError('Invalid setting: CRYPTO_BOT_TOKEN')
        key, secret = (_string(values, name) for name in ('BINANCE_READ_API_KEY', 'BINANCE_READ_API_SECRET'))
        if any(value and not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value) for value in (key, secret)):
            raise ConfigurationError('Invalid Binance reading credential format')
        address = _string(values, 'CRYPTO_RECEIVING_ADDRESS')
        if address:
            try:
                address_bytes(address)
            except ValueError:
                raise ConfigurationError('Invalid setting: CRYPTO_RECEIVING_ADDRESS') from None
        network = _string(values, 'CRYPTO_NETWORK').upper()
        if network and network not in {'TRON', 'TRC20', 'TRX'}:
            raise ConfigurationError('CRYPTO_NETWORK must be TRON/TRC20')
        identity = _string(values, 'CRYPTO_EXPECTED_BOT_ID') or '9258163194'
        if not identity.isdecimal() or not 0 < int(identity) < 2**53:
            raise ConfigurationError('Invalid setting: CRYPTO_EXPECTED_BOT_ID')
        username = _string(values, 'CRYPTO_EXPECTED_BOT_USERNAME') or 'DriveMods_licensebot'
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) or not username.casefold().endswith('bot'):
            raise ConfigurationError('Invalid setting: CRYPTO_EXPECTED_BOT_USERNAME')
        region = _string(values, 'CRYPTO_STORE_REGION') or None
        return cls(data, token, key, secret, _string(values, 'TRONGRID_API_KEY'), address,
                   _path(_string(values, 'CRYPTO_STORE_SESSION_FILE'), root),
                   _path(_string(values, 'CRYPTO_STORE_BINDING_PROOF_FILE'), root),
                   int(identity), username,
                   _boolean(values, 'CRYPTO_STORE_CREATES_ENABLED'),
                   _boolean(values, 'CRYPTO_LIVE_PURCHASES_ENABLED'),
                   _boolean(values, 'CRYPTO_ALLOW_UNIQUE_CENT_AMOUNTS'),
                   _boolean(values, 'CRYPTO_EXCLUSIVE_POLLER_CONFIRMED'), region)


def load_settings(env_file=None, *, environ=None, root=ROOT):
    """Explicit private file only; legacy BOT_TOKEN/DATA_DIR are never inherited."""
    values = {}
    if env_file:
        path = _path(str(env_file), root)
        try:
            bindings = list(parse_stream(StringIO(_read_private(path).decode('utf-8-sig'))))
        except Exception:
            raise ConfigurationError('Private environment file could not be read') from None
        if any(binding.error for binding in bindings):
            raise ConfigurationError('Private environment file is malformed')
        for binding in bindings:
            if binding.key:
                if binding.key in values:
                    raise ConfigurationError('Duplicate variable in private environment file')
                values[binding.key] = binding.value or ''
    values.update(os.environ if environ is None else environ)
    return ApplicationSettings.from_mapping(values, root=root)


class ClientFactory:
    """Concrete clients. Tests may explicitly inject a factory through Python."""
    def tron(self, settings): return TronReader(settings.trongrid_key)
    def exchange(self, settings): return DepositHistoryReader(settings.binance_key, settings.binance_secret)
    def merchant(self, settings, *, allow_creates): return DriveModsPurchaseClient(allow_creates=allow_creates)
    def telegram(self, settings): return Bot(token=settings.bot_token)
    def delivery(self, settings): return TelegramDelivery(settings.bot_token)


@dataclass(repr=False)
class Resources:
    tron: object | None = None
    exchange: object | None = None
    merchant: object | None = None
    telegram: object | None = None
    executor: object | None = None


@asynccontextmanager
async def _resources(settings, *, factory=None, executor=None, run_mode=False):
    factory = factory or ClientFactory()
    result = Resources(executor=executor)
    closers = []
    try:
        result.tron = factory.tron(settings)
        closers.append(result.tron.close)
        if settings.binance_key and settings.binance_secret:
            result.exchange = factory.exchange(settings)
            closers.append(result.exchange.close)
        if settings.session_file:
            result.merchant = factory.merchant(settings, allow_creates=run_mode and settings.store_creates_enabled)
            closers.append(result.merchant.close)
        if settings.bot_token:
            result.telegram = factory.telegram(settings)
            closers.append(result.telegram.session.close)
        yield result
    finally:
        for close in reversed(closers):
            try:
                await close()
            except Exception as exc:
                logging.getLogger(__name__).warning('Preflight client close deferred: %s', type(exc).__name__)


async def _resume_merchant(client, settings):
    saved = _private_json(settings.session_file)
    if set(saved) != {'username', 'user_id', 'session_token'}:
        raise ConfigurationError('Saved DriveMods session fields are unsupported')
    await client.resume_session(saved['session_token'], saved['user_id'], saved['username'])


async def _verify_binding(client, settings):
    if not settings.binding_proof_file:
        return False
    saved = _private_json(settings.binding_proof_file)
    if set(saved) != {'expectation', 'source_path'} or not isinstance(saved['expectation'], dict):
        raise ConfigurationError('Saved supplier binding proof is malformed')
    source_path = _path(saved['source_path'], settings.binding_proof_file.parent)
    if source_path is None:
        raise ConfigurationError('Binding proof source is unavailable')
    source = _read_private(source_path, max_bytes=1_048_576)
    return await client.verify_existing_paid_binding(LicenseExpectation(**saved['expectation']), source)


async def _prices(client):
    # These two public-frontend functions are read-only and take no Device ID.
    group = await client._cloud('getUserPriceGroup', {})
    items = await client._cloud('getUserPriceItems', {})
    if not isinstance(group, dict) or type(group.get('currency')) is not int or group['currency'] != 2:
        return False
    if not isinstance(items, list):
        return False
    found = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('product'), dict):
            return False
        spec = item['product'].get('spec')
        if item.get('isActive') is True and isinstance(spec, str) and spec.casefold() in {'full', 'eco'}:
            plan = spec.casefold()
            if _cents(item.get('price')) == {'full': 9000, 'eco': 6000}[plan]:
                found.add(plan)
    return found == {'full', 'eco'}


async def _preflight(settings, resources):
    checks = {'receiving_address_configured': bool(settings.address),
              'tron_readable': False, 'binance_read_only_history': False,
              'merchant_auth_verified': False, 'merchant_base_usd_prices': False,
              'merchant_device_binding_verified': False, 'telegram_identity_verified': False,
              'automatic_payment_ready': False,
              'store_creates_enabled': settings.store_creates_enabled,
              'live_purchases_enabled': settings.live_purchases_enabled,
              'unique_cents_enabled': settings.unique_cents_enabled,
              'exclusive_poller_confirmed': settings.exclusive_poller_confirmed}
    errors = {}
    async def step(name, operation):
        try:
            checks[name] = (await operation()) is True
        except Exception as exc:
            detail = {'error_type': type(exc).__name__}
            if isinstance(exc, BinanceReadError):
                detail.update(exc.diagnostics())
            errors[name] = detail
    async def tron():
        height = await resources.tron.height()
        return type(height) is int and height > 0
    async def exchange():
        if not resources.exchange or not settings.address:
            return False
        end = int(time.time() * 1000)
        await resources.exchange.history(settings.address, max(0, end - 30 * 60 * 1000), end)
        return True
    async def merchant():
        if not resources.merchant:
            return False
        await _resume_merchant(resources.merchant, settings)
        return resources.merchant.identity is not None
    async def identity():
        if not resources.telegram:
            return False
        log_filter = _TokenLogFilter(settings.bot_token)
        loggers = [logging.getLogger(x) for x in ('aiohttp.client', 'aiogram.dispatcher', 'aiogram.event')]
        for logger in loggers: logger.addFilter(log_filter)
        try:
            user = await resources.telegram.get_me()
            return (user.id == settings.expected_bot_id and user.is_bot is True
                    and (user.username or '').casefold() == settings.expected_username.casefold())
        finally:
            for logger in loggers: logger.removeFilter(log_filter)
    await asyncio.gather(step('tron_readable', tron), step('binance_read_only_history', exchange),
                         step('merchant_auth_verified', merchant), step('telegram_identity_verified', identity))
    if checks['merchant_auth_verified']:
        await step('merchant_base_usd_prices', lambda: _prices(resources.merchant))
        await step('merchant_device_binding_verified', lambda: _verify_binding(resources.merchant, settings))
    if resources.executor is not None:
        try:
            checks['automatic_payment_ready'] = resources.executor.automatic_payment_ready is True
        except Exception as exc:
            errors['automatic_payment_ready'] = {'error_type': type(exc).__name__}
    return {'ready': all(checks.values()), 'checks': checks,
            'missing': [name for name, passed in checks.items() if not passed],
            'errors': errors, 'server_payment_executor_missing': resources.executor is None,
            'effects': {'customer_invoices_created': 0, 'device_uploads': 0,
                        'store_orders_created': 0, 'card_charges': 0,
                        'telegram_messages_sent': 0, 'telegram_pollers_started': 0}}


async def check(settings, *, factory=None, executor=None):
    """Read-only connectivity/readiness check; no Ledger or StoreJournal exists."""
    validate_data_dir(settings.data_dir)
    async with _resources(settings, factory=factory, executor=executor) as resources:
        return await _preflight(settings, resources)


async def run(settings, *, factory=None, executor=None, stop=None):
    """A controlled cutover entry point, not an automatic operator-bot replacement."""
    validate_data_dir(settings.data_dir)
    factory = factory or ClientFactory()
    async with _resources(settings, factory=factory, executor=executor, run_mode=True) as resources:
        report = await _preflight(settings, resources)
        if report['ready'] is not True:
            raise CheckoutUnavailable('Production prerequisites missing: ' + ', '.join(report['missing']))
        os.umask(0o077)
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt': settings.data_dir.chmod(0o700)
        ledger = Ledger(settings.data_dir / 'crypto-orders.sqlite3', simulation=False, require_exchange_credit=True)
        journal = StoreJournal(settings.data_dir / 'store-journal.sqlite3')
        store = DriveModsStoreAdapter(resources.merchant, ledger, journal, purchases_enabled=True,
                                     executor=resources.executor, region=settings.region)
        delivery = factory.delivery(settings)
        try:
            watcher = ConcurrentWatcher(ledger, resources.tron, resources.exchange)
            engine = Engine(ledger, store, delivery, live_purchase_enabled=True)
            worker = RuntimeWorker(ledger, watcher, engine)
            checkout = CheckoutService(ledger, settings.data_dir / 'uploads', resources.tron,
                                       worker.readiness, address=settings.address, allow_unique_cents=True)
            await run_bot(checkout, worker, token=settings.bot_token,
                          expected_bot_id=settings.expected_bot_id, expected_username=settings.expected_username,
                          exclusive_poller_confirmed=settings.exclusive_poller_confirmed, stop=stop)
        finally:
            await delivery.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Read-only preflight; creates no ledger or customer orders')
    mode.add_argument('--run', action='store_true', help='Requires verified production readiness and exclusive bot ownership')
    parser.add_argument('--env-file', type=Path, help='Optional explicit owner-only configuration file')
    args = parser.parse_args()
    try:
        settings = load_settings(args.env_file)
        if args.check:
            report = asyncio.run(check(settings))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ready'] else 2
        asyncio.run(run(settings))
        return 0
    except (ConfigurationError, CheckoutUnavailable) as exc:
        print(json.dumps({'ready': False, 'error': str(exc)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({'ready': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

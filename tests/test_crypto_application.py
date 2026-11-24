"""Production assembly checks run with explicit offline dependency factories."""
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from crypto_checkout import application
from crypto_checkout.application import ApplicationSettings, ConfigurationError, check, load_settings, run
from crypto_checkout.checkout import CheckoutUnavailable
from crypto_checkout.demo import TEST_ADDRESS
from crypto_checkout.ledger import Ledger


TOKEN = '123456789:' + 'TEST_ONLY_NOT_A_REAL_TOKEN_0000000000000'


def private_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding='utf-8')
    path.chmod(0o600)
    return path


def environment(tmp_path, *, enabled=False, proof=False):
    session = private_json(tmp_path / 'private/store-session.json', {
        'username': 'PRIVATE-TEST-USERNAME', 'user_id': 'dealer001', 'session_token': 'PRIVATE-TEST-SESSION'})
    values = {'CRYPTO_DATA_DIR': str(tmp_path / 'new-checkout-state'), 'CRYPTO_BOT_TOKEN': TOKEN,
              'BINANCE_READ_API_KEY': 'K' * 64, 'BINANCE_READ_API_SECRET': 'S' * 64,
              'CRYPTO_RECEIVING_ADDRESS': TEST_ADDRESS, 'CRYPTO_NETWORK': 'TRON',
              'CRYPTO_STORE_SESSION_FILE': str(session)}
    if enabled:
        for name in ('CRYPTO_STORE_CREATES_ENABLED', 'CRYPTO_LIVE_PURCHASES_ENABLED',
                     'CRYPTO_ALLOW_UNIQUE_CENT_AMOUNTS', 'CRYPTO_EXCLUSIVE_POLLER_CONFIRMED'):
            values[name] = 'true'
    if proof:
        source = tmp_path / 'private/device.id.bin'
        source.write_bytes(b'KNOWN TEST DEVICE ID; NEVER UPLOAD TO REAL STORE')
        source.chmod(0o600)
        path = private_json(tmp_path / 'private/binding-proof.json', {
            'expectation': {'license_id': 'license001', 'payment_id': 'payment001', 'owner_id': 'dealer001',
                            'plan': 'full', 'product': 'GEN5', 'bundle': 'FULL', 'region': None,
                            'device_id': 'TEST-DEVICE', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                            'cost_cents': 9000}, 'source_path': str(source)})
        values['CRYPTO_STORE_BINDING_PROOF_FILE'] = str(path)
    return values


class FakeClient:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


class Factory:
    def __init__(self, *, bad_identity=False, failing_exchange=False):
        self.created, self.calls, self.bad_identity, self.failing_exchange = [], [], bad_identity, failing_exchange

    def tron(self, settings):
        outer = self
        class Chain(FakeClient):
            async def height(self):
                outer.calls.append('tron.height')
                return 1000
        client = Chain()
        self.created.append(client)
        return client

    def exchange(self, settings):
        outer = self
        class Exchange(FakeClient):
            async def history(self, address, start, end):
                outer.calls.append('exchange.history')
                assert address == TEST_ADDRESS and 0 <= end - start < 90 * 86400 * 1000
                if outer.failing_exchange: raise RuntimeError('SECRET RESPONSE WITH CREDENTIALS')
                return []
        client = Exchange()
        self.created.append(client)
        return client

    def merchant(self, settings, *, allow_creates):
        outer = self
        class Merchant(FakeClient):
            identity = None
            merchant_device_binding_verified = False
            async def resume_session(self, token, owner, username):
                outer.calls.append('merchant.resume')
                assert token == 'PRIVATE-TEST-SESSION'
                self.identity = SimpleNamespace(user_id=owner, username=username)
            async def _cloud(self, name, params):
                outer.calls.append('merchant.' + name)
                assert params == {} and name in {'getUserPriceGroup', 'getUserPriceItems'}
                if name == 'getUserPriceGroup': return {'currency': 2}
                return [{'isActive': True, 'price': Decimal('90.00'), 'product': {'spec': 'FULL'}},
                        {'isActive': True, 'price': Decimal('60.00'), 'product': {'spec': 'ECO'}}]
            async def verify_existing_paid_binding(self, expected, source):
                outer.calls.append('merchant.verify_binding_readonly')
                assert expected.owner_id == self.identity.user_id
                assert hashlib.sha256(source).hexdigest() == expected.source_sha256
                self.merchant_device_binding_verified = True
                return True
        client = Merchant()
        client.allow_creates = allow_creates
        self.created.append(client)
        self.calls.append('merchant.constructor.allow_creates=' + str(allow_creates))
        return client

    def telegram(self, settings):
        outer = self
        class Telegram:
            def __init__(self): self.session = FakeClient()
            async def get_me(self):
                outer.calls.append('telegram.get_me')
                return SimpleNamespace(id=9227738530 if outer.bad_identity else settings.expected_bot_id,
                                       is_bot=True, username=settings.expected_username)
        client = Telegram()
        self.created.append(client.session)
        return client

    def delivery(self, settings):
        self.calls.append('delivery.constructor')
        class Delivery(FakeClient): simulation = False
        client = Delivery()
        self.created.append(client)
        return client


def test_settings_hide_secrets_and_ignore_legacy_bot_configuration(tmp_path):
    values = environment(tmp_path)
    values.update(BOT_TOKEN='OLD BOT TOKEN MUST NOT BE USED', DATA_DIR='old-data', APP_MODE='live')
    settings = ApplicationSettings.from_mapping(values, root=tmp_path)
    printed = repr(settings)
    for secret in (TOKEN, 'K' * 64, 'S' * 64, TEST_ADDRESS): assert secret not in printed
    assert settings.bot_token == TOKEN and settings.data_dir == tmp_path / 'new-checkout-state'
    assert not settings.store_creates_enabled and not settings.live_purchases_enabled
    assert not settings.unique_cents_enabled and not settings.exclusive_poller_confirmed


@pytest.mark.parametrize('setting,value', [('CRYPTO_BOT_TOKEN', 'INVALID PRIVATE TOKEN'),
    ('BINANCE_READ_API_SECRET', 'bad-secret'), ('CRYPTO_NETWORK', 'ERC20'),
    ('CRYPTO_RECEIVING_ADDRESS', 'invalid-wallet'), ('CRYPTO_LIVE_PURCHASES_ENABLED', 'yes-run-now'),
    ('CRYPTO_EXPECTED_BOT_ID', 'not-number')])
def test_bad_setting_errors_never_echo_the_rejected_value(tmp_path, setting, value):
    values = environment(tmp_path)
    values[setting] = value
    with pytest.raises(ConfigurationError) as exc:
        ApplicationSettings.from_mapping(values, root=tmp_path)
    assert value not in str(exc.value)


@pytest.mark.parametrize('relative', ['.', 'data', 'data/new-license-bot', 'data/new-license-bot/nested',
                                       'data/crypto-checkout/live'])
def test_active_or_nondedicated_data_directories_are_rejected(tmp_path, relative):
    with pytest.raises(ConfigurationError):
        ApplicationSettings.from_mapping({'CRYPTO_DATA_DIR': str(tmp_path / relative)}, root=tmp_path)


def test_an_existing_production_database_is_never_reused(tmp_path):
    path = tmp_path / 'another-application'
    path.mkdir()
    production = path / 'orders.sqlite3'
    production.write_bytes(b'PRODUCTION DATA DO NOT MODIFY')
    with pytest.raises(ConfigurationError):
        ApplicationSettings.from_mapping({'CRYPTO_DATA_DIR': str(path)}, root=tmp_path)
    assert production.read_bytes() == b'PRODUCTION DATA DO NOT MODIFY'


def test_existing_simulation_database_cannot_become_the_live_ledger(tmp_path):
    path = tmp_path / 'another-application'
    Ledger(path / 'crypto-orders.sqlite3', simulation=True)
    with pytest.raises(ConfigurationError, match='identity or policy'):
        ApplicationSettings.from_mapping({'CRYPTO_DATA_DIR': str(path)}, root=tmp_path)


def test_explicit_environment_file_has_no_interpolation_and_process_env_can_override(tmp_path):
    source = tmp_path / 'private.env'
    source.write_text('CRYPTO_DATA_DIR=first\nCRYPTO_STORE_REGION=${NOT_EXPANDED}\n', encoding='utf-8')
    source.chmod(0o600)
    settings = load_settings(source, environ={'CRYPTO_DATA_DIR': str(tmp_path / 'dedicated')}, root=tmp_path)
    assert settings.data_dir == tmp_path / 'dedicated' and settings.region == '${NOT_EXPANDED}'
    source.write_text('CRYPTO_DATA_DIR=one\nCRYPTO_DATA_DIR=two\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='Duplicate'):
        load_settings(source, environ={}, root=tmp_path)


def test_readonly_check_creates_no_directory_ledger_order_or_upload(tmp_path, monkeypatch):
    settings = ApplicationSettings.from_mapping(environment(tmp_path, enabled=True), root=tmp_path)
    factory = Factory()
    def forbidden(*args, **kwargs): raise AssertionError('Read-only check constructed a database')
    monkeypatch.setattr(application, 'Ledger', forbidden)
    monkeypatch.setattr(application, 'StoreJournal', forbidden)
    report = asyncio.run(check(settings, factory=factory))
    assert not report['ready'] and report['server_payment_executor_missing'] is True
    assert report['checks']['binance_read_only_history'] is True
    assert report['checks']['merchant_auth_verified'] is True
    assert report['checks']['merchant_base_usd_prices'] is True
    assert report['checks']['telegram_identity_verified'] is True
    assert not settings.data_dir.exists() and all(value == 0 for value in report['effects'].values())
    assert 'merchant.constructor.allow_creates=False' in factory.calls
    assert 'delivery.constructor' not in factory.calls
    assert all(client.closed for client in factory.created)
    serialized = json.dumps(report)
    assert TOKEN not in serialized and 'PRIVATE-TEST-' not in serialized and TEST_ADDRESS not in serialized


def test_online_failure_is_isolated_and_reports_no_response_or_secret(tmp_path):
    settings = ApplicationSettings.from_mapping(environment(tmp_path), root=tmp_path)
    factory = Factory(failing_exchange=True)
    report = asyncio.run(check(settings, factory=factory))
    assert report['checks']['merchant_auth_verified'] and report['checks']['telegram_identity_verified']
    assert report['errors']['binance_read_only_history'] == {'error_type': 'RuntimeError'}
    assert 'SECRET RESPONSE' not in json.dumps(report) and not settings.data_dir.exists()


def test_wrong_bot_identity_never_becomes_ready(tmp_path):
    settings = ApplicationSettings.from_mapping(environment(tmp_path, enabled=True, proof=True), root=tmp_path)
    report = asyncio.run(check(settings, factory=Factory(bad_identity=True),
                               executor=SimpleNamespace(automatic_payment_ready=True)))
    assert report['ready'] is False and report['checks']['telegram_identity_verified'] is False
    assert not settings.data_dir.exists()


def test_readonly_existing_binding_proof_can_be_checked_without_creating_customer_orders(tmp_path):
    settings = ApplicationSettings.from_mapping(environment(tmp_path, enabled=True, proof=True), root=tmp_path)
    factory = Factory()
    report = asyncio.run(check(settings, factory=factory, executor=SimpleNamespace(automatic_payment_ready=True)))
    assert report['ready'] is True and report['server_payment_executor_missing'] is False
    assert 'merchant.verify_binding_readonly' in factory.calls
    assert not settings.data_dir.exists() and not any('create' in call and 'constructor' not in call for call in factory.calls)


def test_run_cannot_be_enabled_by_environment_flags_without_real_executor(tmp_path, monkeypatch):
    settings = ApplicationSettings.from_mapping(environment(tmp_path, enabled=True, proof=True), root=tmp_path)
    def forbidden(*args, **kwargs): raise AssertionError('Not-ready run created a ledger')
    monkeypatch.setattr(application, 'Ledger', forbidden)
    with pytest.raises(CheckoutUnavailable, match='automatic_payment_ready'):
        asyncio.run(run(settings, factory=Factory()))
    assert not settings.data_dir.exists()


def test_only_verified_run_assembles_real_components_and_one_bot_lifecycle(tmp_path, monkeypatch):
    settings = ApplicationSettings.from_mapping(environment(tmp_path, enabled=True, proof=True), root=tmp_path)
    factory, executor, calls = Factory(), SimpleNamespace(automatic_payment_ready=True), []
    async def fake_run_bot(checkout, worker, **options):
        calls.append(options)
        assert checkout.ledger is worker.ledger and not worker.ledger.simulation
        assert worker.ledger.require_exchange_credit is True and worker.readiness().ready
        assert worker.engine.store.executor is executor
        assert worker.engine.store.journal.path == settings.data_dir / 'store-journal.sqlite3'
        assert options['exclusive_poller_confirmed'] is True and options['expected_bot_id'] == 9258163194
    monkeypatch.setattr(application, 'run_bot', fake_run_bot)
    asyncio.run(run(settings, factory=factory, executor=executor))
    assert len(calls) == 1 and (settings.data_dir / 'crypto-orders.sqlite3').is_file()
    assert (settings.data_dir / 'uploads').is_dir() and 'delivery.constructor' in factory.calls
    assert all(client.closed for client in factory.created)

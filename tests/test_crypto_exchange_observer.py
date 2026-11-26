"""Exchange connectivity remains read-only, bounded, durable and distinct from orders."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from crypto_checkout.demo import TEST_ADDRESS
from crypto_checkout.exchange_observer import (
    DepositHistoryReader, ExchangeObservations, ObservedDeposit, WINDOW_MS, cycle, own_deposit,
)


def permissions(**overrides):
    return {'enableReading': True, 'enableWithdrawals': False, 'enableSpotAndMarginTrading': False,
            'enableFutures': False, 'enableMargin': False, 'enableInternalTransfer': False,
            'permitsUniversalTransfer': False, 'enableVanillaOptions': False,
            'enableFixApiTrade': False, 'enablePortfolioMarginTrading': False, **overrides}


def row(index=1, **changes):
    return {'id': str(index), 'txId': f'{index:064x}', 'coin': 'USDT', 'network': 'TRX',
            'address': TEST_ADDRESS, 'addressTag': '', 'amount': '100.01',
            'insertTime': 100_000, 'status': 1, 'travelRuleStatus': 0, **changes}


def read_history(handler, **kwargs):
    async def go():
        reader = DepositHistoryReader('test-only-key', 'test-only-secret',
                                      transport=httpx.MockTransport(handler), clock=lambda: 1000)
        try:
            return await reader.history(TEST_ADDRESS, 0, 1_000_000, **kwargs)
        finally:
            await reader.close()
    return asyncio.run(go())


def test_complete_pagination_keeps_exact_amount_and_status_and_filters_other_networks():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        if request.url.path.endswith('apiRestrictions'):
            return httpx.Response(200, json=permissions())
        offset = int(request.url.params['offset'])
        pages = {0: [row(1), row(2, network='ETH')], 2: [row(3, status=0)]}
        return httpx.Response(200, json=pages[offset])
    deposits = read_history(handler, page_size=2)
    assert len(deposits) == 2 and deposits[0].amount_atoms == 100_010_000
    assert deposits[0].available and not deposits[1].available
    assert [r.url.params.get('offset') for r in requests[1:]] == ['0', '2']
    assert all(r.url.params['startTime'] == '0' and r.url.params['endTime'] == '1000000'
               for r in requests[1:])


def test_privileged_key_never_reads_history():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=permissions(enableWithdrawals=True))
    with pytest.raises(ValueError):
        read_history(handler)
    assert calls == ['/sapi/v1/account/apiRestrictions']


@pytest.mark.parametrize('kind', ['repeat', 'limit', 'oversize', 'not_list'])
def test_incomplete_or_repeated_pages_fail_closed(kind):
    def handler(request):
        if request.url.path.endswith('apiRestrictions'):
            return httpx.Response(200, json=permissions())
        offset = int(request.url.params['offset'])
        values = {'repeat': [row(1)], 'limit': [row(offset + 1)],
                  'oversize': [row(1), row(2)], 'not_list': {'rows': []}}
        return httpx.Response(200, json=values[kind])
    with pytest.raises(ValueError):
        read_history(handler, page_size=1, max_pages=2)


@pytest.mark.parametrize('changes', [
    {'status': True}, {'status': 900}, {'travelRuleStatus': False},
    {'insertTime': True}, {'insertTime': 2_000_000}, {'amount': 'NaN'},
    {'amount': '1.0000001'}, {'amount': '-1'}, {'amount': 1},
    {'txId': 'not-a-chain-tx'}, {'id': ''}, {'addressTag': 'tag'},
])
def test_malformed_relevant_deposits_are_never_counted(changes):
    with pytest.raises(ValueError):
        own_deposit(row(**changes), TEST_ADDRESS, 0, 1_000_000)


def test_unknown_compliance_status_never_becomes_available():
    assert not own_deposit(row(travelRuleStatus=None), TEST_ADDRESS, 0, 1_000_000).available
    assert not own_deposit(row(status=6), TEST_ADDRESS, 0, 1_000_000).available


def test_network_failure_does_not_expose_signed_url_or_key():
    def handler(request):
        raise httpx.ConnectError('sensitive query and credential', request=request)
    with pytest.raises(RuntimeError) as exc:
        read_history(handler)
    assert str(exc.value) == 'Binance read failed: ConnectError'


def test_first_start_is_durable_and_status_update_rescans_old_pending_rows(tmp_path):
    now = [100.0]
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: now[0])
    class Reader:
        status = 0
        async def history(self, address, start, end):
            assert address == TEST_ADDRESS and start <= 100_000 <= end
            return [own_deposit(row(status=self.status), address, start, end)]
    reader = Reader()
    first = asyncio.run(cycle(reader, store))
    assert first['ok'] and first['scan_complete'] and first['available_deposits'] == 0
    now[0] += 200 * 86400
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: now[0])
    reader.status = 1
    calls = []
    async def history(address, start, end):
        calls.append((start, end))
        assert 0 <= end - start < 90 * 86400 * 1000
        return [own_deposit(row(), address, start, end)] if start <= 100_000 <= end else []
    reader.history = history
    result = asyncio.run(cycle(reader, store))
    assert len(calls) == 3 and calls[0][0] == 100_000
    assert result['available_deposits'] == 1 and result['observed_deposits'] == 1
    assert result['payment_confirmed_for_orders'] is False
    assert result['license_purchase_enabled'] is False


def test_failed_window_does_not_advance_cursor_or_save_partial_deposits(tmp_path):
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: 100)
    first_window = store.window()
    class Reader:
        async def history(self, *args):
            raise RuntimeError('connection lost')
    with pytest.raises(RuntimeError):
        asyncio.run(cycle(Reader(), store))
    assert store.window() == first_window
    assert store.health(ok=False)['observed_deposits'] == 0


def test_unique_id_and_transaction_identity_and_window_commit_are_atomic(tmp_path):
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: 100)
    start, end, sweep = store.window()
    original = own_deposit(row(), TEST_ADDRESS, start, end)
    conflicting = ObservedDeposit('different-id', original.txid, TEST_ADDRESS,
                                  original.amount_atoms, original.inserted_ms, 1, 0)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_window([original, conflicting], start, end, sweep)
    assert store.window() == (start, end, sweep)
    assert store.health(ok=False)['observed_deposits'] == 0


def test_existing_deposit_cannot_change_amount_or_txid(tmp_path):
    now = [100.0]
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: now[0])
    original = own_deposit(row(), TEST_ADDRESS, 100_000, 100_000)
    store.save_window([original], *store.window())
    now[0] = 101
    changed = ObservedDeposit(original.deposit_id, original.txid, TEST_ADDRESS,
                              original.amount_atoms + 1, original.inserted_ms, 1, 0)
    with pytest.raises(ValueError):
        store.save_window([changed], *store.window())
    assert store.health(ok=False)['available_deposits'] == 1


def test_window_cursor_resumes_large_sweep_after_restart(tmp_path):
    now = [100.0]
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: now[0])
    store.save_window([], *store.window())
    now[0] += 200 * 86400
    class Reader:
        async def history(self, *args): return []
    result = asyncio.run(cycle(Reader(), store, windows_per_cycle=1))
    assert result['scan_complete'] is False
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: now[0])
    assert store.window()[0] == 100_000 + WINDOW_MS
    assert asyncio.run(cycle(Reader(), store))['scan_complete'] is True


def test_health_contains_no_financial_identifiers(tmp_path):
    store = ExchangeObservations(tmp_path, TEST_ADDRESS, clock=lambda: 100)
    store.save_window([own_deposit(row(), TEST_ADDRESS, 100_000, 100_000)], *store.window())
    report = store.health(ok=True, scan_complete=True)
    saved = (tmp_path / 'exchange-health.json').read_text()
    assert json.loads(saved) == report
    assert TEST_ADDRESS not in saved and f'{1:064x}' not in saved and '100.01' not in saved

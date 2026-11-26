"""Read-only TRON mainnet USDT verification. Never signs or broadcasts transfers."""
from dataclasses import dataclass
import asyncio
from email.utils import parsedate_to_datetime
import hashlib
import math
import re
import time
from urllib.parse import urlparse

import httpx

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
USDT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
TRANSFER = 'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


class HistoryPageLimit(RuntimeError):
    """The caller must narrow its time interval, never advance a partial cursor."""


class TronRateLimited(RuntimeError):
    """Read deferred locally until the provider's cooldown has elapsed."""


def address_bytes(address: str) -> bytes:
    if not isinstance(address, str) or not re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', address):
        raise ValueError('Invalid TRON address')
    number = 0
    for character in address:
        number = number * 58 + ALPHABET.index(character)
    data = number.to_bytes((number.bit_length() + 7) // 8, 'big')
    if len(data) != 25 or data[0] != 0x41 or hashlib.sha256(hashlib.sha256(data[:-4]).digest()).digest()[:4] != data[-4:]:
        raise ValueError('TRON address checksum failed')
    return data[:-4]


def address_from_bytes(data: bytes) -> str:
    if len(data) != 21 or data[0] != 0x41:
        raise ValueError('Invalid TRON address bytes')
    payload = data + hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    number, result = int.from_bytes(payload, 'big'), ''
    while number:
        number, remainder = divmod(number, 58)
        result = ALPHABET[remainder] + result
    return result


@dataclass(frozen=True)
class Transfer:
    txid: str
    log_index: int
    address: str
    amount_atoms: int
    block: int
    timestamp_ms: int
    receipt_sha256: str


def parse_solid_receipt(receipt: dict, expected_txid: str) -> list[Transfer]:
    """Only call with a response from walletsolidity, never user-supplied JSON."""
    import json
    if not re.fullmatch(r'[0-9a-f]{64}', expected_txid):
        raise ValueError('Invalid transaction ID')
    if not receipt:
        return []  # Still pending/unknown, never paid or definitely failed.
    if receipt.get('id') != expected_txid or receipt.get('Error'):
        raise ValueError('Unexpected receipt identity')
    if receipt.get('result') == 'FAILED' or receipt.get('receipt', {}).get('result') != 'SUCCESS':
        return []
    block, timestamp = receipt.get('blockNumber'), receipt.get('blockTimeStamp')
    if type(block) is not int or block <= 0 or type(timestamp) is not int or timestamp <= 0:
        raise ValueError('Missing confirmation metadata')
    events = receipt.get('log', [])
    if not isinstance(events, list):
        raise ValueError('Invalid event list')
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    contract_hex = address_bytes(USDT)[1:].hex()
    result = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get('address', '').lower() != contract_hex:
            continue
        topics, value = event.get('topics'), event.get('data')
        if not isinstance(topics, list) or len(topics) != 3 or topics[0].lower() != TRANSFER:
            continue
        if not all(isinstance(topic, str) and re.fullmatch(r'[0-9a-fA-F]{64}', topic) for topic in topics):
            raise ValueError('Malformed USDT transfer topics')
        if topics[1][:24] != '0' * 24 or topics[2][:24] != '0' * 24:
            raise ValueError('Malformed indexed address')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
            raise ValueError('Malformed USDT amount')
        amount = int(value, 16)
        if amount <= 0:
            continue
        address = address_from_bytes(bytes.fromhex('41' + topics[2][-40:]))
        result.append(Transfer(expected_txid, index, address, amount, block, timestamp, digest))
    return result


class TronReader:
    def __init__(self, api_key: str = '', *, base_url='https://api.trongrid.io', transport=None,
                 minimum_interval=1.1, clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep):
        url = urlparse(base_url)
        if url.scheme != 'https' or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Use an HTTPS TRON node origin')
        if (type(minimum_interval) not in (int, float) or not math.isfinite(minimum_interval)
                or not 0 <= minimum_interval <= 300):
            raise ValueError('Invalid TRON request interval')
        # TronGrid charges credits per request, not one credit per HTTP call:
        # history=200 and solidity reads=80. Serialize all paths through one gate
        # so concurrent invoices and pagination cannot burst past the quota.
        self._lock = asyncio.Lock()
        self._next = self._cooldown = 0.0
        self._interval, self._clock, self._wall_clock, self._sleep = minimum_interval, clock, wall_clock, sleep
        self.client = httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=20, follow_redirects=False,
                                       headers={'TRON-PRO-API-KEY': api_key} if api_key else {},
                                       transport=transport, trust_env=False)

    async def close(self):
        await self.client.aclose()

    def _backoff(self, response):
        delay = 300.0 if response.status_code == 403 else 30.0
        retry = response.headers.get('Retry-After', '')
        try:
            seconds = float(retry)
            if math.isfinite(seconds):
                delay = max(delay, seconds)
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry).timestamp() - self._wall_clock())
            except (ValueError, TypeError, OverflowError):
                pass
        # TronGrid may provide only a JSON "suspended for 29 s" message.
        # Parse a duration, never propagate the provider body into logs.
        match = re.search(r'suspended for\s+(\d{1,9})\s*s', response.text[:4096], re.I)
        if match:
            delay = max(delay, int(match[1]))
        return delay + 1.0

    async def _json(self, method, path, **kwargs):
        async with self._lock:
            if self._clock() < self._cooldown:
                raise TronRateLimited('TRON provider cooldown; payment remains pending')
            # These three node endpoints only read chain data. A transient
            # connection/read failure can be retried without creating a payment.
            # Every attempt still goes through the same frequency gate.
            for attempt in range(3):
                delay = self._next - self._clock()
                if delay > 0:
                    await self._sleep(delay)
                self._next = self._clock() + self._interval
                try:
                    response = await self.client.request(method, path, **kwargs)
                    if response.status_code in (403, 429):
                        self._cooldown = self._clock() + self._backoff(response)
                        raise TronRateLimited('TRON provider cooldown; payment remains pending')
                    response.raise_for_status()
                    return response.json()
                except TronRateLimited:
                    raise
                except httpx.TransportError as exc:
                    if attempt == 2:
                        raise RuntimeError('TRON read failed: ' + type(exc).__name__) from None
                    self._next = max(self._next, self._clock() + 2 ** attempt)
                except Exception as exc:
                    # No request bodies, credentials, or full URLs in errors.
                    raise RuntimeError('TRON read failed: ' + type(exc).__name__) from None

    async def height(self) -> int:
        data = await self._json('POST', '/walletsolidity/getnowblock', json={})
        height = data.get('block_header', {}).get('raw_data', {}).get('number')
        if type(height) is not int or height <= 0:
            raise ValueError('Solidified block is unavailable')
        return height

    async def transfers(self, txid: str) -> list[Transfer]:
        if not re.fullmatch(r'[0-9a-fA-F]{64}', txid):
            raise ValueError('Invalid transaction ID')
        txid = txid.lower()
        receipt = await self._json('POST', '/walletsolidity/gettransactioninfobyid', json={'value': txid})
        return parse_solid_receipt(receipt, txid)

    async def candidates(self, address: str, min_timestamp: int, *, max_timestamp=None, max_pages=10) -> list[str]:
        address_bytes(address)
        if (type(min_timestamp) is not int or min_timestamp < 0
                or (max_timestamp is not None and (type(max_timestamp) is not int or max_timestamp<min_timestamp))
                or type(max_pages) is not int or not 1 <= max_pages <= 1000):
            raise ValueError('Invalid bounded TRON history request')
        params = {'only_confirmed': 'true', 'only_to': 'true', 'limit': 200,
                  'contract_address': USDT, 'min_timestamp': min_timestamp,
                  'order_by': 'block_timestamp,asc'}
        if max_timestamp is not None:
            params['max_timestamp']=max_timestamp
        ids, fingerprints = [], set()
        for _ in range(max_pages):
            data = await self._json('GET', f'/v1/accounts/{address}/transactions/trc20', params=params)
            if data.get('success') is not True or not isinstance(data.get('data'), list):
                raise ValueError('TRON indexer did not confirm success')
            for item in data['data']:
                txid = item.get('transaction_id', '')
                if re.fullmatch(r'[0-9a-fA-F]{64}', txid) and txid.lower() not in ids:
                    ids.append(txid.lower())
            meta = data.get('meta', {})
            if not meta.get('links', {}).get('next'):
                return ids
            fingerprint = meta.get('fingerprint')
            if not isinstance(fingerprint, str) or not fingerprint or fingerprint in fingerprints:
                raise ValueError('Invalid history pagination; no cursor advanced')
            fingerprints.add(fingerprint)
            params['fingerprint'] = fingerprint
        raise HistoryPageLimit('History page limit reached; keep the original time window')

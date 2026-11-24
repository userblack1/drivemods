"""Read-only TRON mainnet USDT verification. Never signs or broadcasts transfers."""
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlparse

import httpx

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
USDT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
TRANSFER = 'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


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
    def __init__(self, api_key: str = '', *, base_url='https://api.trongrid.io', transport=None):
        url = urlparse(base_url)
        if url.scheme != 'https' or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Use an HTTPS TRON node origin')
        self.client = httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=20, follow_redirects=False,
                                       headers={'TRON-PRO-API-KEY': api_key} if api_key else {},
                                       transport=transport, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def _json(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
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

    async def candidates(self, address: str, min_timestamp: int, *, max_pages=10) -> list[str]:
        address_bytes(address)
        params = {'only_confirmed': 'true', 'only_to': 'true', 'limit': 200,
                  'contract_address': USDT, 'min_timestamp': min_timestamp,
                  'order_by': 'block_timestamp,asc'}
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
        raise RuntimeError('History page limit reached; keep the original time window')

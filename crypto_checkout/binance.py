"""Binance deposit history only. No trading, transfers, withdrawals or card access."""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx
from .tron import address_bytes


@dataclass(frozen=True)
class DepositCredit:
    deposit_id: str
    txid: str
    address: str
    amount_atoms: int


def match_credit(rows, transfer):
    if not isinstance(rows,list):
        raise ValueError('Invalid Binance deposit history')
    candidates=[]
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError('Invalid Binance deposit')
        if (row.get('coin')!='USDT' or row.get('network')!='TRX' or row.get('status')!=1
                or row.get('travelRuleStatus',0)!=0 or row.get('addressTag','')
                or str(row.get('txId','')).lower()!=transfer.txid or row.get('address')!=transfer.address):
            continue
        try:
            amount=Decimal(row['amount'])*1_000_000
        except (InvalidOperation,KeyError,TypeError):
            raise ValueError('Invalid Binance amount') from None
        if not amount.is_finite() or amount!=amount.to_integral_value() or int(amount)!=transfer.amount_atoms:
            continue
        if not isinstance(row.get('id'),str) or not row['id']:
            raise ValueError('Missing Binance deposit ID')
        candidates.append(DepositCredit(row['id'],transfer.txid,transfer.address,int(amount)))
    if len(candidates)>1:
        raise ValueError('Ambiguous exchange deposit')
    return candidates[0] if candidates else None


class BinanceReader:
    def __init__(self, api_key, api_secret, *, transport=None, clock=time.time):
        if not api_key or not api_secret:
            raise ValueError('Binance read-only credentials are not configured')
        self._secret=api_secret.encode()
        self._clock=clock
        self._client=httpx.AsyncClient(base_url='https://api.binance.com',
              headers={'X-MBX-APIKEY':api_key},timeout=20,trust_env=False,follow_redirects=False,transport=transport)

    async def close(self):
        await self._client.aclose()

    async def _get(self,path,params):
        allowed={'/sapi/v1/capital/deposit/hisrec','/sapi/v1/account/apiRestrictions'}
        if path not in allowed:
            raise ValueError('This client only reads deposits and key permissions')
        query=urlencode({**params,'timestamp':int(self._clock()*1000),'recvWindow':5000})
        signature=hmac.new(self._secret,query.encode(),hashlib.sha256).hexdigest()
        try:
            response=await self._client.get(path+'?'+query+'&signature='+signature)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError('Binance read failed: '+type(exc).__name__) from None

    async def check_read_only(self):
        permissions=await self._get('/sapi/v1/account/apiRestrictions',{})
        if not isinstance(permissions,dict) or permissions.get('enableReading') is not True:
            raise ValueError('Binance reading permission is required')
        for key in ('enableWithdrawals','enableSpotAndMarginTrading','enableFutures',
                    'enableMargin','enableInternalTransfer','permitsUniversalTransfer','enableVanillaOptions'):
            if permissions.get(key) is not False:
                raise ValueError('Use a verified strictly read-only Binance key')
        return True

    async def credit(self, transfer):
        address_bytes(transfer.address)
        rows=await self._get('/sapi/v1/capital/deposit/hisrec',{'coin':'USDT','txId':transfer.txid,
                           'startTime':max(0,transfer.timestamp_ms-60_000),
                           'endTime':int(self._clock()*1000),'limit':1000})
        return match_credit(rows,transfer)

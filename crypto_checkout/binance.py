"""Binance deposit history only. No trading, transfers, withdrawals or card access."""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx
from .tron import address_bytes


_NETWORK_ERROR_TYPES=frozenset({
    'ConnectError','ReadError','WriteError','CloseError','ConnectTimeout','ReadTimeout',
    'WriteTimeout','PoolTimeout','ProxyError','UnsupportedProtocol','LocalProtocolError',
    'RemoteProtocolError','TooManyRedirects','RequestError','TransportError','TimeoutException',
})


class BinanceReadError(RuntimeError):
    """Only categorical diagnostics; never retain a response, request or message."""

    def __init__(self, failure_type, *, http_status=None, error_code=None, network_type=None):
        if failure_type not in _NETWORK_ERROR_TYPES|{'HTTPStatusError','InvalidJSON','UnexpectedError'}:
            failure_type='UnexpectedError'
        self.http_status=http_status if type(http_status) is int and 100<=http_status<=599 else None
        self.error_code=error_code if type(error_code) is int and -(2**63)<=error_code<2**63 else None
        self.network_type=network_type if network_type in _NETWORK_ERROR_TYPES else None
        super().__init__('Binance read failed: '+failure_type)

    def diagnostics(self):
        return {'http_status':self.http_status,'error_code':self.error_code,'network_type':self.network_type}


@dataclass(frozen=True)
class DepositCredit:
    deposit_id: str
    txid: str
    address: str
    amount_atoms: int


def validate_read_only_permissions(permissions):
    if not isinstance(permissions,dict) or permissions.get('enableReading') is not True:
        raise ValueError('Binance reading permission is required')
    required_disabled=(
        'enableWithdrawals','enableSpotAndMarginTrading','enableFutures','enableMargin',
        'enableInternalTransfer','permitsUniversalTransfer','enableVanillaOptions',
        'enableFixApiTrade','enablePortfolioMarginTrading',
    )
    if any(permissions.get(key) is not False for key in required_disabled):
        raise ValueError('Use a verified strictly read-only Binance key')
    read_flags={'enableReading','enableFixReadOnly'}
    for key,value in permissions.items():
        if not isinstance(key,str):
            raise ValueError('Invalid Binance permission response')
        if key.startswith(('enable','permits')):
            if key in read_flags:
                if type(value) is not bool:
                    raise ValueError('Invalid Binance reading permission')
            elif value is not False:
                # New mutation flags must not silently broaden accepted keys.
                raise ValueError('Unrecognized or enabled Binance mutation permission')


def match_credit(rows, transfer):
    if not isinstance(rows,list):
        raise ValueError('Invalid Binance deposit history')
    candidates=[]
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError('Invalid Binance deposit')
        if (row.get('coin')!='USDT' or row.get('network')!='TRX'
                or type(row.get('status')) is not int or row['status']!=1
                or type(row.get('travelRuleStatus')) is not int or row['travelRuleStatus']!=0
                or row.get('addressTag','')
                or str(row.get('txId','')).lower()!=transfer.txid or row.get('address')!=transfer.address):
            continue
        try:
            if not isinstance(row.get('amount'),str):
                raise ValueError('Invalid Binance amount')
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
        self._permissions_checked_at=None
        self._signing_offset=0.0
        self._offset_checked_at=None
        self._client=httpx.AsyncClient(base_url='https://api.binance.com',
              headers={'X-MBX-APIKEY':api_key},timeout=20,trust_env=False,follow_redirects=False,transport=transport)

    async def close(self):
        await self._client.aclose()

    async def _get(self,path,params):
        allowed={'/sapi/v1/capital/deposit/hisrec','/sapi/v1/account/apiRestrictions'}
        if path not in allowed:
            raise ValueError('This client only reads deposits and key permissions')
        # Only these idempotent GETs are eligible for one timestamp retry.
        # A fresh signature is built after the failed connection/request, never
        # reused after a slow TLS handshake. No card/store operation uses this.
        for attempt in range(2):
            now=self._clock()
            if self._offset_checked_at is None or not 0<=now-self._offset_checked_at<=300:
                self._signing_offset=0.0
                self._offset_checked_at=None
            query=urlencode({**params,'timestamp':int((now+self._signing_offset)*1000),'recvWindow':10000})
            signature=hmac.new(self._secret,query.encode(),hashlib.sha256).hexdigest()
            try:
                response=await self._client.get(path+'?'+query+'&signature='+signature)
            except Exception as exc:
                name=type(exc).__name__
                network_type=name if name in _NETWORK_ERROR_TYPES else None
                raise BinanceReadError(network_type or 'UnexpectedError',network_type=network_type) from None
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                error_code=None
                try:
                    body=response.json()
                    if isinstance(body,dict):
                        error_code=body.get('code')
                except Exception:
                    pass
                if attempt==0 and response.status_code==400 and type(error_code) is int and error_code==-1021:
                    # Date is obtained from the authenticated HTTPS response,
                    # has only second precision, and is rounded down. Measuring
                    # against receive time keeps the retry slightly behind the
                    # server instead of risking its strict 1s-ahead rejection.
                    try:
                        received=self._clock()
                        server=parsedate_to_datetime(response.headers.get('date',''))
                        offset=server.timestamp()-received
                        if server.tzinfo is not None and abs(offset)<=60:
                            self._signing_offset=offset
                            self._offset_checked_at=received
                    except (ValueError,TypeError,OverflowError):
                        pass
                    continue
                raise BinanceReadError('HTTPStatusError',http_status=response.status_code,error_code=error_code) from None
            try:
                return response.json()
            except Exception:
                raise BinanceReadError('InvalidJSON',http_status=response.status_code) from None

    async def check_read_only(self):
        self._permissions_checked_at=None
        permissions=await self._get('/sapi/v1/account/apiRestrictions',{})
        validate_read_only_permissions(permissions)
        self._permissions_checked_at=self._clock()
        return True

    async def credit(self, transfer):
        address_bytes(transfer.address)
        now=self._clock()
        checked=self._permissions_checked_at
        # Do not rely on callers remembering the preflight. Revalidate during a
        # long-running watcher too, since key permissions can change later.
        if checked is None or not 0 <= now-checked < 60:
            await self.check_read_only()
        end_time=int(self._clock()*1000)
        start_time=max(0,transfer.timestamp_ms-60_000)
        if start_time>end_time:
            raise ValueError('Transfer timestamp is ahead of the local clock')
        # Binance requires each requested history window to be shorter than
        # ninety days, even when the query is narrowed to a known TXID.
        end_time=min(end_time,start_time+90*86400*1000-1)
        rows=await self._get('/sapi/v1/capital/deposit/hisrec',{'coin':'USDT','txId':transfer.txid,
                           'startTime':start_time,'endTime':end_time,'limit':1000})
        return match_credit(rows,transfer)

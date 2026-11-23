"""Bound connection waits without replaying an uncertain Telegram request."""
import asyncio

import aiohttp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError


class LicenseApiSession(AiohttpSession):
    def __init__(self, *, connect_attempts=5, connect_timeout=6, retry_delay=0.25, **kwargs):
        super().__init__(**kwargs)
        self.connect_attempts = connect_attempts
        self.connect_timeout = connect_timeout
        self.retry_delay = retry_delay
        self._connector_init["keepalive_timeout"] = 300

    async def make_request(self, bot, method, timeout=None):
        session = await self.create_session()
        request_timeout = aiohttp.ClientTimeout(
            total=self.timeout if timeout is None else timeout,
            connect=self.connect_timeout, sock_connect=self.connect_timeout)
        for attempt in range(self.connect_attempts):
            try:
                async with session.post(self.api.api_url(token=bot.token, method=method.__api_method__),
                                        data=self.build_form_data(bot, method), timeout=request_timeout) as response:
                    raw = await response.text()
            except aiohttp.ClientSSLError as exc:
                raise TelegramNetworkError(method=method, message="TLS certificate verification failed") from exc
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
                if attempt + 1 < self.connect_attempts:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                raise TelegramNetworkError(method=method, message="Cannot connect to Telegram API") from exc
            except asyncio.TimeoutError as exc:
                raise TelegramNetworkError(method=method, message="Request timeout; request was not replayed") from exc
            except aiohttp.ClientError as exc:
                raise TelegramNetworkError(method=method, message=type(exc).__name__ + "; request was not replayed") from exc
            return self.check_response(bot=bot, method=method, status_code=response.status, content=raw).result


def make_bot(token: str) -> Bot:
    return Bot(token, session=LicenseApiSession(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))

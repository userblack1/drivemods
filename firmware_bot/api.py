"""Retry only failed connections; never replay an uncertain Telegram delivery."""
import asyncio

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter


class FirmwareApiSession(AiohttpSession):
    def __init__(self, *, connect_attempts=5, connect_timeout=6, retry_delay=0.25, write_interval=0.04, **kwargs):
        super().__init__(**kwargs)
        self.connect_attempts = connect_attempts
        self.connect_timeout = connect_timeout
        self.retry_delay = retry_delay
        self.write_interval = write_interval
        self._write_lock = asyncio.Lock()
        self._next_write = 0.0
        # Keep a working route open while the supplier prepares its response.
        self._connector_init["keepalive_timeout"] = 300

    async def make_request(self, bot, method, timeout=None):
        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        total = self.timeout if timeout is None else timeout
        request_timeout = aiohttp.ClientTimeout(total=total, connect=self.connect_timeout,
                                                sock_connect=self.connect_timeout)
        for attempt in range(self.connect_attempts):
            if method.__api_method__ in {"sendMessage", "sendPhoto", "sendDocument", "editMessageText", "answerCallbackQuery"}:
                async with self._write_lock:
                    loop = asyncio.get_running_loop()
                    # A timer can wake early. Recheck before allowing a write,
                    # including when several catalog users arrive together.
                    while self._next_write > loop.time():
                        await asyncio.sleep(max(0.001, self._next_write - loop.time()))
                    self._next_write = loop.time() + self.write_interval
            try:
                async with session.post(url, data=self.build_form_data(bot, method), timeout=request_timeout) as response:
                    raw = await response.text()
            except aiohttp.ClientSSLError as exc:
                raise TelegramNetworkError(method=method, message="TLS certificate verification failed") from exc
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
                # The HTTP request has not been transmitted on a connection failure.
                if attempt + 1 < self.connect_attempts:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                raise TelegramNetworkError(method=method, message="Cannot connect to Telegram API") from exc
            except asyncio.TimeoutError as exc:
                raise TelegramNetworkError(method=method, message="Request timeout error; delivery was not retried") from exc
            except aiohttp.ClientError as exc:
                raise TelegramNetworkError(method=method, message=type(exc).__name__ + "; delivery was not retried") from exc
            try:
                return self.check_response(bot=bot, method=method, status_code=response.status, content=raw).result
            except TelegramRetryAfter as exc:
                # Telegram explicitly rejected this request; it was not delivered.
                # Unlike a lost HTTP response, a 429 can safely be retried.
                if attempt + 1 >= self.connect_attempts or not 0 <= exc.retry_after <= 60:
                    raise
                await asyncio.sleep(exc.retry_after + 0.1)


def make_bot(token: str) -> Bot:
    return Bot(token, session=FirmwareApiSession())

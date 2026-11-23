"""Telegram's official TLS WebSocket transport using Telethon's MTProto codec.

Protocol and DC hostnames: https://core.telegram.org/mtproto/transports
"""
import asyncio
from collections import deque

import aiohttp
from telethon.network.connection.tcpfull import ConnectionTcpFull
from telethon.network.connection.tcpobfuscated import ConnectionTcpObfuscated


DC_NAMES = {1: "pluto", 2: "venus", 3: "aurora", 4: "vesta", 5: "flora"}


def websocket_url(dc_id: int) -> str:
    if dc_id not in DC_NAMES:
        raise ValueError("Неизвестный Telegram DC для защищённого WebSocket.")
    return f"wss://{DC_NAMES[dc_id]}.web.telegram.org/apiws"


class WebSocketStream:
    """Expose stream reads/writes over binary WebSocket frames, preserving order."""

    def __init__(self, websocket, session):
        self.websocket = websocket
        self.session = session
        self.buffer = bytearray()
        self.outgoing = deque()
        self.send_lock = asyncio.Lock()
        self.close_task = None

    async def readexactly(self, size):
        while len(self.buffer) < size:
            message = await self.websocket.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                self.buffer.extend(message.data)
            elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                partial = bytes(self.buffer)
                self.buffer.clear()
                raise asyncio.IncompleteReadError(partial, size)
            elif message.type == aiohttp.WSMsgType.TEXT:
                raise ConnectionError("Telegram WebSocket returned a non-binary frame.")
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def write(self, data):
        if self.close_task is not None:
            raise ConnectionError("Telegram WebSocket is closing.")
        self.outgoing.append(bytes(data))

    async def drain(self):
        async with self.send_lock:
            while self.outgoing:
                await self.websocket.send_bytes(self.outgoing.popleft())

    def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())

    async def _close(self):
        try:
            await self.websocket.close()
        finally:
            await self.session.close()

    async def wait_closed(self):
        self.close()
        await asyncio.shield(self.close_task)


class ConnectionWss(ConnectionTcpObfuscated):
    """Full-duplex TLS, with the required native MTProto obfuscation layer."""

    async def _connect(self, timeout=None, ssl=None):
        if self._proxy or self._local_addr:
            raise ValueError("Настройки proxy/local_addr для WebSocket не поддерживаются.")
        url = websocket_url(self._dc_id)
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout or 10))
        try:
            websocket = await asyncio.wait_for(session.ws_connect(
                url, protocols=("binary",), ssl=True, autoping=True, heartbeat=None,
                compress=0, max_msg_size=64 * 1024 * 1024), timeout or 10)
            stream = WebSocketStream(websocket, session)
            self._reader = stream
            self._writer = stream
            self._codec = self.packet_codec(self)
            self._init_conn()
            await stream.drain()
        except BaseException:
            await session.close()
            raise


def connection_type(mode: str):
    if mode == "tcp":
        return ConnectionTcpFull
    if mode == "wss":
        return ConnectionWss
    raise ValueError("TELEGRAM_TRANSPORT: укажите tcp или wss")

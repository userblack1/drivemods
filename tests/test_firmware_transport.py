import asyncio
from collections import defaultdict
import logging
from types import SimpleNamespace

import aiohttp
import pytest
pytest.importorskip("telethon")

from firmware_bot.transport import ConnectionWss, WebSocketStream, connection_type, websocket_url


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.frames = []
        self.closed = False
        self.sent = asyncio.Event()

    async def receive(self):
        return await self.messages.get()

    async def send_bytes(self, data):
        self.frames.append(data)
        if len(self.frames) >= 2:
            self.sent.set()

    async def close(self):
        self.closed = True


class Session:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False
        self.url = None
        self.options = None

    async def close(self):
        self.closed = True

    async def ws_connect(self, url, **options):
        self.url, self.options = url, options
        return self.socket


def test_websocket_frames_are_a_continuous_stream_and_close_preserves_partial_read():
    async def scenario():
        socket = Socket()
        stream = WebSocketStream(socket, Session(socket))
        for data in (b"abc", b"defgh", b"z"):
            socket.messages.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=data))
        socket.messages.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None))
        assert await stream.readexactly(2) == b"ab"
        assert await stream.readexactly(4) == b"cdef"
        assert await stream.readexactly(2) == b"gh"
        with pytest.raises(asyncio.IncompleteReadError) as error:
            await stream.readexactly(2)
        assert error.value.partial == b"z"
    asyncio.run(scenario())


def test_websocket_writes_remain_in_order_and_resources_close():
    async def scenario():
        socket = Socket()
        session = Session(socket)
        stream = WebSocketStream(socket, session)
        stream.write(b"first")
        stream.write(b"second")
        await stream.drain()
        assert socket.frames == [b"first", b"second"]
        stream.close()
        await stream.wait_closed()
        assert socket.closed and session.closed
        with pytest.raises(ConnectionError):
            stream.write(b"after close")
    asyncio.run(scenario())


def test_wss_uses_verified_tls_binary_protocol_and_native_mtproto_framing(monkeypatch):
    async def scenario():
        socket = Socket()
        session = Session(socket)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
        connection = ConnectionWss("149.154.167.51", 443, 2,
                                   loggers=defaultdict(lambda: logging.getLogger("test-wss")))
        try:
            await connection.connect(timeout=1)
            assert session.url == "wss://venus.web.telegram.org/apiws"
            assert session.options["ssl"] is True
            assert session.options["protocols"] == ("binary",)
            assert session.options["heartbeat"] is None
            assert len(socket.frames[0]) == 64
            await connection.send(b"\x00" * 16)
            await asyncio.wait_for(socket.sent.wait(), 1)
            assert len(socket.frames[1]) == 17
        finally:
            await connection.disconnect()
        assert socket.closed and session.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("dc,name", [(1,"pluto"),(2,"venus"),(3,"aurora"),(4,"vesta"),(5,"flora")])
def test_wss_uses_only_documented_telegram_dc_hosts(dc,name):
    assert websocket_url(dc) == f"wss://{name}.web.telegram.org/apiws"


def test_unsupported_transport_or_unknown_dc_is_rejected():
    with pytest.raises(ValueError):
        websocket_url(6)
    with pytest.raises(ValueError):
        connection_type("unrecognized")
    assert connection_type("wss") is ConnectionWss

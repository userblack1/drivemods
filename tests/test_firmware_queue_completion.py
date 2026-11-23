import asyncio
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.types import Update
import pytest

from firmware_bot.bridge import TelegramBridge
from firmware_bot.config import Settings
from firmware_bot.response import CompletedResponseRejected, FirmwareNotFound, SourceUnavailable
from firmware_bot.service import Service
from firmware_bot.storage import Database
from firmware_bot.telegram import Messenger, make_router
from firmware_bot.versions import SoftwareVersion
from test_firmware import FakeClient, RecordingSession, raw_message, terminal_message
from test_firmware_concurrency import Bridge, until, close_service, enqueue


class ClosingSupplier(FakeClient):
    """Real supplier cadence: package, silence, extra attachment, final prompt."""
    def __init__(self):
        super().__init__([])
        self.busy = False
        self.discarded = []
        self.completed = []

    async def send_message(self, source, text, **kwargs):
        base = 1000 * (len(self.sent) + 1)
        if self.busy:
            self.discarded.append(text)
        self.sent.append(text)
        self.busy = True
        async def emit():
            async def deliver(message):
                await self.handlers[0](SimpleNamespace(message=message))
            await deliver(raw_message(base + 1, "Scanning...🔎", reply=base))
            await asyncio.sleep(0.005)
            await deliver(raw_message(base + 2, text + "_M.V034.003.250117\nИНСТРУКЦИЯ ПО УСТАНОВКЕ\nПРОШИВКА https://example.com/file", reply=base))
            # Longer than the bridge's ordinary silence threshold.
            await asyncio.sleep(0.045)
            photo = raw_message(base + 3, "Дополнительная инструкция к панели приборов", reply=base)
            photo.photo = SimpleNamespace(id=1)
            photo.file = SimpleNamespace(size=100)
            await deliver(photo)
            await asyncio.sleep(0.045)
            self.busy = False
            self.completed.append(text)
            await deliver(terminal_message(base + 4, reply=base))
        self.tasks.append(asyncio.create_task(emit()))
        return SimpleNamespace(id=base)


def test_next_search_waits_for_supplier_completion_and_includes_late_attachment(tmp_path):
    async def scenario():
        client = ClosingSupplier()
        settings = Settings(data_dir=tmp_path, response_timeout=0.5, settle_seconds=0.01, min_collect_seconds=0.02)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        first = asyncio.create_task(bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W")))
        await asyncio.sleep(0.035)
        assert not first.done()
        assert bridge.is_searching() and bridge.is_blocked()
        result = await first
        assert [message.id for message in result] == [1002, 1003]
        assert result[-1].media_kind == "photo"
        assert client.completed == ["LX2.KOR.S5W"]
        second = await bridge.lookup(SoftwareVersion.parse("LX3.KOR.S5W"))
        assert [message.id for message in second] == [2002, 2003]
        assert client.discarded == []
        assert not bridge.is_searching() and not bridge.is_blocked()
    asyncio.run(scenario())


def test_complete_package_without_terminal_is_not_released_as_success(tmp_path):
    async def scenario():
        client = FakeClient([(0, raw_message(101, "LX2.KOR.S5W_M.V034.003\nИНСТРУКЦИЯ ПО УСТАНОВКЕ\nПРОШИВКА https://example.com/file"))])
        bridge = TelegramBridge(Settings(data_dir=tmp_path, response_timeout=0.08, settle_seconds=0.005, min_collect_seconds=0.01), client=client)
        bridge.source = SimpleNamespace(id=999)
        with pytest.raises(SourceUnavailable, match="закончил ответ"):
            await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert bridge.is_blocked() and not bridge.is_searching()
        assert bridge._read_state()["recoverable"]
    asyncio.run(scenario())


def test_negative_reply_waits_for_terminal_before_next_customer(tmp_path):
    async def scenario():
        client = FakeClient([(0, raw_message(101, "Данная прошивка не найдена")), (0.05, terminal_message(102))])
        bridge = TelegramBridge(Settings(data_dir=tmp_path, response_timeout=0.2, settle_seconds=0.005, min_collect_seconds=0.01), client=client)
        bridge.source = SimpleNamespace(id=999)
        task = asyncio.create_task(bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W")))
        await asyncio.sleep(0.025)
        assert not task.done() and bridge.is_searching()
        with pytest.raises(FirmwareNotFound):
            await task
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def test_terminal_without_package_is_rejected_promptly_instead_of_waiting_full_timeout(tmp_path):
    async def scenario():
        client = FakeClient([(0, raw_message(101, "Scanning...🔎")), (0.005, terminal_message(102))])
        bridge = TelegramBridge(Settings(data_dir=tmp_path, response_timeout=1, settle_seconds=0.005, min_collect_seconds=0.5), client=client)
        bridge.source = SimpleNamespace(id=999)
        with pytest.raises(CompletedResponseRejected):
            await asyncio.wait_for(bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W")), 0.15)
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def update(user, text, message_id=1):
    return Update.model_validate({"update_id": user * 10 + message_id, "message": {
        "message_id": message_id, "date": 1794050000, "chat": {"id": user, "type": "private"},
        "from": {"id": user, "is_bot": False, "first_name": "Synthetic"}, "text": text}})


def test_active_search_is_reported_as_queue_not_outage_and_repeat_keeps_same_request(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path, owner_only=False, admin_ids=frozenset({11}))
        db = Database(tmp_path / "queue.db")
        first = enqueue(db, 1, "LX2.KOR.S5W")
        db.update(first, "searching")
        bridge = SimpleNamespace(is_blocked=lambda: True, is_searching=lambda: True)
        session = RecordingSession()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, bridge))
        await dispatcher.feed_update(bot, update(2, "LX3.KOR.S5W"))
        accepted = session.methods[-1].text
        assert "в очереди" in accepted and "место: 2" in accepted
        assert "недоступен" not in accepted
        request_id = db.latest(2)["id"]
        await dispatcher.feed_update(bot, update(2, "LX3.KOR.S5W", 2))
        assert db.latest(2)["id"] == request_id
        assert "уже принят" in session.methods[-1].text
        await dispatcher.feed_update(bot, update(11, "/status"))
        assert "выполняет поиск" in session.methods[-1].text
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


def test_crashed_pending_state_is_still_blocked_not_misreported_as_running(tmp_path):
    bridge = TelegramBridge(Settings(data_dir=tmp_path), client=FakeClient([]))
    bridge._write_state("pending", "LX2.KOR.S5W", sent_id=100, recoverable=True)
    assert bridge.is_blocked() and not bridge.is_searching()


def test_ten_distinct_requests_use_fifo_and_each_customer_gets_only_their_package(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path, owner_only=False, user_cooldown=0)
        db = Database(tmp_path / "queue.db")
        bridge = Bridge()
        session = RecordingSession()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, bridge))
        queries = [f"MDL{i}.KOR.S5W" for i in range(10)]
        for user, query in enumerate(queries, 1):
            await dispatcher.feed_update(bot, update(user, query))
        assert [db.queue_position(db.latest(user)["id"]) for user in range(1, 11)] == list(range(1, 11))
        service = Service(settings, db, bridge, Messenger(bot, settings))
        await until(service, lambda: db.counts().get("delivered") == 10)
        assert [query for query, variant in bridge.queries] == queries
        assert bridge.peak == 1
        for user, query in enumerate(queries, 1):
            writes = [m for m in session.methods if getattr(m, "chat_id", None) == user]
            starts = [m.text for m in writes if "Сейчас ваша очередь" in (getattr(m, "text", "") or "")]
            assert len(starts) == 1 and query in starts[0]
            urls = [e.url for m in writes for e in getattr(m, "entities", None) or [] if e.type == "text_link"]
            assert urls == ["https://example.com/" + query]
            assert len([m for m in writes if "передан полностью" in (getattr(m, "text", "") or "")]) == 1
        await close_service(service)
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


def test_later_cached_request_does_not_overtake_a_waiting_uncached_request(tmp_path):
    async def scenario():
        db = Database(tmp_path / "queue.db")
        bridge = Bridge()
        delivered = []
        class Recorder:
            async def deliver(self, request, *args):
                delivered.append(request["user_id"])
        service = Service(Settings(data_dir=tmp_path), db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        await until(service, lambda: db.latest(1)["state"] == "delivered")
        bridge.blocked = True
        enqueue(db, 2, "LX3.KOR.S5W")
        enqueue(db, 3, "LX2.KOR.S5W")
        await service.dispatch()
        await asyncio.sleep(0)
        assert db.latest(2)["state"] == db.latest(3)["state"] == "queued"
        assert delivered == [1]
        bridge.blocked = False
        await until(service, lambda: db.latest(2)["state"] == db.latest(3)["state"] == "delivered")
        await close_service(service)
        db.close()
    asyncio.run(scenario())

import asyncio
from collections import Counter
from contextlib import suppress
from tempfile import TemporaryDirectory
import time

from aiogram import Bot, Dispatcher

from firmware_bot.cache import Bundle, BundleCache, cache_key
from firmware_bot.config import Settings
from firmware_bot.response import SourceMessage, validate_response
from firmware_bot.service import Service
from firmware_bot.storage import Database
from firmware_bot.telegram import Messenger, make_router
from firmware_bot.versions import SoftwareVersion
from test_firmware import RecordingSession, gen5_messages, raw_message, source_card, CallbackClient, TelegramBridge


async def until(service, predicate, timeout=4):
    async def pump():
        while not predicate():
            await service.dispatch()
            await asyncio.sleep(0.002)
    await asyncio.wait_for(pump(), timeout)


async def close_service(service):
    tasks = list(service.tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    service.cache.clear()


class Bridge:
    def __init__(self):
        self.queries, self.downloads = [], []
        self.active = self.peak = 0
        self.blocked = False
        self.gate = None

    def is_blocked(self):
        return self.blocked

    async def try_recover(self):
        if self.gate:
            await self.gate.wait()
        return not self.blocked

    async def lookup(self, version):
        self.queries.append(cache_key(version))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            suffix = ".V141.240205." + version.variant if version.variant else "_M.V034.003.250117"
            from aiogram.types import MessageEntity
            result = [SourceMessage(1, version.family + suffix + "\nИНСТРУКЦИЯ ПО УСТАНОВКЕ ПРОШИВКА\nСкачать", entities=[
                MessageEntity(type="text_link", offset=0, length=3, url="https://example.com/" + version.query)]),
                SourceMessage(2, version.query, media_kind="photo", size=4)]
            validate_response(version, result, Settings())
            return result
        finally:
            self.active -= 1

    async def download(self, message, folder):
        await asyncio.sleep(0.01)
        self.downloads.append(message.id)
        path = folder / "synthetic.bin"
        path.write_bytes(message.text.encode())
        return path


def enqueue(db, user, query, message=1):
    return db.enqueue(user, user, message, SoftwareVersion.parse(query), 100, 0)[0]


def test_100_simultaneous_users_receive_own_package_with_one_lookup_per_exact_query(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path, owner_only=False)
        db = Database(tmp_path / "queue.db")
        bridge = Bridge()
        class Session(RecordingSession):
            def __init__(self):
                super().__init__()
                self.photos = []
            async def make_request(self, bot, method, timeout=None):
                if method.__api_method__ == "sendPhoto":
                    self.photos.append((method.chat_id, method.photo.path.read_text()))
                return await super().make_request(bot, method, timeout)
        session = Session()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, bridge))
        queries = ["LX2.KOR.S5W", "LX3.KOR.S5W", "LX2.KOR.S5W_M.V034.003.250117",
                   "IG19.KOR.SOP.V141.240205.STD_H", "IG19.KOR.SOP.V141.240205.STD_M"]
        from aiogram.types import Update
        updates = [Update.model_validate({"update_id": user, "message": {
            "message_id": 1, "date": 1794050000, "chat": {"id": user, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Synthetic"},
            "text": queries[(user - 1) % len(queries)]}}) for user in range(1, 101)]
        await asyncio.gather(*(dispatcher.feed_update(bot, update) for update in updates))
        assert len(db.queued()) == 100
        service = Service(settings, db, bridge, Messenger(bot, settings))
        await until(service, lambda: db.counts().get("delivered") == 100)
        assert bridge.peak == 1
        assert Counter(bridge.queries) == Counter(cache_key(SoftwareVersion.parse(q)) for q in queries)
        assert len(bridge.downloads) == len(queries)
        assert sorted(session.photos) == [(user, queries[(user - 1) % len(queries)]) for user in range(1, 101)]
        for user in range(1, 101):
            messages = [m for m in session.methods if getattr(m, "chat_id", None) == user]
            completed = [m for m in messages if "передан полностью" in (getattr(m, "text", "") or "")]
            links = [e.url for m in messages for e in (getattr(m, "entities", None) or []) if e.type == "text_link"]
            assert len(completed) == 1
            assert links == ["https://example.com/" + queries[(user - 1) % len(queries)]]
            assert len([m for m in messages if m.__api_method__ == "sendPhoto"]) == 1
        await close_service(service)
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


def test_stale_queue_is_finished_with_notification_not_left_pending_forever(tmp_path):
    db = Database(tmp_path / "queue.db")
    request_id = enqueue(db, 1, "LX2.KOR.S5W")
    with db.connection:
        db.connection.execute("UPDATE requests SET created=? WHERE id=?", (time.time() - 120, request_id))
    db.expire_queued(60)
    assert db.latest(1)["state"] == "failed"
    assert len(db.unnotified()) == 1
    assert db.next_request() is None
    db.close()


def test_slow_or_blocked_customer_does_not_hold_up_other_models_and_cache_hits(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path)
        db = Database(tmp_path / "queue.db")
        bridge = Bridge()
        slow = asyncio.Event()
        delivered = []
        class Recorder:
            async def deliver(self, request, messages, files):
                if request["user_id"] == 1:
                    await slow.wait()
                if request["user_id"] == 3:
                    raise RuntimeError("synthetic blocked customer")
                assert all(p.exists() for p in files.values())
                delivered.append(request["user_id"])
            async def notify_admins(self, *args):
                pass
        service = Service(settings, db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        await until(service, lambda: db.latest(1)["state"] == "delivering")
        for user, query in [(2, "LX3.KOR.S5W"), (3, "LX2.KOR.S5W"), (4, "LX2.KOR.S5W")]:
            enqueue(db, user, query)
        await until(service, lambda: {2, 4} <= set(delivered) and db.latest(3)["state"] == "failed")
        assert 1 not in delivered and len(bridge.queries) == 2
        bridge.blocked = True
        bridge.gate = asyncio.Event()
        enqueue(db, 5, "LX2.KOR.S5W")
        await until(service, lambda: 5 in delivered)
        assert len(bridge.queries) == 2
        slow.set()
        await until(service, lambda: db.latest(1)["state"] == "delivered")
        await close_service(service)
        db.close()
    asyncio.run(scenario())


def test_new_subscriber_joins_running_lookup_without_replay(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path)
        db = Database(tmp_path / "queue.db")
        bridge = Bridge()
        received = []
        class Recorder:
            async def deliver(self, request, *args):
                received.append(request["user_id"])
        service = Service(settings, db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        await service.dispatch()
        await asyncio.sleep(0)
        enqueue(db, 2, "LX2.KOR.S5W")
        await until(service, lambda: len(received) == 2)
        assert len(bridge.queries) == len(bridge.downloads) == 1
        await close_service(service)
        db.close()
    asyncio.run(scenario())


def test_cache_expiration_capacity_and_active_delivery_keep_their_own_files(tmp_path):
    now = [0.0]
    cache = BundleCache(10, max_entries=1, clock=lambda: now[0])
    def bundle():
        temporary = TemporaryDirectory(dir=tmp_path)
        from pathlib import Path
        path = Path(temporary.name) / "synthetic.bin"
        path.write_bytes(b"test")
        return Bundle([], {1: path}, temporary)
    active = bundle()
    cache.put(("A", ""), active)
    cache.put(("B", ""), bundle())
    assert cache.get(("A", "")) is None
    assert active.files[1].is_file()
    now[0] = 11
    assert cache.get(("B", "")) is None
    cache.clear()
    active.temporary.cleanup()


def test_failed_validation_is_never_cached_and_other_queued_request_continues(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path)
        db = Database(tmp_path / "queue.db")
        class InvalidBridge(Bridge):
            async def lookup(self, version):
                if version.family == "LX2.KOR.S5W":
                    self.queries.append(cache_key(version))
                    return [SourceMessage(1, "LX3.KOR.S5W_M.V034.003\nПрошивка https://example.com/")]
                return await super().lookup(version)
        bridge = InvalidBridge()
        received = []
        class Recorder:
            async def deliver(self, request, *args):
                received.append(request["user_id"])
            async def notify_admins(self, *args):
                pass
        service = Service(settings, db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        enqueue(db, 2, "LX3.KOR.S5W")
        await until(service, lambda: db.latest(1)["state"] == "failed" and db.latest(2)["state"] == "delivered")
        enqueue(db, 3, "LX2.KOR.S5W")
        await until(service, lambda: db.latest(3)["state"] == "failed")
        assert received == [2]
        assert bridge.queries.count(("LX2.KOR.S5W", "")) == 2
        await close_service(service)
        db.close()
    asyncio.run(scenario())


def test_attachment_failure_rejects_all_subscribers_before_any_partial_delivery(tmp_path):
    async def scenario():
        db = Database(tmp_path / "queue.db")
        class BrokenDownload(Bridge):
            async def download(self, *args):
                raise OSError("synthetic download failure")
        class Recorder:
            async def deliver(self, *args):
                raise AssertionError("must not send partial package")
            async def notify_admins(self, *args):
                pass
        bridge = BrokenDownload()
        service = Service(Settings(data_dir=tmp_path), db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        enqueue(db, 2, "LX2.KOR.S5W")
        await until(service, lambda: db.counts().get("failed") == 2)
        assert len(bridge.queries) == 1 and not service.cache.entries
        assert not list(tmp_path.glob("delivery-*"))
        await close_service(service)
        db.close()
    asyncio.run(scenario())


def test_worker_preserves_queued_requests_while_source_offline_then_resumes(tmp_path):
    async def scenario():
        db = Database(tmp_path / "queue.db")
        settings = Settings(data_dir=tmp_path)
        bridge = Bridge()
        bridge.blocked = True
        received = []
        class Recorder:
            async def deliver(self, request, *args):
                received.append(request["id"])
            async def notify_user(self, *args):
                pass
        service = Service(settings, db, bridge, Recorder())
        enqueue(db, 1, "LX2.KOR.S5W")
        worker = asyncio.create_task(service.worker())
        await asyncio.sleep(0.03)
        assert db.latest(1)["state"] == "queued" and bridge.queries == []
        bridge.blocked = False
        db.changed.set()
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.005)
        assert db.latest(1)["state"] == "delivered"
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await asyncio.sleep(0)
        assert not service.tasks
        db.close()
    asyncio.run(scenario())


def test_terminal_and_firmware_are_collected_without_waiting_for_callback_ack(tmp_path):
    async def scenario():
        from types import SimpleNamespace
        class SlowAck(CallbackClient):
            async def __call__(self, request):
                self.callbacks.append(request)
                for part in gen5_messages():
                    await self.handlers[0](SimpleNamespace(message=raw_message(part.id + 100, part.text, reply=request.msg_id)))
                await self.handlers[0](SimpleNamespace(message=raw_message(120, "ℹ /help\nДля нового поискового запроса\nВведите SW version", reply=request.msg_id)))
                await asyncio.sleep(1)
        card = source_card()
        card.message = "IG19.KOR.SOP.V---.------.STD_H"
        client = SlowAck([(0, card)])
        settings = Settings(data_dir=tmp_path, response_timeout=2, settle_seconds=0.01, min_collect_seconds=0.5)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        started = time.monotonic()
        result = await bridge.lookup(SoftwareVersion.parse("IG19.KOR.SOP.V141.240205.STD_H"))
        assert time.monotonic() - started < 0.3
        assert len(client.callbacks) == 1 and len(result) == 3
        assert not bridge.is_blocked()
    asyncio.run(scenario())

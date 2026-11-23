import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .cache import Bundle, BundleCache, cache_key, request_version
from .response import FirmwareNotFound, SourceUnavailable, validate_response


log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings, db, bridge, messenger):
        self.settings, self.db, self.bridge, self.messenger = settings, db, bridge, messenger
        self.cache = BundleCache(settings.cache_ttl)
        self.groups = {}
        self.source_busy = False
        self.recovery_task = None
        self.next_recovery = 0
        self.tasks = set()
        self.delivery_slots = asyncio.Semaphore(settings.delivery_concurrency)
        self.status_slots = asyncio.Semaphore(settings.delivery_concurrency)

    async def _notify_started(self, request):
        notify = getattr(self.messenger, "notify_user", None)
        if notify is None:
            return
        try:
            async with self.status_slots:
                selected = " " + request["variant"] if request.get("variant") and not request["query"].endswith(request["variant"]) else ""
                await notify(request["chat_id"], f"Сейчас ваша очередь. Запрос #{request['id']}: {request['query']}{selected}\n"
                             "Проверяю прошивку и инструкцию для этой версии.")
        except Exception as exc:
            log.warning("Start notification %s failed: %s", request["id"], type(exc).__name__)

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(task):
            self.tasks.discard(task)
            self.db.changed.set()
            if not task.cancelled() and task.exception():
                log.error("Firmware background task failed: %s", type(task.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def _prepare(self, version, *, release_source=False):
        try:
            messages = await self.bridge.lookup(version)
            validate_response(version, messages, self.settings)
        finally:
            if release_source:
                self.source_busy = False
                self.db.changed.set()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = TemporaryDirectory(prefix="delivery-", dir=self.settings.data_dir)
        try:
            files = {}
            for message in messages:
                if message.media_kind:
                    folder = Path(temporary.name) / str(message.id)
                    folder.mkdir()
                    files[message.id] = await self.bridge.download(message, folder)
            return Bundle(messages, files, temporary)
        except BaseException:
            temporary.cleanup()
            raise

    async def _failed(self, requests, exc, *, delivering=False):
        reason = str(exc) if isinstance(exc, SourceUnavailable) else type(exc).__name__
        for request in requests:
            if isinstance(exc, FirmwareNotFound):
                self.db.update(request["id"], "not_found", str(exc))
            else:
                detail = ("Передача ответа прервалась. Часть сообщений могла прийти. Запросите полный комплект заново перед установкой."
                          if delivering else "Не удалось получить полный проверенный ответ. Повторите запрос позже или обратитесь в поддержку.")
                self.db.update(request["id"], "failed", detail)
                log.warning("Firmware request %s failed: %s", request["id"], reason)
        if not isinstance(exc, FirmwareNotFound):
            try:
                ids = ", ".join(str(r["id"]) for r in requests)
                await self.messenger.notify_admins(f"Запросы #{ids} не выполнены: {reason}\nПроверьте /status и диалог источника.")
            except Exception as error:
                log.warning("Admin notification failed: %s", type(error).__name__)

    async def _deliver(self, request, bundle):
        async with self.delivery_slots:
            try:
                await self.messenger.deliver(request, bundle.messages, bundle.files)
                self.db.update(request["id"], "delivered")
                log.info("Firmware request %s delivered in %.2fs", request["id"], time.time() - request["created"])
            except Exception as exc:
                await self._failed([request], exc, delivering=True)

    async def _lookup_group(self, key, version):
        started = time.monotonic()
        try:
            bundle = await self._prepare(version, release_source=True)
            self.cache.put(key, bundle)
        except Exception as exc:
            requests = self.groups.pop(key)
            self.db.changed.set()
            await self._failed(requests, exc)
            return
        requests = self.groups.pop(key)
        log.info("Firmware source package ready in %.2fs for %s requests", time.monotonic() - started, len(requests))
        for request in requests:
            self.db.update(request["id"], "delivering")
            self._spawn(self._deliver(request, bundle))
        self.db.changed.set()

    async def step(self):
        """Process one request to completion (used by offline validation tools)."""
        await self.notify_results()
        request = self.db.next_request()
        if not request:
            return False
        self.db.update(request["id"], "searching")
        try:
            bundle = await self._prepare(request_version(request))
            try:
                self.db.update(request["id"], "delivering")
                await self._deliver(request, bundle)
            finally:
                bundle.temporary.cleanup()
        except Exception as exc:
            await self._failed([request], exc)
        await self.notify_results()
        return True

    async def notify_results(self):
        for request in self.db.unnotified():
            try:
                if request["state"] != "delivered":
                    await self.messenger.notify_user(request["chat_id"], request["detail"])
                self.db.notified(request["id"])
            except Exception as exc:
                log.warning("Firmware notification %s failed: %s", request["id"], type(exc).__name__)

    async def _notifications(self):
        while True:
            await self.notify_results()
            await asyncio.sleep(1)

    async def dispatch(self):
        """Cache hits and subscribers never wait for an unrelated source lookup."""
        self.cache.prune()
        self.db.expire_queued(self.settings.queue_timeout)
        can_lookup = not self.source_busy
        if can_lookup and self.bridge.is_blocked():
            can_lookup = False
            if (self.recovery_task is None or self.recovery_task.done()) and time.monotonic() >= self.next_recovery:
                self.next_recovery = time.monotonic() + 15
                self.recovery_task = self._spawn(self.bridge.try_recover())
        room = max(0, self.settings.max_queue - len(self.tasks))
        for request in self.db.queued():
            version = request_version(request)
            key = cache_key(version)
            bundle = self.cache.get(key)
            if bundle is not None and room and not self.source_busy:
                self.db.update(request["id"], "delivering")
                self._spawn(self._deliver_cached(request, bundle))
                room -= 1
                log.info("Firmware request %s served from verified cache", request["id"])
            elif key in self.groups:
                self.db.update(request["id"], "searching")
                self.groups[key].append(request)
                self._spawn(self._notify_started(request))
            elif can_lookup and room:
                self.db.update(request["id"], "searching")
                self.groups[key] = [request]
                self.source_busy = True
                can_lookup = False
                self._spawn(self._notify_started(request))
                self._spawn(self._lookup_group(key, version))
                room -= 1
            else:
                # A later cached request must not jump over the head of the queue.
                break

    async def _deliver_cached(self, request, bundle):
        await self._notify_started(request)
        await self._deliver(request, bundle)

    async def worker(self):
        self.db.recover()
        notifications = asyncio.create_task(self._notifications())
        try:
            while True:
                self.db.changed.clear()
                try:
                    await self.dispatch()
                except Exception as exc:
                    log.error("Firmware scheduler failed: %s", type(exc).__name__)
                try:
                    await asyncio.wait_for(self.db.changed.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        finally:
            notifications.cancel()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(notifications, *self.tasks, return_exceptions=True)
            self.cache.clear()

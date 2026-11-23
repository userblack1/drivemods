"""Serialize supplier conversations and refuse ambiguous or incomplete replies."""
import asyncio
from contextlib import suppress
import json
import logging
from pathlib import Path
import time

from telethon import TelegramClient, events, functions
from telethon.errors import BotResponseTimeoutError, FloodWaitError

from .config import Settings
from .response import CompletedResponseRejected, FirmwareNotFound, IncompleteResponse, ResponseRejected, ResponseTimeout, SourceMessage, SourceUnavailable, VersionMismatch, button_payload, validate_response
from .versions import SoftwareVersion, response_families, response_variants
from .transport import connection_type


def links_callback(message, version: SoftwareVersion | None = None):
    if version is not None and response_families(message.message or "") != {version.family}:
        return None
    if version is not None and version.variant and response_variants(message.message or "") != {version.variant}:
        return None
    for row in getattr(getattr(message, "reply_markup", None), "rows", []) or []:
        for button in row.buttons:
            if button.text.strip().casefold() not in {"ссылки", "🔗 ссылки"}:
                continue
            payload = button_payload(button)
            if type(payload).__name__ not in {"InlineButtonTypeCallback", "KeyboardButtonCallback"}:
                continue
            if getattr(payload, "requires_password", False):
                raise ResponseRejected("Кнопка источника требует пароль. Автоматическая выдача остановлена.")
            return payload.data
    return None


def source_ready(message) -> bool:
    text = (getattr(message, "message", None) or getattr(message, "text", "") or "").casefold()
    return "/help" in text and "для нового поискового запроса" in text and "sw version" in text


def relay_message(message: SourceMessage, selected_card: int | None) -> bool:
    text = message.text.strip()
    if source_ready(message) and not message.media_kind and not message.urls():
        return False
    if text in {".", "Scanning...🔎"} or ("результаты поиска" in text.casefold() and len(text) < 100):
        return False
    if message.id == selected_card and ".V---" in text.upper() and "\n" not in text:
        return False
    return True


def source_family_hints(version: SoftwareVersion, cards) -> list[str]:
    """Display supplier labels for the same model/region, never select or rewrite them.

    ccNC search cards mask revisions without a V, so the delivery validator does
    not classify those masks. A display-only hint may still show their exact
    platform label. It cannot authorize clicking Links or delivering materials.
    """
    hints = set()
    prefix = version.family.split('.')[:2]
    for card in cards:
        lines = (getattr(card, 'message', None) or '').splitlines()
        if not lines:
            continue
        label = '.'.join(lines[0].strip().split('.')[:3])
        try:
            candidate = SoftwareVersion.parse(label)
        except ValueError:
            continue
        if candidate.family.split('.')[:2] == prefix and candidate.family != version.family:
            hints.add(candidate.family)
    return sorted(hints)[:8]


class TelegramBridge:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client if client is not None else TelegramClient(
            str(settings.session_path), settings.api_id, settings.api_hash,
            flood_sleep_threshold=0, request_retries=0,
            device_model="NextMode SW", app_version="0.1.0",
            connection=connection_type(settings.telegram_transport))
        self.source = None
        self.lock = asyncio.Lock()
        self.state_path = settings.data_dir / "bridge-state.json"
        self.keepalive_task = None
        self.last_recovery_check = -float("inf")

    def _write_state(self, state: str, query: str = "", reason: str = "", *, sent_id=None, recoverable=False):
        target = self.state_path.with_suffix(".tmp")
        value = {"state": state, "query": query, "reason": reason}
        if state != "ready":
            value.update(sent_id=sent_id, recoverable=recoverable, updated=time.time())
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        target.replace(self.state_path)

    def _read_state(self):
        if not self.state_path.exists():
            return {"state": "ready"}
        try:
            result = json.loads(self.state_path.read_text(encoding="utf-8"))
            return result if isinstance(result, dict) and "state" in result else {"state": "blocked"}
        except (OSError, ValueError, KeyError):
            return {"state": "blocked"}

    def is_blocked(self) -> bool:
        return self._read_state()["state"] != "ready"

    def is_searching(self) -> bool:
        # A live pending search is ordinary work, not a supplier outage.
        # After a crash the same persisted state has no active lock and must
        # still go through recovery before another query may be sent.
        return self.lock.locked() and self._read_state()["state"] == "pending"

    def recovery_pending(self) -> bool:
        return self._read_state().get("recoverable") is True

    async def try_recover(self) -> bool:
        """Release a timed-out search only after its own final supplier prompt.

        Never replay the failed query or deliver its late replies. Manual messages,
        missing history and non-timeout safety failures require owner review.
        """
        if not self.is_blocked():
            return True
        if self.lock.locked() or time.monotonic() - self.last_recovery_check < 15:
            return False
        async with self.lock:
            state = self._read_state()
            sent_id = state.get("sent_id")
            if state.get("recoverable") is not True or type(sent_id) is not int or sent_id <= 0:
                return False
            self.last_recovery_check = time.monotonic()
            try:
                messages = await asyncio.wait_for(self.client.get_messages(self.source, limit=50), 15)
                if not messages:
                    return False
                latest = messages[0]
                origin = next((m for m in messages if m.id == sent_id), None)
                if (not origin or not origin.out or origin.message != state.get("query")
                        or latest.id <= sent_id or latest.out or latest.sender_id != self.source.id
                        or not source_ready(latest)
                        or time.time() - (getattr(latest, "edit_date", None) or latest.date).timestamp() < self.settings.settle_seconds
                        or any(m.out and m.id > sent_id for m in messages)):
                    return False
            except Exception as exc:
                logging.getLogger(__name__).warning("Supplier recovery check failed: %s", type(exc).__name__)
                return False
            self._write_state("ready")
            logging.getLogger(__name__).warning("Supplier search completed; accepting new queries without replaying the old one")
            return True

    async def connect(self):
        self.settings.ensure_account_local()
        if not self.settings.session_path.is_file():
            raise SourceUnavailable("Сначала выполните команду connect для входа в Telegram-аккаунт.")
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SourceUnavailable("Сессия Telegram-аккаунта не авторизована. Выполните connect.")
        me = await self.client.get_me()
        if not me or me.bot:
            raise SourceUnavailable("Связка должна использовать пользовательский Telegram-аккаунт.")
        self.source = await self.client.get_entity(self.settings.source_bot)
        if not getattr(self.source, "bot", False):
            raise SourceUnavailable("FIRMWARE_SOURCE_BOT должен указывать на бота выдачи прошивок.")
        if self.settings.telegram_transport == "wss":
            self.keepalive_task = asyncio.create_task(self._keepalive())

    async def _keepalive(self):
        # Use MTProto pings, as Telegram's browser clients do, instead of WS control pings.
        while True:
            await asyncio.sleep(20)
            if not self.client.is_connected():
                continue
            try:
                await asyncio.wait_for(self.client(functions.PingRequest(ping_id=time.monotonic_ns())), 10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning("Telegram keepalive interrupted: %s", type(exc).__name__)

    async def close(self):
        if self.keepalive_task is not None:
            self.keepalive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.keepalive_task
        await self.client.disconnect()

    async def lookup(self, version: SoftwareVersion) -> list[SourceMessage]:
        if self.is_blocked():
            await self.try_recover()
        async with self.lock:
            if self.is_blocked():
                raise SourceUnavailable("Связка ожидает проверки владельцем после незавершённого ответа.")
            queue = asyncio.Queue(maxsize=200)
            overflow = False

            async def receive(event):
                nonlocal overflow
                if queue.full():
                    overflow = True
                else:
                    queue.put_nowait(event.message)

            incoming = events.NewMessage(chats=self.source)
            edited = events.MessageEdited(chats=self.source)
            self.client.add_event_handler(receive, incoming)
            self.client.add_event_handler(receive, edited)
            # Persist before sending: a crash must never silently mix the next user's result.
            self._write_state("pending", version.query)
            sent = None
            callback_task = None
            try:
                sent = await self.client.send_message(self.source, version.query, parse_mode=None, link_preview=False)
                self._write_state("pending", version.query, sent_id=sent.id, recoverable=True)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.settings.response_timeout
                first = last = None
                replies = {}
                selected_card = None
                selection_delay = min(2, self.settings.settle_seconds)
                mismatch = None
                incomplete_reason = None
                while True:
                    now = loop.time()
                    incomplete = False
                    if overflow:
                        raise ResponseRejected("Слишком много событий от источника.")
                    if callback_task is not None and callback_task.done():
                        # Callback RPC acknowledgements can take 15 seconds even
                        # after the bot has already sent all firmware messages.
                        callback_task.result()
                    candidates = [(m, links_callback(m, version)) for m in replies.values()]
                    candidates = [(m, data) for m, data in candidates if data is not None]
                    cards = [m for m in replies.values() if links_callback(m) is not None]
                    selection_error = None
                    if len(candidates) > 1:
                        selection_error = VersionMismatch("Источник предложил несколько карточек прошивки. Нужна точная версия.")
                    elif cards and not candidates:
                        selection_error = VersionMismatch("Карточка источника не совпадает с указанной версией.")
                    if candidates and not selection_error and selected_card is None and now - last >= selection_delay:
                        card, data = candidates[0]
                        selected_card = card.id
                        async def open_links():
                            try:
                                await asyncio.wait_for(self.client(functions.messages.GetBotCallbackAnswerRequest(
                                    peer=self.source, msg_id=card.id, data=data)), min(15, max(0.1, deadline - loop.time())))
                            except (BotResponseTimeoutError, asyncio.TimeoutError):
                                # Collect its result without clicking twice.
                                pass
                            finally:
                                if not queue.full():
                                    queue.put_nowait(None)
                        callback_task = asyncio.create_task(open_links())
                        continue
                    terminal = bool(replies) and source_ready(replies[max(replies)])
                    fast_settle = min(2, self.settings.settle_seconds)
                    settled = bool(replies) and now - first >= self.settings.min_collect_seconds and now - last >= self.settings.settle_seconds
                    finished = terminal and now - last >= fast_settle
                    if replies and (settled or finished):
                        # The selected hardware variant owns the response. Other search
                        # cards are alternatives, not parts of that firmware package.
                        other_cards = {m.id for m in cards if m.id != selected_card} if selected_card is not None else set()
                        result = [SourceMessage.from_telegram(replies[key]) for key in sorted(replies) if key not in other_cards]
                        try:
                            validate_response(version, result, self.settings)
                        except FirmwareNotFound as exc:
                            if finished:
                                raise
                            incomplete_reason = str(exc)
                            incomplete = True
                        except IncompleteResponse as exc:
                            incomplete_reason = str(exc)
                            incomplete = True
                        except VersionMismatch as exc:
                            mismatch = exc
                            incomplete = True
                        else:
                            if not selection_error and finished:
                                self._write_state("ready")
                                return [m for m in result if relay_message(m, selected_card)]
                            # Silence is not completion: the supplier can still
                            # be closing its previous search and discard a new one.
                            incomplete_reason = "Источник ещё не подтвердил завершение выдачи."
                            incomplete = True
                        if selection_error or mismatch:
                            incomplete = True
                            # An expired search menu is a completed, unsuccessful search.
                            # Drain it until the supplier explicitly accepts a new query;
                            # silence alone must never release a potentially active answer.
                            if finished:
                                variants = sorted(set().union(*(response_variants(m.message or "") for m in cards)))
                                hint = ("Источник нашёл варианты " + " и ".join(variants) + ". ") if len(variants) > 1 else ""
                                families = source_family_hints(version, cards)
                                if families:
                                    hint += (f"Указано: {version.query}. Обозначения в карточках источника: "
                                             + ", ".join(families) + ". Они не выбираются автоматически. ")
                                raise FirmwareNotFound(hint + "Не удалось однозначно выбрать прошивку по этой строке. "
                                                       "Отправьте полную строку Software Version с ШГУ, сохранив все символы.")
                        elif incomplete and finished:
                            raise CompletedResponseRejected(incomplete_reason or "Не получен полный комплект источника.")
                    remaining = deadline - now
                    if remaining <= 0:
                        if selection_error or mismatch:
                            raise selection_error or mismatch
                        detail = "Источник не закончил ответ за отведённое время."
                        if incomplete_reason:
                            detail += " Проверка ответа: " + incomplete_reason
                        raise ResponseTimeout(detail)
                    wait = remaining
                    if replies:
                        if terminal:
                            wait = min(wait, max(fast_settle - (now - last), 0.01))
                        elif not settled:
                            wait = min(wait, max(self.settings.min_collect_seconds - (now - first),
                                                 self.settings.settle_seconds - (now - last), 0.01))
                    if candidates and not selection_error and selected_card is None:
                        wait = min(wait, max(selection_delay - (now - last), 0.01))
                    try:
                        message = await asyncio.wait_for(queue.get(), wait)
                    except asyncio.TimeoutError:
                        continue
                    if message is None:
                        continue
                    if message.id <= sent.id:
                        continue
                    if message.out:
                        raise ResponseRejected("В диалог источника отправлен другой запрос во время поиска.")
                    if message.sender_id != self.source.id:
                        continue
                    reply_id = getattr(message, "reply_to_msg_id", None)
                    if reply_id and reply_id not in {sent.id, selected_card}:
                        raise ResponseRejected("Ответ относится к другому запросу.")
                    if len(replies) >= 30 and message.id not in replies:
                        raise ResponseRejected("Ответ содержит больше 30 сообщений.")
                    replies[message.id] = message
                    now = loop.time()
                    first = first if first is not None else now
                    last = now
            except (FirmwareNotFound, CompletedResponseRejected):
                self._write_state("ready")
                raise
            except FloodWaitError as exc:
                # No automatic replay after a send with an uncertain outcome.
                self._write_state("blocked", version.query, f"FloodWait: {exc.seconds}s")
                raise SourceUnavailable("Telegram временно ограничил запросы к источнику.") from None
            except BaseException as exc:
                reason = type(exc).__name__
                if isinstance(exc, SourceUnavailable):
                    reason += ": " + str(exc)
                self._write_state("blocked", version.query, reason, sent_id=getattr(sent, "id", None),
                                  recoverable=isinstance(exc, (ResponseTimeout, IncompleteResponse, VersionMismatch, asyncio.CancelledError)))
                if isinstance(exc, (SourceUnavailable, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise SourceUnavailable("Не удалось получить проверенный ответ источника.") from None
            finally:
                if callback_task is not None:
                    callback_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await callback_task
                self.client.remove_event_handler(receive, incoming)
                self.client.remove_event_handler(receive, edited)

    async def download(self, message: SourceMessage, directory: Path) -> Path:
        # No source download happens until the entire response passed validation.
        path = await self.client.download_media(message.raw, file=str(directory) + "/")
        if not path:
            raise SourceUnavailable("Не удалось получить вложение источника.")
        output = Path(path)
        if not output.is_file() or output.stat().st_size > 49 * 1024 * 1024:
            raise SourceUnavailable("Вложение превышает допустимый размер.")
        return output

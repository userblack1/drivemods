import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, MessageEntity, Update
import pytest
pytest.importorskip("telethon", reason="Install the optional firmware dependencies to test NextMode SW")
from telethon.tl.types import (MessageEntityTextUrl, MessageEntityBlockquote, KeyboardInlineButton,
                               InlineButtonTypeCallback, InlineButtonTypeUrl)

from firmware_bot.bridge import TelegramBridge
from firmware_bot.config import Settings
from firmware_bot.response import (CompletedResponseRejected, FirmwareNotFound, IncompleteResponse, ResponseRejected,
                                   SourceAccessDenied, SourceMessage, SourceUnavailable, convert_entities, validate_response)
from firmware_bot.service import Service
from firmware_bot.storage import Database, QueueError
from firmware_bot.telegram import Messenger, make_router
from firmware_bot.versions import InvalidVersion, SoftwareVersion
from scripts.build_firmware_bundle import build


def complete_messages(family="LX2.KOR.S5W"):
    return [
        SourceMessage(11, "Mobis GEN5W: Пакет FULL / ECO"),
        SourceMessage(12, family + "_M.V034.003.250117\nИНСТРУКЦИЯ ПО ОБНОВЛЕНИЮ\nhttps://drive.google.com/file/d/update"),
        SourceMessage(13, "ИНСТРУКЦИЯ ПО УСТАНОВКЕ\nhttps://drive.google.com/file/d/install\n"
                          "ПРОШИВКА\nОпределите версию после LX2.KOR.S5W_M.V. Например V031.002 или V034.003.\n"
                          "ССЫЛКА 1. Базовая версия V034.003", entities=[
            MessageEntity(type="text_link", offset=0, length=10, url="https://drive.google.com/firmware")]),
    ]


def gen5_messages(family="IG19.KOR.SOP", variant="STD_H"):
    return [
        SourceMessage(11, "Mobis GEN5.\nКастомная прошивка с Яндекс.Навигатор, YouTube и другими."),
        SourceMessage(12, f"{family}.V130.220318.{variant}\nHYUNDAI GRANDEUR\n"
                          "(update+bin — установка штатным обновлением по кнопке UPDATE)\n"
                          "📋 ИНСТРУКЦИЯ\nhttps://example.com/instruction\n\n"
                          "📦 УСТАНОВЩИК + КАСТОМ + РУСИФИКАТОР\n"
                          "Если версия на ШГУ не выше V130.220318:\nhttps://example.com/base\n"
                          "Если версия выше V130.220318:\nhttps://example.com/downgrade"),
        SourceMessage(13, "Для данной панели приборов: язык подтягивается с ШГУ", media_kind="photo", size=100062),
    ]


@pytest.mark.parametrize("query,family,variant", [
    ("IG19.KOR.SOP", "IG19.KOR.SOP", None),
    ("IG19.KOR.SOP.V102.181210.STD_H", "IG19.KOR.SOP", "STD_H"),
    ("IGHEV19.KOR.SOP.V141.241119.STD_H", "IGHEV19.KOR.SOP", "STD_H"),
    ("IG19.KOR.SOP.V102.181210.STD_M", "IG19.KOR.SOP", "STD_M"),
])
def test_gen5_sop_supports_actual_instruction_and_newer_target_revision(query, family, variant):
    version = SoftwareVersion.parse(query)
    assert (version.family, version.variant) == (family, variant)
    validate_response(version, gen5_messages(family, variant or "STD_H"), Settings())


def test_gen5_requires_instruction_installer_links_and_requested_hardware_variant():
    version = SoftwareVersion.parse("IG19.KOR.SOP.V102.181210.STD_H")
    for missing in ("📋 ИНСТРУКЦИЯ", "📦 УСТАНОВЩИК", "https://example.com/"):
        messages = gen5_messages()
        messages[1].text = messages[1].text.replace(missing, "")
        with pytest.raises(IncompleteResponse):
            validate_response(version, messages, Settings())
    with pytest.raises(ResponseRejected, match="STD_H/STD_M"):
        validate_response(version, gen5_messages(variant="STD_M"), Settings())
    messages = gen5_messages()
    messages[1].text = messages[1].text.replace(".STD_H", "")
    with pytest.raises(IncompleteResponse, match="окончание версии"):
        validate_response(version, messages, Settings())


@pytest.mark.parametrize("text", ["LX2.KOR.S5W", " lx2.kor.s5w ", "LX2.KOR.S5W_M.V034.003.250117", "LX2.KOR.S5W_M"])
def test_versions_accept_short_and_full_without_changing_characters(text):
    version = SoftwareVersion.parse(text)
    assert version.family == "LX2.KOR.S5W"
    assert version.query == text.strip().upper()
    validate_response(version, complete_messages(), Settings())


@pytest.mark.parametrize("text", ["V034.003", "Привет LX2.KOR.S5W", "LХ2.KOR.S5W", "LXß.KOR.S5W", "LX2.KOR.ſ5W", "https://drive.google.com", "LX2 KOR S5W", "LX2.KOR.S5W\nhello"])
def test_versions_do_not_guess_or_substitute_similar_letters(text):
    with pytest.raises(InvalidVersion):
        SoftwareVersion.parse(text)


def test_other_model_region_and_ambiguous_catalogue_are_rejected():
    version = SoftwareVersion.parse("LX2.KOR.S5W")
    for wrong in ("LX2.EUR.S5W", "LX3.KOR.S5W", "LX2.KOR.S5X"):
        with pytest.raises(ResponseRejected):
            validate_response(version, complete_messages(wrong), Settings())
    with pytest.raises(ResponseRejected):
        validate_response(version, complete_messages() + [SourceMessage(14, "LX3.KOR.S5W_M.V034.003")], Settings())


def test_explicit_not_found_differs_from_incomplete_response():
    version = SoftwareVersion.parse("LX2.KOR.S5W")
    for text in ("Данная версия не найдена", "Прошивка LX2.KOR.S5W не найдена",
                 "LX2.KOR.S5W Поиск не дал результатов. Уточните наличие прошивки у администратора.",
                 "Ошибка! Отправленное сообщение не соответствует символам Software version (SW Version)"):
        with pytest.raises(FirmwareNotFound):
            validate_response(version, [SourceMessage(11, text)], Settings())
    with pytest.raises(IncompleteResponse):
        validate_response(version, complete_messages()[:2], Settings())


def test_source_registration_refusal_is_not_firmware_not_found():
    with pytest.raises(SourceAccessDenied):
        validate_response(SoftwareVersion.parse("LX2.KOR.S5W"), [SourceMessage(1,
            "Регистрация в боте доступна только по ПРОМО ссылке для зарегистрированных представителей.")], Settings())


def test_hidden_link_url_buttons_and_quotes_are_preserved():
    entities = convert_entities([MessageEntityTextUrl(0, 7, "https://example.com/file"), MessageEntityBlockquote(8, 5)])
    assert entities[0].type == "text_link" and entities[0].url.endswith("/file")
    assert entities[1].type == "blockquote"
    raw = SimpleNamespace(id=5, message="Скачать", entities=[], photo=None, document=None, media=None,
                          file=None, noforwards=False, reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[
                              SimpleNamespace(text="Google", url="https://example.com/file"),
                              SimpleNamespace(text="FAQ", data=b"callback"),
                          ])]))
    result = SourceMessage.from_telegram(raw)
    assert result.buttons == [[("Google", "https://example.com/file")]]
    assert result.urls() == ["https://example.com/file"]


@pytest.mark.parametrize("change", [{"protected": True}, {"size": 51 * 1024 * 1024, "media_kind": "document"}, {"media_kind": "unsupported"}])
def test_whole_response_rejected_before_sending_unsupported_attachment(change):
    messages = complete_messages()
    messages.append(SourceMessage(14, **change))
    with pytest.raises(ResponseRejected):
        validate_response(SoftwareVersion.parse("LX2.KOR.S5W"), messages, Settings())


def test_queue_deduplicates_updates_limits_users_and_recovers_crashes(tmp_path):
    db = Database(tmp_path / "requests.db")
    version = SoftwareVersion.parse("LX2.KOR.S5W")
    first, created = db.enqueue(1, 1, 100, version, 2, 0)
    assert created
    assert db.enqueue(1, 1, 100, version, 2, 0) == (first, False)
    assert db.enqueue(1, 1, 101, version, 2, 0) == (first, False)
    with pytest.raises(QueueError):
        db.enqueue(1, 1, 102, SoftwareVersion.parse("LX3.KOR.S5W"), 2, 0)
    db.enqueue(2, 2, 102, version, 2, 0)
    with pytest.raises(QueueError):
        db.enqueue(3, 3, 103, version, 2, 0)
    db.update(first, "searching")
    db.close()
    db = Database(tmp_path / "requests.db")
    db.recover()
    assert db.latest(1)["state"] == "failed"
    assert db.next_request()["user_id"] == 2
    db.close()


class FakeClient:
    def __init__(self, events, reply_to=100, edit=False):
        self.events = events
        self.handlers = []
        self.tasks = []
        self.sent = []
        self.history = []
        self.history_checks = 0

    async def get_messages(self, source, **kwargs):
        self.history_checks += 1
        return self.history

    def add_event_handler(self, callback, builder):
        self.handlers.append(callback)

    def remove_event_handler(self, callback, builder):
        self.handlers.remove(callback)

    async def send_message(self, source, text, **kwargs):
        self.sent.append(text)

        async def emit():
            for delay, message in self.events:
                await asyncio.sleep(delay)
                if self.handlers:
                    await self.handlers[0](SimpleNamespace(message=message))

        self.tasks.append(asyncio.create_task(emit()))
        return SimpleNamespace(id=100)


def raw_message(id, text, *, reply=100, out=False):
    return SimpleNamespace(id=id, message=text, out=out, sender_id=999, reply_to_msg_id=reply,
                           entities=[], photo=None, document=None, media=None, file=None,
                           noforwards=False, reply_markup=None, date=datetime.now(timezone.utc) - timedelta(seconds=60))


def terminal_message(id=114, reply=100):
    return raw_message(id, "ℹ /help\nДля нового поискового запроса\nВведите SW version", reply=reply)


def fake_bridge(tmp_path, events):
    settings = Settings(data_dir=tmp_path, response_timeout=0.16, settle_seconds=0.01, min_collect_seconds=0.02)
    bridge = TelegramBridge(settings, client=FakeClient(events))
    bridge.source = SimpleNamespace(id=999)
    return bridge


def source_card(id=103, family="LX2.KOR.S5W", password=False):
    message = raw_message(id, family + "_M.V---.---.------")
    message.reply_markup = SimpleNamespace(rows=[SimpleNamespace(buttons=[
        KeyboardInlineButton("❔F.A.Q", InlineButtonTypeCallback(b"faq")),
        KeyboardInlineButton("🔗 Ссылки", InlineButtonTypeCallback(b"firmware-links", requires_password=password)),
    ])])
    return message


def gen5_card(id=103, variant="STD_H"):
    message = source_card(id, "IG19.KOR.SOP")
    message.message = f"IG19.KOR.SOP.V---.------.{variant}"
    return message


class CallbackClient(FakeClient):
    def __init__(self, events, callback_timeout=False):
        super().__init__(events)
        self.callbacks = []
        self.callback_timeout = callback_timeout

    async def __call__(self, request):
        self.callbacks.append(request)
        for part in complete_messages():
            await self.handlers[0](SimpleNamespace(message=raw_message(part.id + 100, part.text, reply=request.msg_id)))
        await self.handlers[0](SimpleNamespace(message=terminal_message(reply=request.msg_id)))
        if self.callback_timeout:
            raise asyncio.TimeoutError
        return SimpleNamespace(message=None)


@pytest.mark.parametrize("callback_timeout", [False, True])
def test_bridge_opens_matching_links_once_and_collects_callback_reply(tmp_path, callback_timeout):
    async def scenario():
        client = CallbackClient([(0, raw_message(101, "Scanning...🔎")),
                                 (0, raw_message(102, "ℹ️ /help\n***** Результаты поиска *****")),
                                 (0, source_card())], callback_timeout)
        settings = Settings(data_dir=tmp_path, response_timeout=0.3, settle_seconds=0.01, min_collect_seconds=0.02)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        result = await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert len(client.callbacks) == 1
        assert client.callbacks[0].data == b"firmware-links"
        assert client.callbacks[0].msg_id == 103
        assert [part.id for part in result] == [111, 112, 113]
        assert not bridge.is_blocked()
        with pytest.raises(InvalidVersion):
            SoftwareVersion.parse("LX2.KOR.S5W_M.V---.---.------")
    asyncio.run(scenario())


@pytest.mark.parametrize("cards", [[source_card(family="LX2.EUR.S5W")],
                                    [source_card(password=True)], [source_card(), source_card(id=104)]])
def test_bridge_never_clicks_wrong_region_password_or_ambiguous_cards(tmp_path, cards):
    async def scenario():
        client = CallbackClient([(0, card) for card in cards])
        settings = Settings(data_dir=tmp_path, response_timeout=0.3, settle_seconds=0.01, min_collect_seconds=0.02)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        with pytest.raises(ResponseRejected):
            await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert client.callbacks == []
    asyncio.run(scenario())


@pytest.mark.parametrize("query,cards", [
    ("MQ4HEV.KOR.S5", [source_card(family="MQ4HEV.KOR.S5W")]),
    ("LX2.KOR.S5W", [source_card(), source_card(id=104)]),
    ("IG19.KOR.SOP", [gen5_card(), gen5_card(id=104, variant="STD_M")]),
])
def test_completed_ambiguous_search_does_not_block_next_customer(tmp_path, query, cards):
    async def scenario():
        terminal = raw_message(105, "ℹ /help\n🔍Для нового поискового запроса\n"
                                    "Введите версию софта (SW version)")
        client = CallbackClient([(0, card) for card in cards] + [(0.05, terminal)])
        settings = Settings(data_dir=tmp_path, response_timeout=0.3, settle_seconds=0.01, min_collect_seconds=0.02)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        with pytest.raises(FirmwareNotFound, match="полную строку Software Version"):
            await bridge.lookup(SoftwareVersion.parse(query))
        assert client.callbacks == []
        assert not bridge.is_blocked()
        client.events = [(0, source_card())]
        result = await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert len(client.callbacks) == 1
        assert [m.id for m in result] == [111, 112, 113]
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def test_gen5_full_version_selects_only_matching_hardware_from_two_cards(tmp_path):
    async def scenario():
        class Gen5Client(CallbackClient):
            async def __call__(self, request):
                self.callbacks.append(request)
                for part in gen5_messages():
                    message = raw_message(part.id + 100, part.text, reply=request.msg_id)
                    await self.handlers[0](SimpleNamespace(message=message))
                await self.handlers[0](SimpleNamespace(message=terminal_message(reply=request.msg_id)))

        client = Gen5Client([(0, gen5_card()), (0, gen5_card(id=104, variant="STD_M"))])
        settings = Settings(data_dir=tmp_path, response_timeout=0.3, settle_seconds=0.01, min_collect_seconds=0.02)
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        result = await bridge.lookup(SoftwareVersion.parse("IG19.KOR.SOP.V102.181210.STD_H"))
        assert [request.msg_id for request in client.callbacks] == [103]
        assert [message.id for message in result] == [111, 112, 113]
        assert not any("STD_M" in message.text for message in result)
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def test_completed_but_unsupported_package_fails_only_current_request(tmp_path):
    async def scenario():
        bridge = fake_bridge(tmp_path, [
            (0, raw_message(101, "IG19.KOR.SOP.V130.220318.STD_H\nПрошивка https://example.com/file")),
            (0.04, raw_message(102, "ℹ /help\nДля нового поискового запроса\nВведите SW version")),
        ])
        with pytest.raises(CompletedResponseRejected, match="инструкция"):
            await bridge.lookup(SoftwareVersion.parse("IG19.KOR.SOP"))
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def test_late_supplier_completion_recovers_after_restart_without_replaying_failed_query(tmp_path):
    async def scenario():
        bridge = fake_bridge(tmp_path, [])
        with pytest.raises(SourceUnavailable):
            await bridge.lookup(SoftwareVersion.parse("IG19.KOR.SOP"))
        restarted = fake_bridge(tmp_path, [(0, raw_message(part.id + 100, part.text)) for part in complete_messages()] + [(0, terminal_message())])
        restarted.client.history = [
            raw_message(102, "ℹ /help\nДля нового поискового запроса\nВведите SW version"),
            raw_message(100, "IG19.KOR.SOP", out=True),
        ]
        result = await restarted.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert restarted.client.sent == ["LX2.KOR.S5W"]
        assert any("ИНСТРУКЦИЯ ПО УСТАНОВКЕ" in m.text for m in result)
        assert not restarted.is_blocked()
    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["no_origin", "manual_query", "not_finished", "not_settled", "recent_edit", "nonrecoverable", "wrong_query"])
def test_recovery_requires_own_finished_search_and_never_bypasses_other_blocks(tmp_path, condition):
    async def scenario():
        bridge = fake_bridge(tmp_path, [])
        terminal = raw_message(102, "ℹ /help\nДля нового поискового запроса\nВведите SW version")
        origin = raw_message(100, "IG19.KOR.SOP", out=True)
        bridge._write_state("blocked", "IG19.KOR.SOP", "timeout", sent_id=100, recoverable=condition != "nonrecoverable")
        history = [terminal, origin]
        if condition == "no_origin": history.remove(origin)
        if condition == "manual_query": history.insert(1, raw_message(101, "LX2.KOR.S5W", out=True))
        if condition == "not_finished": terminal.message = "Scanning...🔎"
        if condition == "not_settled": terminal.date = datetime.now(timezone.utc) + timedelta(seconds=1)
        if condition == "recent_edit": terminal.edit_date = datetime.now(timezone.utc) + timedelta(seconds=1)
        if condition == "wrong_query": origin.message = "IGHEV19.KOR.SOP"
        bridge.client.history = history
        assert not await bridge.try_recover()
        assert bridge.is_blocked()
        assert bridge.client.sent == []
    asyncio.run(scenario())


def test_mismatched_response_without_source_completion_stays_blocked_with_reason(tmp_path):
    async def scenario():
        bridge = fake_bridge(tmp_path, [(0, source_card(family="MQ4HEV_22.KOR.S5W"))])
        with pytest.raises(ResponseRejected):
            await bridge.lookup(SoftwareVersion.parse("MQ4HEV.KOR.S5"))
        assert bridge.is_blocked()
        assert "Карточка источника не совпадает" in json.loads(bridge.state_path.read_text(encoding="utf-8"))["reason"]
    asyncio.run(scenario())


def test_modern_inline_url_buttons_are_preserved():
    message = raw_message(1, "Скачать")
    message.reply_markup = SimpleNamespace(rows=[SimpleNamespace(buttons=[
        KeyboardInlineButton("Google", InlineButtonTypeUrl("https://example.com/firmware")),
    ])])
    assert SourceMessage.from_telegram(message).buttons == [[("Google", "https://example.com/firmware")]]


def test_bridge_collects_slow_multiple_messages_edits_and_ignores_history(tmp_path):
    messages = complete_messages()
    events = [(0, raw_message(90, "старый ответ")), (0, raw_message(101, "Ищу…"))]
    for message in messages:
        events.append((0.025, raw_message(message.id + 100, message.text)))
    events.append((0, raw_message(113, messages[-1].text + "\nДополнение источника")))
    events.append((0.02, terminal_message()))

    async def scenario():
        bridge = fake_bridge(tmp_path, events)
        result = await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert [m.id for m in result] == [101, 111, 112, 113]
        assert "Дополнение" in result[-1].text
        assert not bridge.is_blocked()
        assert not bridge.client.handlers

    asyncio.run(scenario())


def test_timeout_blocks_later_queries_even_after_restart(tmp_path):
    async def scenario():
        bridge = fake_bridge(tmp_path, [])
        with pytest.raises(SourceUnavailable):
            await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert bridge.is_blocked()
        restarted = fake_bridge(tmp_path, [])
        with pytest.raises(SourceUnavailable):
            await restarted.lookup(SoftwareVersion.parse("LX3.KOR.S5W"))
        assert restarted.client.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("event", [raw_message(101, "ответ", reply=99), raw_message(101, "другой запрос", out=True)])
def test_bridge_rejects_unrelated_reply_or_parallel_manual_query(tmp_path, event):
    async def scenario():
        bridge = fake_bridge(tmp_path, [(0, event)])
        with pytest.raises(ResponseRejected):
            await bridge.lookup(SoftwareVersion.parse("LX2.KOR.S5W"))
        assert bridge.is_blocked()
    asyncio.run(scenario())


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if method.__api_method__ == "answerCallbackQuery":
            return True
        return Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=method.chat_id, type="private"), text=getattr(method, "text", None))

    async def stream_content(self, *args, **kwargs):
        yield b""


def variant_click(request_id, variant, user_id=11, chat_id=11, update_id=2):
    return Update.model_validate({"update_id": update_id, "callback_query": {
        "id": f"choice-{update_id}", "chat_instance": "test-chat", "data": f"fw_variant:{request_id}:{variant}",
        "from": {"id": user_id, "is_bot": False, "first_name": "User"},
        "message": {"message_id": 100, "date": 1794050000, "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 123456, "is_bot": True, "first_name": "NextMode SW"}, "text": "Выберите вариант"}}})


@pytest.mark.parametrize("variant,card_id", [("STD_H", 103), ("STD_M", 104)])
def test_short_ig19_buttons_to_selected_supplier_firmware_and_instruction(tmp_path, variant, card_id):
    async def scenario():
        class Gen5Client(CallbackClient):
            async def __call__(self, request):
                self.callbacks.append(request)
                target = "STD_H" if request.msg_id == 103 else "STD_M"
                for part in gen5_messages(variant=target):
                    await self.handlers[0](SimpleNamespace(message=raw_message(part.id + 100, part.text, reply=request.msg_id)))
                await self.handlers[0](SimpleNamespace(message=terminal_message(reply=request.msg_id)))

        settings = Settings(data_dir=tmp_path, admin_ids=frozenset({11}), response_timeout=0.3,
                            settle_seconds=0.01, min_collect_seconds=0.02)
        client = Gen5Client([(0, gen5_card()), (0, gen5_card(id=104, variant="STD_M"))])
        bridge = TelegramBridge(settings, client=client)
        bridge.source = SimpleNamespace(id=999)
        db = Database(tmp_path / "queue.db")
        session = RecordingSession()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, bridge))
        service = Service(settings, db, bridge, Messenger(bot, settings))
        update = Update.model_validate({"update_id": 1, "message": {
            "message_id": 10, "date": 1794050000, "chat": {"id": 11, "type": "private"},
            "from": {"id": 11, "is_bot": False, "first_name": "User"}, "text": "IG19.KOR.SOP"}})
        await dispatcher.feed_update(bot, update)
        request = db.latest(11)
        assert request["state"] == "awaiting_variant"
        buttons = session.methods[-1].reply_markup.inline_keyboard[0]
        assert [b.text for b in buttons] == ["STD_H", "STD_M"]
        assert [b.callback_data for b in buttons] == [f"fw_variant:{request['id']}:STD_H", f"fw_variant:{request['id']}:STD_M"]
        assert not await service.step()
        assert client.sent == []
        await dispatcher.feed_update(bot, variant_click(request["id"], variant))
        selected = db.next_request()
        assert selected["id"] == request["id"] and selected["variant"] == variant
        assert selected["query"] == "IG19.KOR.SOP"
        await service.step()
        assert client.sent == ["IG19.KOR.SOP"]
        assert [call.msg_id for call in client.callbacks] == [card_id]
        assert db.latest(11)["state"] == "delivered"
        texts = [m.text for m in session.methods if getattr(m, "text", None)]
        assert any(f"IG19.KOR.SOP.V130.220318.{variant}" in text and "📋 ИНСТРУКЦИЯ" in text for text in texts)
        assert f"Комплект по запросу #{request['id']} передан полностью" in texts[-1]
        await dispatcher.feed_update(bot, variant_click(request["id"], variant, update_id=3))
        assert db.next_request() is None
        assert not await service.step()
        assert client.sent == ["IG19.KOR.SOP"]
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("user_id,chat_id,variant", [(12, 11, "STD_H"), (11, 12, "STD_H"), (11, 11, "STD_X")])
def test_variant_callback_cannot_select_another_users_request_or_forge_variant(tmp_path, user_id, chat_id, variant):
    async def scenario():
        settings = Settings(data_dir=tmp_path, owner_only=False)
        db = Database(tmp_path / "queue.db")
        version = SoftwareVersion.parse("IG19.KOR.SOP")
        request_id, _ = db.enqueue(11, 11, 10, version, 20, 0, variants=version.selectable_variants())
        session = RecordingSession()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, SimpleNamespace(is_blocked=lambda: False)))
        await dispatcher.feed_update(bot, variant_click(request_id, variant, user_id, chat_id))
        assert db.latest(11)["state"] == "awaiting_variant"
        assert db.next_request() is None
        assert session.methods[-1].show_alert
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


def test_variant_choice_persists_restart_and_replacing_query_invalidates_old_buttons(tmp_path):
    path = tmp_path / "queue.db"
    db = Database(path)
    version = SoftwareVersion.parse("IG19.KOR.SOP")
    first, _ = db.enqueue(11, 11, 1, version, 20, 0, variants=version.selectable_variants())
    db.close()
    db = Database(path)
    db.recover()
    selected = db.select_variant(first, 11, 11, "STD_M", 20)
    assert selected["query"] == "IG19.KOR.SOP" and selected["variant"] == "STD_M"
    db.update(first, "delivered")
    second, _ = db.enqueue(11, 11, 2, version, 20, 0, variants=version.selectable_variants())
    full = SoftwareVersion.parse("IG19.KOR.SOP.V102.181210.STD_H")
    third, _ = db.enqueue(11, 11, 3, full, 20, 300)
    with pytest.raises(QueueError):
        db.select_variant(second, 11, 11, "STD_M", 20)
    assert db.next_request()["id"] == third and db.next_request()["variant"] == "STD_H"
    db.close()


def test_full_queue_keeps_variant_buttons_usable_until_space_is_available(tmp_path):
    db = Database(tmp_path / "queue.db")
    version = SoftwareVersion.parse("IG19.KOR.SOP")
    request_id, _ = db.enqueue(11, 11, 1, version, 1, 0, variants=version.selectable_variants())
    other, _ = db.enqueue(12, 12, 1, SoftwareVersion.parse("LX2.KOR.S5W"), 1, 0)
    with pytest.raises(QueueError, match="Очередь заполнена"):
        db.select_variant(request_id, 11, 11, "STD_H", 1)
    assert db.latest(11)["state"] == "awaiting_variant"
    db.update(other, "delivered")
    assert db.select_variant(request_id, 11, 11, "STD_H", 1)["state"] == "queued"
    db.close()


def test_existing_firmware_database_is_migrated_without_losing_requests(tmp_path):
    path = tmp_path / "queue.db"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER,message_id INTEGER,query TEXT,family TEXT,
        state TEXT,created REAL,updated REAL,detail TEXT DEFAULT '',notified INTEGER DEFAULT 0,
        UNIQUE(chat_id,message_id));
        INSERT INTO requests(user_id,chat_id,message_id,query,family,state,created,updated)
        VALUES(11,11,1,'LX2.KOR.S5W','LX2.KOR.S5W','delivered',1,1);""")
    old.close()
    db = Database(path)
    existing = db.latest(11)
    assert existing["state"] == "delivered" and existing["query"] == "LX2.KOR.S5W"
    assert existing["variant"] == "" and json.loads(existing["variants"]) == []
    db.close()


def test_real_dispatcher_enqueues_owner_only_private_text_and_preserves_hidden_links(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path, admin_ids=frozenset({11}))
        db = Database(tmp_path / "queue.db")
        session = RecordingSession()
        bot = Bot("123456:TEST_TOKEN_ONLY", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, db, SimpleNamespace(is_blocked=lambda: False)))
        for user, chat_type in [(12, "private"), (11, "group"), (11, "private")]:
            update = Update.model_validate({"update_id": user, "message": {
                "message_id": user, "date": 1794050000, "chat": {"id": user, "type": chat_type},
                "from": {"id": user, "is_bot": False, "first_name": "User"}, "text": "LX2.KOR.S5W"}})
            await dispatcher.feed_update(bot, update)
        assert db.latest(12) is None
        request = db.next_request()
        assert request["user_id"] == 11
        await Messenger(bot, settings).deliver(request, complete_messages(), {})
        hidden = [method for method in session.methods if getattr(method, "entities", None)]
        assert hidden and hidden[-1].entities[0].url == "https://drive.google.com/firmware"
        assert hidden[-1].parse_mode is None
        assert "передан полностью" in session.methods[-1].text
        await bot.session.close()
        db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("error,expected", [(FirmwareNotFound("Версия не найдена"), "not_found"), (SourceUnavailable("таймаут"), "failed"),
                                           (CompletedResponseRejected("Не получена инструкция"), "failed")])
def test_service_never_delivers_unvalidated_response(tmp_path, error, expected):
    async def scenario():
        settings = Settings(data_dir=tmp_path)
        db = Database(tmp_path / "queue.db")
        db.enqueue(1, 1, 1, SoftwareVersion.parse("LX2.KOR.S5W"), 20, 0)

        class Bridge:
            async def lookup(self, version):
                raise error

        class Recorder:
            def __init__(self):
                self.users = []

            async def notify_user(self, chat, text):
                self.users.append(text)

            async def notify_admins(self, text):
                pass

            async def deliver(self, *args):
                pytest.fail("Invalid response delivered")

        messenger = Recorder()
        await Service(settings, db, Bridge(), messenger).step()
        assert db.latest(1)["state"] == expected
        assert len(messenger.users) == 1
        if expected == "failed":
            assert "не найдена" not in messenger.users[0]
        db.close()
    asyncio.run(scenario())


def test_configuration_is_isolated_from_license_bot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "existing-license-token")
    env = tmp_path / ".firmware.env"
    env.write_text("FIRMWARE_DATA_DIR=data/firmware\n", encoding="utf-8")
    settings = Settings.load(env)
    assert settings.bot_token == ""
    assert settings.owner_only
    assert settings.data_dir == tmp_path / "data/firmware"


def test_server_marker_prevents_connecting_a_second_copy_of_the_account(tmp_path):
    (tmp_path / "server-deployment.json").write_text('{"active":true}', encoding="utf-8")
    settings = Settings(data_dir=tmp_path)

    class Client:
        called = False

        async def connect(self):
            self.called = True

    client = Client()
    bridge = TelegramBridge(settings, client=client)
    with pytest.raises(RuntimeError, match="на сервере"):
        asyncio.run(bridge.connect())
    assert not client.called


def test_worker_delivers_each_users_own_response_and_prepares_all_files_first(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path)
        db = Database(tmp_path / "queue.db")
        for user, query in [(1, "LX2.KOR.S5W"), (2, "LX3.KOR.S5W")]:
            db.enqueue(user, user, 1, SoftwareVersion.parse(query), 20, 0)
        downloads = []
        temporary_paths = []

        class Bridge:
            async def lookup(self, version):
                messages = [SourceMessage(1, version.family + " ИНСТРУКЦИЯ ПО УСТАНОВКЕ ПРОШИВКА"),
                            SourceMessage(2, media_kind="document"), SourceMessage(3, media_kind="photo")]
                validate_response(version, messages, settings)
                return messages

            async def download(self, message, folder):
                path = folder / "attachment.bin"
                path.write_bytes(b"synthetic attachment")
                downloads.append(message.id)
                temporary_paths.append(path)
                return path

        class Recorder:
            def __init__(self):
                self.deliveries = []

            async def deliver(self, request, messages, files):
                assert len(downloads) % 2 == 0
                assert set(files) == {2, 3}
                assert all(path.is_file() for path in files.values())
                self.deliveries.append((request["chat_id"], messages[0].text))

            async def notify_user(self, *args):
                pytest.fail("Successful delivery should not produce a failure notification")

        recorder = Recorder()
        service = Service(settings, db, Bridge(), recorder)
        assert await service.step()
        assert await service.step()
        assert not await service.step()
        assert recorder.deliveries == [(1, "LX2.KOR.S5W ИНСТРУКЦИЯ ПО УСТАНОВКЕ ПРОШИВКА"),
                                      (2, "LX3.KOR.S5W ИНСТРУКЦИЯ ПО УСТАНОВКЕ ПРОШИВКА")]
        assert all(not path.exists() for path in temporary_paths)
        assert db.latest(1)["state"] == db.latest(2)["state"] == "delivered"
        db.close()
    asyncio.run(scenario())


def test_firmware_server_bundle_contains_no_credentials_sessions_or_customer_files(tmp_path):
    from pathlib import Path
    import tarfile
    root = Path(__file__).resolve().parent.parent
    archive_path = build(root, tmp_path / "firmware.tar.gz")
    with tarfile.open(archive_path) as archive:
        names = archive.getnames()
        assert ".firmware.env.example" in names
        assert "deploy/nextmode-sw.service" in names
        assert all(not name.startswith("data/") for name in names)
        assert ".env" not in names and ".firmware.env" not in names
        assert not any(name.endswith((".session", ".sqlite3", ".bin")) for name in names)
        assert all(not item.issym() and not item.islnk() for item in archive.getmembers())

import asyncio
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from .service import Service
from .api import make_bot
from .presentation import customer_text
from .storage import QueueError
from .versions import InvalidVersion, SoftwareVersion
from .catalog import CATALOG, find_entry, catalog_message_id


HELP = ("DriveMods SW — прошивки и инструкции.\n\n"
        "Отправьте точное обозначение из строки Software Version на ШГУ. Можно короткое, например:\n"
        "LX2.KOR.S5W\n\nИли полное:\nLX2.KOR.S5W_M.V034.003.250117\n\n"
        "Бот проверит ответ источника и пришлёт прошивку, ссылки и инструкции. "
        "Для IG19.KOR.SOP предложит кнопки STD_H и STD_M — выберите окончание своей версии. "
        "Если версия не найдена, проверьте символы, модель и регион.\n\n"
        "/catalog — каталог прошивок, ранее выдававшихся в старом боте. "
        "Можно выбрать точное обозначение кнопкой или отправить другую версию текстом.")


class Messenger:
    def __init__(self, bot, settings):
        self.bot = bot
        self.settings = settings

    async def notify_user(self, chat_id, text):
        await self.bot.send_message(chat_id, text, parse_mode=None)

    async def notify_admins(self, text):
        for user_id in self.settings.admin_ids:
            with suppress(TelegramAPIError):
                await self.notify_user(user_id, text)

    async def deliver(self, request, messages, files):
        chat_id = request["chat_id"]
        target = request['family'] + (" " + request["variant"] if request.get("variant") else "")
        await self.notify_user(chat_id, f"Прошивка найдена для {target}. Передаю комплект по запросу #{request['id']}…")
        for message in messages:
            text, entities = customer_text(message.text, message.entities)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=label, url=url) for label, url in row] for row in message.buttons
            ]) if message.buttons else None
            file = files.get(message.id)
            if not file:
                if text:
                    await self.bot.send_message(chat_id, text, entities=entities, parse_mode=None,
                                                reply_markup=keyboard, link_preview_options=LinkPreviewOptions(is_disabled=True))
                elif keyboard:
                    await self.bot.send_message(chat_id, "Ссылки источника:", reply_markup=keyboard, parse_mode=None)
                continue
            # Premium source captions may exceed the Bot API caption length.
            separate_caption = len(text.encode("utf-16-le")) // 2 > 1024
            if separate_caption and text:
                await self.bot.send_message(chat_id, text, entities=entities, parse_mode=None,
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
            kwargs = dict(chat_id=chat_id, caption=None if separate_caption else text or None,
                          caption_entities=None if separate_caption else entities, parse_mode=None, reply_markup=keyboard)
            if message.media_kind == "photo":
                await self.bot.send_photo(photo=FSInputFile(file), **kwargs)
            else:
                await self.bot.send_document(document=FSInputFile(file), **kwargs)
        await self.notify_user(chat_id, f"Комплект по запросу #{request['id']} передан полностью. Следуйте инструкции источника для вашей текущей версии ПО.")


def make_router(settings, db, bridge):
    router = Router(name="firmware")
    # Do not receive customer firmware requests in groups.
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    def allowed(message):
        return message.from_user and (not settings.owner_only or message.from_user.id in settings.admin_ids)

    def progress(request):
        states = dict(awaiting_variant="выберите STD_H или STD_M кнопкой под запросом", cancelled="заменён новым запросом",
                      queued="в очереди", searching="ищу у источника", delivering="передаю комплект",
                      delivered="комплект передан", not_found="версия не найдена", failed="не выполнен")
        text = states[request["state"]]
        if request["state"] == "queued":
            text += f"; место: {db.queue_position(request['id'])}"
        if request["state"] in {"queued", "searching", "delivering"}:
            text += f"; прошло {max(0, int(time.time() - request['created']))} сек."
        return text

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message):
        if not allowed(message):
            await message.answer("DriveMods SW пока настраивается. Доступ к проверке есть у владельца.", parse_mode=None)
            return
        await message.answer(HELP, parse_mode=None)

    @router.message(Command('catalog'))
    async def catalog(message: Message):
        if not allowed(message):
            await message.answer('Каталог пока доступен владельцу.', parse_mode=None)
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=entry.label, callback_data='fw_catalog:' + entry.key)
        ] for entry in CATALOG])
        await message.answer(
            'Прошивки из старого бота\n\n'
            'Выберите обозначение, совпадающее со строкой Software Version вашей ШГУ. '
            'Для SOP также должно совпадать окончание STD_H/STD_M.\n\n'
            'Эти семейства уже успешно выдавались. После выбора заново проверю наличие '
            'прошивки и полный комплект инструкций у источника. '
            'Другие версии тоже можно отправлять текстом.', reply_markup=keyboard, parse_mode=None)

    @router.callback_query(F.data.startswith('fw_catalog:'))
    async def choose_catalog(callback: CallbackQuery):
        if (not isinstance(callback.message, Message) or callback.message.chat.type != ChatType.PRIVATE
                or callback.message.chat.id != callback.from_user.id
                or (settings.owner_only and callback.from_user.id not in settings.admin_ids)):
            await callback.answer('Этот выбор недоступен.', show_alert=True)
            return
        entry = find_entry(callback.data.removeprefix('fw_catalog:'))
        if entry is None:
            await callback.answer('Этой кнопки нет в каталоге. Откройте /catalog заново.', show_alert=True)
            return
        try:
            request_id, created = db.enqueue(
                callback.from_user.id, callback.message.chat.id, catalog_message_id(callback.id),
                entry.version(), settings.max_queue, settings.user_cooldown)
        except QueueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        # Capture the place before answering Telegram: the worker may already
        # start this request during that await, when queue_position returns 0.
        accepted_position = db.queue_position(request_id) if created else 0
        with suppress(TelegramAPIError):
            await callback.answer('Запрос поставлен в очередь.' if created else 'Этот запрос уже принят.')
        request = db.get(request_id)
        selected = '\nВыбран вариант: ' + request['variant'] if request.get('variant') else ''
        if created:
            await callback.message.answer(
                f"Запрос #{request_id} принят: {request['query']}{selected}\n"
                f'Место в очереди: {accepted_position}. '
                'Когда подойдёт ваша очередь, проверю прошивку и инструкцию. Статус: /request', parse_mode=None)
        else:
            await callback.message.answer(
                f"Запрос #{request_id} уже принят: {request['query']}{selected}\n"
                f'Статус: {progress(request)}\nПовторять выбор не нужно. Статус: /request', parse_mode=None)

    @router.message(Command("myid"))
    async def myid(message: Message):
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}", parse_mode=None)

    @router.message(Command("support"))
    async def support(message: Message):
        await message.answer(settings.support_contact or "Поддержка пока не настроена владельцем.", parse_mode=None)

    @router.message(Command("status"))
    async def status(message: Message):
        if message.from_user.id not in settings.admin_ids:
            return
        searching = getattr(bridge, "is_searching", lambda: False)()
        if bridge.is_blocked() and not searching:
            await bridge.try_recover()
        state = "выполняет поиск" if searching else "готова"
        if bridge.is_blocked() and not searching:
            state = "ожидает завершения ответа, проверяется автоматически" if bridge.recovery_pending() else "ожидает проверки владельцем"
        counts = db.counts()
        await message.answer(f"DriveMods SW\nРазмещение: {settings.instance_label}\nИсточник: @{settings.source_bot}\nСвязка: {state}\n"
                             f"Очередь: {counts.get('queued', 0)}; поиск: {counts.get('searching', 0)}; передача: {counts.get('delivering', 0)}\n"
                             f"Доступ: {'только владелец' if settings.owner_only else 'пользователи'}", parse_mode=None)

    @router.message(Command("request"))
    async def request_status(message: Message):
        if not allowed(message):
            return
        request = db.latest(message.from_user.id)
        if not request:
            await message.answer("Запросов пока нет.", parse_mode=None)
            return
        selected = "\nВыбран вариант: " + request["variant"] if request.get("variant") else ""
        await message.answer(f"Запрос #{request['id']}: {request['query']}{selected}\nСтатус: {progress(request)}", parse_mode=None)

    @router.callback_query(F.data.startswith("fw_variant:"))
    async def choose_variant(callback: CallbackQuery):
        if (not isinstance(callback.message, Message) or callback.message.chat.type != ChatType.PRIVATE
                or (settings.owner_only and callback.from_user.id not in settings.admin_ids)):
            await callback.answer("Этот выбор недоступен.", show_alert=True)
            return
        try:
            _, raw_id, variant = callback.data.split(":")
            request_id = int(raw_id)
            request = db.select_variant(request_id, callback.from_user.id, callback.message.chat.id, variant, settings.max_queue)
        except (ValueError, QueueError) as exc:
            detail = str(exc) if isinstance(exc, QueueError) else "Некорректная кнопка выбора."
            await callback.answer(detail, show_alert=True)
            return
        with suppress(TelegramAPIError):
            await callback.answer(f"Выбран {variant}. Запрос поставлен в очередь.")
        await callback.message.edit_text(f"Запрос #{request_id} принят: {request['query']}\nВыбран вариант: {variant}.\n"
                                         f"Место в очереди: {db.queue_position(request_id)}.\n"
                                         "Когда подойдёт ваша очередь, бот начнёт поиск. Статус: /request", reply_markup=None, parse_mode=None)

    @router.message(F.text)
    async def text(message: Message):
        if not allowed(message):
            await message.answer("DriveMods SW пока настраивается. Доступ к проверке есть у владельца.", parse_mode=None)
            return
        if message.text.startswith("/"):
            await message.answer("Отправьте версию ПО текстом или используйте /help.", parse_mode=None)
            return
        try:
            version = SoftwareVersion.parse(message.text)
            request_id, created = db.enqueue(message.from_user.id, message.chat.id, message.message_id, version,
                                            settings.max_queue, settings.user_cooldown, variants=version.selectable_variants())
            if created:
                variants = version.selectable_variants()
                if variants:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=variant, callback_data=f"fw_variant:{request_id}:{variant}") for variant in variants
                    ]])
                    await message.answer(f"Запрос #{request_id}: {version.query}\nВыберите окончание, указанное в строке "
                                         "Software Version на ШГУ:", reply_markup=keyboard, parse_mode=None)
                else:
                    if getattr(bridge, "is_searching", lambda: False)():
                        waiting = (f"Запрос в очереди, место: {db.queue_position(request_id)}. "
                                   "Сейчас обрабатывается другая заявка. Повторять версию не нужно.")
                    elif bridge.is_blocked():
                        waiting = (f"Место в очереди: {db.queue_position(request_id)}. "
                                   "Источник временно недоступен. Запрос сохранён, бот продолжит автоматически после восстановления.")
                    else:
                        waiting = (f"Место в очереди: {db.queue_position(request_id)}. "
                                   "Когда подойдёт ваша очередь, начну поиск и пришлю комплект по вашей версии.")
                    await message.answer(f"Запрос #{request_id} принят: {version.query}\n{waiting}\nСтатус: /request", parse_mode=None)
            else:
                existing = db.get(request_id)
                await message.answer(f"Запрос #{request_id} уже принят: {existing['query']}\nСтатус: {progress(existing)}\n"
                                     "Повторять версию не нужно. Статус: /request", parse_mode=None)
        except (InvalidVersion, QueueError) as exc:
            await message.answer(str(exc), parse_mode=None)

    @router.message()
    async def other(message: Message):
        await message.answer("Отправьте версию ПО текстом из строки Software Version. Пример: LX2.KOR.S5W", parse_mode=None)

    return router


async def run_bot(settings, db, bridge):
    bot = make_bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, db, bridge))
    worker = None
    try:
        await bridge.connect()
        me = await bot.get_me()
        if me.id == bridge.source.id:
            raise ValueError("Бот-получатель должен отличаться от источника прошивок.")
        if (await bot.get_webhook_info()).url:
            raise ValueError("У нового бота настроен webhook. Удалите его перед запуском polling.")
        await bot.set_my_commands([
            BotCommand(command="start", description="Получить прошивку и инструкцию"),
            BotCommand(command="catalog", description="Прошивки из старого бота"),
            BotCommand(command="request", description="Статус моего запроса"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="myid", description="Мой Telegram ID"),
        ])
        service = Service(settings, db, bridge, Messenger(bot, settings))
        worker = asyncio.create_task(service.worker())
        await dispatcher.start_polling(bot, close_bot_session=False, handle_as_tasks=True, tasks_concurrency_limit=32,
                                       allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()
        await bridge.close()

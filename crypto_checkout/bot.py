"""Private Telegram checkout router and explicitly gated single-poller runner.

No credentials, environment loading, payment executor, or live startup defaults
are supplied here. The caller must construct and verify every live dependency.
"""
import asyncio
from datetime import datetime, timezone
from io import BytesIO
import logging
import re
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, CopyTextButton, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from .checkout import CheckoutUnavailable, invoice_text
from .payment_qr import payment_details
from .runtime import _TokenLogFilter


MAX_UPLOAD = 1_048_576
UNAVAILABLE = ('Автоматическое оформление временно недоступно. '
               'Новый счёт сейчас не выставляется. Статус существующего заказа: /orders.')
UNTRUSTED_RECEIPT = ('Чек, скриншот или сообщение об оплате не подтверждают поступление. '
                     'Бот проверяет реальное поступление по реквизитам вашего счёта. '
                     'Статус вашего заказа: /orders.')
STATE_LABELS = {
    'awaiting_payment': 'Ожидается точная сумма по счёту',
    'chain_confirmed': 'Перевод подтверждён сетью; ожидается зачисление Binance',
    'paid': 'Оплата подтверждена; заказ ожидает очереди оформления лицензии',
    'preparing': 'Оформляется заказ в DriveMods',
    'prepared': 'Заказ DriveMods подготовлен',
    'inspecting': 'Проверяется состояние заказа DriveMods',
    'charging': 'Обрабатывается покупка в DriveMods',
    'waiting_supplier': 'Ожидается подтверждение DriveMods',
    'action_required': 'Нужно подтверждение владельца карты; заказ сохранён',
    'purchase_review': 'Проверяется состояние покупки; заказ сохранён',
    'payment_review': 'Платёж требует проверки; заказ сохранён',
    'download_pending': 'Лицензия оплачена; ожидается получение файла',
    'downloading': 'Получается готовая лицензия',
    'ready': 'Лицензия готова к отправке',
    'sending': 'Отправляется файл лицензии',
    'delivered': 'Готовая лицензия отправлена в этот диалог',
}


class _BoundedBuffer(BytesIO):
    def write(self, data):
        if max(len(self.getbuffer()), self.tell() + len(data)) > MAX_UPLOAD:
            raise ValueError('Device ID file exceeds the upload limit')
        return super().write(data)


def _private_user(message):
    user = message.from_user if isinstance(message, Message) else None
    if (user and not user.is_bot and type(user.id) is int and user.id > 0
            and message.chat.type == 'private' and message.chat.id == user.id):
        return user.id
    return None


def _ready(checkout):
    value = checkout.readiness() if callable(checkout.readiness) else checkout.readiness
    return (value.ready is True and (checkout.allow_unique_cents is True or getattr(checkout,'receiving_wallet',None) is not None)
            and checkout.ledger.simulation is False)


def order_summary(invoice):
    state = invoice['state']
    label = STATE_LABELS.get(state, 'Состояние заказа проверяется')
    if state == 'awaiting_payment' and invoice['expires_ms'] < int(time.time() * 1000):
        label = 'Срок счёта истёк. Для нового счёта отправьте Device ID ещё раз'
    return 'Заказ ' + invoice['id'] + ' · ' + invoice['plan'].upper() + '\n' + label


def payment_text(invoice):
    if invoice['expires_ms'] < int(time.time() * 1000):
        return order_summary(invoice)
    deadline = datetime.fromtimestamp(invoice['expires_ms'] / 1000, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    return invoice_text(invoice) + '\nСрок оплаты: до ' + deadline + '.\n/status ' + invoice['id']


async def send_payment(message, invoice):
    if invoice['state'] != 'awaiting_payment' or invoice['expires_ms'] <= int(time.time() * 1000):
        await message.answer(order_summary(invoice))
        return
    try:
        address, amount = payment_details(invoice)
    except ValueError:
        # Expiry can occur between the first check and validation; don't issue
        # fresh-looking payment instructions for that invoice.
        await message.answer('Счёт недоступен для оплаты. Проверьте /status ' + invoice['id'])
        return
    text = payment_text(invoice) + (
        '\n\nОплата через «Мой О!»:\n'
        '1. Откройте «Криптовалюта» и выберите USDT в сети TRON (TRC20).\n'
        '2. Нажмите «Скопировать адрес» ниже и вставьте его в поле адреса получателя.\n'
        '3. Укажите сумму из счёта, проверьте данные и подтвердите оплату.\n'
        'Используйте этот адрес только для этого заказа.')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Скопировать адрес', copy_text=CopyTextButton(text=address)),
        InlineKeyboardButton(text='Скопировать сумму', copy_text=CopyTextButton(text=amount))]])
    await message.answer(text, reply_markup=keyboard)


def make_router(checkout):
    router = Router(name='drivemods_crypto_checkout')
    router.message.filter(F.chat.type == 'private')
    router.callback_query.filter(F.message.chat.type == 'private')

    @router.message(CommandStart())
    async def start(message: Message):
        if not _private_user(message):
            return
        if not _ready(checkout):
            await message.answer(UNAVAILABLE)
            return
        payment_hint=('Каждый заказ получает отдельный адрес: FULL ровно 100 USDT, ECO ровно 70 USDT. '
                      if getattr(checkout,'receiving_wallet',None) is not None else
                      'Перед оплатой бот покажет точную сумму счёта с добавлением 0,01–0,99 USDT для определения вашего платежа. ')
        await message.answer(
            'Пришлите файл идентификатора устройства Device ID (.bin или .id, до 1 МБ). '
            'Затем выберите FULL — 100 USDT или ECO — 70 USDT. '
            +payment_hint+'После подтверждённой оплаты начнётся оформление лицензии.\n'
            'Мои заказы: /orders. Статус: /status номер_заказа.')

    @router.message(Command('orders'))
    async def orders(message: Message):
        user = _private_user(message)
        if not user:
            return
        rows = checkout.ledger.list_by_user(user, limit=10)
        await message.answer('\n\n'.join(order_summary(row) for row in rows) if rows else
                             'У вас пока нет заказов. Пришлите файл Device ID для начала.')

    @router.message(Command('status'))
    async def status(message: Message):
        user = _private_user(message)
        if not user:
            return
        parts = (message.text or '').split()
        try:
            if len(parts) == 2 and re.fullmatch(r'[0-9a-f]{32}', parts[1]):
                row = checkout.status(parts[1], user)
            elif len(parts) == 1:
                rows = checkout.ledger.list_by_user(user, limit=1)
                if not rows:
                    await message.answer('У вас пока нет заказов.')
                    return
                row = rows[0]
            else:
                await message.answer('Укажите /status и номер своего заказа.')
                return
        except ValueError:
            await message.answer('Заказ не найден в вашем диалоге. Ваши заказы: /orders.')
            return
        if row['state'] == 'awaiting_payment' and _ready(checkout):
            await send_payment(message, row)
        else:
            text = order_summary(row)
            if row['state'] == 'awaiting_payment' and not _ready(checkout):
                text += '\nПриём новых платежей приостановлен. Если перевод уже сделан, заказ сохранён.'
            await message.answer(text)

    @router.message(F.document)
    async def document(message: Message, bot: Bot):
        user = _private_user(message)
        if not user:
            return
        if not _ready(checkout):
            await message.answer(UNAVAILABLE)
            return
        doc = message.document
        filename = doc.file_name or ''
        if (not re.search(r'\.(?:bin|id)$', filename, flags=re.IGNORECASE)
                or doc.mime_type in {'application/pdf', 'image/png', 'image/jpeg', 'image/webp'}):
            await message.answer('Пришлите исходный файл Device ID с расширением .bin или .id. '
                                 'Чек или изображение не меняют состояние оплаты.')
            return
        if doc.file_size is not None and not 0 < doc.file_size <= MAX_UPLOAD:
            await message.answer('Файл Device ID должен быть размером от 1 байта до 1 МБ.')
            return
        try:
            file = await bot.get_file(doc.file_id)
            if (not file.file_path or file.file_size is not None
                    and not 0 < file.file_size <= MAX_UPLOAD):
                raise ValueError('Device ID file is unavailable or too large')
            buffer = _BoundedBuffer()
            try:
                await bot.download_file(file.file_path, destination=buffer, seek=True)
                uploaded = checkout.upload(user, filename, buffer.getvalue(),
                                           f'tg:{message.chat.id}:{message.message_id}')
            finally:
                buffer.close()
        except ValueError:
            await message.answer('Не удалось принять Device ID. Проверьте исходный файл и размер до 1 МБ.')
            return
        except Exception as exc:
            logging.getLogger(__name__).warning('Device download deferred: %s', type(exc).__name__)
            await message.answer('Не удалось получить файл из Telegram. Отправьте его ещё раз позже.')
            return
        buttons = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='FULL · 100 USDT', callback_data='choose:' + uploaded['id'] + ':full'),
            InlineKeyboardButton(text='ECO · 70 USDT', callback_data='choose:' + uploaded['id'] + ':eco')]])
        hint=('Для заказа будет создан отдельный адрес оплаты.' if getattr(checkout,'receiving_wallet',None) is not None
              else 'В счёте будет указана точная сумма с добавлением 0,01–0,99 USDT для определения вашего перевода.')
        await message.answer('Device ID получен. Выберите пакет. '+hint,
                             reply_markup=buttons)

    @router.callback_query(F.data.startswith('choose:'))
    async def choose(call: CallbackQuery):
        message = call.message
        if (not isinstance(message, Message) or message.chat.id != call.from_user.id
                or call.from_user.is_bot):
            await call.answer('Кнопка доступна только владельцу этого заказа.', show_alert=True)
            return
        match = re.fullmatch(r'choose:([0-9a-f]{32}):(full|eco)', call.data or '')
        if not match:
            await call.answer('Некорректная кнопка заказа.', show_alert=True)
            return
        try:
            row = await checkout.choose(match[1], call.from_user.id, match[2])
        except CheckoutUnavailable:
            await call.answer('Автоматическое оформление пока недоступно.', show_alert=True)
            await message.answer(UNAVAILABLE)
            return
        except ValueError:
            await call.answer('Заказ недоступен. Откройте /orders в своём диалоге.', show_alert=True)
            return
        except Exception as exc:
            logging.getLogger(__name__).warning('Invoice creation deferred: %s', type(exc).__name__)
            await call.answer('Не удалось подготовить счёт. Повторите позже.', show_alert=True)
            return
        await call.answer()
        await send_payment(message, row)

    @router.message(F.photo)
    async def photo(message: Message):
        if _private_user(message):
            await message.answer(UNTRUSTED_RECEIPT)

    @router.message()
    async def other(message: Message):
        if _private_user(message):
            await message.answer(UNTRUSTED_RECEIPT + '\nДля нового заказа пришлите файл Device ID.')

    @router.error()
    async def safe_error(event: ErrorEvent):
        logging.getLogger(__name__).warning('Checkout handler deferred: %s', type(event.exception).__name__)
        return True

    return router


async def run(checkout, worker, *, token, expected_bot_id, expected_username='DriveMods_licensebot',
              exclusive_poller_confirmed=False, session=None, dispatcher=None, stop=None):
    """Run one explicitly configured live bot, only after worker startup succeeds.

    `session`/`dispatcher` are injectable for testing; callers must never start
    another poller for the same token. No webhook is deleted automatically.
    """
    if (checkout.ledger is not worker.ledger or checkout.ledger.simulation
            or type(expected_bot_id) is not int or expected_bot_id <= 0
            or not worker.readiness().ready or not _ready(checkout)):
        raise CheckoutUnavailable('Live bot prerequisites are not verified')
    if exclusive_poller_confirmed is not True:
        raise CheckoutUnavailable('Existing bot poller must be stopped and exclusive ownership verified')
    if (expected_username is not None and (not isinstance(expected_username, str)
            or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', expected_username))):
        raise ValueError('Expected bot identity must be specified explicitly')
    bot = Bot(token=token, session=session)
    token_filter = _TokenLogFilter(token)
    filtered_loggers = [logging.getLogger(name) for name in
                        ('aiohttp.client', 'aiogram.dispatcher', 'aiogram.event')]
    for logger in filtered_loggers:
        logger.addFilter(token_filter)
    stop = stop if stop is not None else asyncio.Event()
    worker_task = startup_task = poller_task = stopper_task = None
    try:
        identity = await bot.get_me()
        if (identity.id != expected_bot_id or identity.is_bot is not True
                or expected_username is not None
                and (identity.username or '').casefold() != expected_username.casefold()):
            raise CheckoutUnavailable('Configured Telegram token belongs to another bot')
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise CheckoutUnavailable('Existing webhook must be reviewed before polling')
        dispatcher = dispatcher if dispatcher is not None else Dispatcher()
        if any(router.name == 'drivemods_crypto_checkout' for router in dispatcher.sub_routers):
            raise CheckoutUnavailable('Checkout router is already registered in this dispatcher')
        dispatcher.include_router(make_router(checkout))
        worker_task = asyncio.create_task(worker.run(stop))
        startup_task = asyncio.create_task(worker.started.wait())
        await asyncio.wait({worker_task, startup_task}, return_when=asyncio.FIRST_COMPLETED)
        if worker_task.done():
            await worker_task
            raise CheckoutUnavailable('Worker stopped before Telegram startup')
        await startup_task
        poller_task = asyncio.create_task(dispatcher.start_polling(
            bot, handle_signals=False, close_bot_session=False,
            allowed_updates=dispatcher.resolve_used_update_types()))
        stopper_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({worker_task, poller_task, stopper_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        if worker_task in done:
            await worker_task
            if not stop.is_set():
                raise CheckoutUnavailable('Crypto worker stopped unexpectedly')
        if poller_task in done:
            await poller_task
    except CheckoutUnavailable:
        raise
    except Exception as exc:
        raise RuntimeError('Crypto Telegram runtime failed: ' + type(exc).__name__) from None
    finally:
        stop.set()
        for task in (startup_task, poller_task, stopper_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (startup_task, poller_task, stopper_task)
                               if task is not None), return_exceptions=True)
        if worker_task is not None:
            await asyncio.gather(worker_task, return_exceptions=True)
        try:
            await bot.session.close()
        except Exception as exc:
            logging.getLogger(__name__).warning('Telegram session close deferred: %s', type(exc).__name__)
        finally:
            for logger in filtered_loggers:
                logger.removeFilter(token_filter)

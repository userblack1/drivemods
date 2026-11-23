import asyncio
from contextlib import suppress
from html import escape
import logging
from pathlib import Path
import re
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery)

from .config import Settings
from .models import DeliveryRejected, OrderError, Quote, State, is_demo_order
from .api import make_bot
from .operator_orders import OperatorOrders
from .operator_ui import accept_license, register_operator_handlers
from .service import Service


STATE_LABELS = {
    State.UPLOADED: "Файл получен", State.QUOTED: "Ожидает выбора пакета",
    State.INVOICE: "Ожидает оплаты", State.CHECKOUT: "Оплата обрабатывается",
    State.PAID: "Оплачен, ожидает покупки", State.BUYING: "Заказ обрабатывается",
    State.WAITING: "Лицензия оформляется в DriveMods", State.REVIEW: "Проверяется поддержкой",
    State.READY: "Лицензия готова", State.SENDING: "Отправляем файл",
    State.DELIVERED: "Файл отправлен", State.FAILED: "Не удалось проверить файл",
    State.REFUNDING: "Возврат обрабатывается", State.REFUNDED: "Платёж возвращён",
    State.OPERATOR_PAYMENT: "Оператор согласует или проверяет оплату",
    State.OPERATOR_PAID: "Оплата проверена оператором, лицензия оформляется",
    State.OPERATOR_FILE: "Оператор проверяет готовую лицензию",
    State.CANCELLED: "Заявка отменена оператором",
}

UNVERIFIED_RECEIPT = (
    "Чек или скриншот не подтверждает зачисление и не меняет статус заказа. "
    "Оплата подтверждается по данным платёжной системы. "
    "Проверьте /orders или обратитесь в /paysupport."
)


def usd(cents: int) -> str:
    return f"{cents / 100:g} $"


def order_summary(order: dict) -> str:
    return ("🧪 Тестовая заявка\n" if is_demo_order(order) else "") + f"Заказ <code>{order['id']}</code>\n{STATE_LABELS[order['state']]}" + (
        f"\nПакет: {order['plan'].upper()} · {usd(order['retail_cents'])}" if order["plan"] else "")


def parse_parts(value: str, prefix: str) -> tuple[str, str]:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != prefix or not re.fullmatch(r"[0-9a-f]{32}", parts[1]) or parts[2] not in {"full", "eco"}:
        raise OrderError("Некорректный счёт или кнопка.")
    return parts[1], parts[2]


class TelegramMessenger:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot, self.settings = bot, settings

    async def delivered(self, order: dict, path: Path, demo: bool) -> str:
        caption = ("ТЕСТОВЫЙ ФАЙЛ. Это не лицензия; не устанавливайте в автомобиль."
                   if demo else "Ваша лицензия готова. Сохраните исходное имя device.license.bin.")
        try:
            message = await self.bot.send_document(order["chat_id"], FSInputFile(path, filename="DEMO-NOT-A-LICENSE.txt" if demo else "device.license.bin"),
                                                   caption=caption + f"\nЗаказ <code>{order['id']}</code>")
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise DeliveryRejected("Telegram отклонил отправку; нужна проверка диалога покупателя") from exc
        if not message.document:
            raise RuntimeError("Telegram did not return a document receipt")
        return message.document.file_id

    async def notify_admins(self, text: str) -> None:
        for admin_id in self.settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, escape(text))
            except TelegramAPIError:
                logging.getLogger(__name__).warning("Admin notification delivery failed")

    async def notified(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id, escape(text))

    async def payment_qr(self, chat_id: int, text: str, path: Path, payment_url: str) -> None:
        await self.bot.send_document(chat_id, FSInputFile(path, filename="payment-qr.png"), caption=escape(text),
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                         InlineKeyboardButton(text="Открыть оплату в банке", url=payment_url)]]))


def make_router(service: Service, messenger: TelegramMessenger) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    settings, db = service.settings, service.db
    operator = OperatorOrders(settings, db)
    register_operator_handlers(router, service, messenger, operator)

    def admin(user_id: int) -> bool:
        return user_id in settings.admin_ids

    def terms() -> str:
        if settings.mode == "operator":
            return ("<b>Заказ с оператором</b>\nОтправьте ID своего устройства и выберите пакет. "
                    "Оператор согласует сумму и способ оплаты, проверит зачисление, оформит лицензию в DriveMods "
                    "и проверит её соответствие устройству и пакету. Бот доставит подготовленный файл. "
                    "Автоматическое подтверждение банковских платежей и закупка с карты сейчас не подключены. "
                    "Чек сам по себе не подтверждает оплату.\n"
                    "FULL — 100 USD, ECO — 70 USD; сумма в сомах согласуется до оплаты.\n"
                    "Поддержка: " + escape(settings.support) + ". Укажите номер заказа.")
        return ("<b>Условия заказа</b>\n"
                "Вы загружаете файл ID своего устройства для покупки лицензии DriveMods. "
                "После подтверждённой оплаты файл будет передан в DriveMods для проверки совместимости и оформления лицензии.\n"
                "FULL: русский интерфейс и приложения. ECO: русификация без приложений. "
                "Доступность выбранного пакета подтверждает магазин после оплаты. Если пакет недоступен, обратитесь в поддержку для возврата.\n"
                "Проверьте файл и пакет до оплаты: лицензия оформляется для конкретного устройства. "
                "При задержке или ошибке оплаты обратитесь в поддержку с номером заказа.\n"
                "Оплата внутри Telegram производится в Stars. Сумма в USD — ориентир тарифа; "
                "стоимость покупки Stars зависит от Telegram и способа пополнения.\n"
                "Для вопросов и возвратов: " + escape(settings.support or "контакт владельца ещё не настроен") + ". "
                "Поддержку покупки оказывает продавец этого бота.")

    async def show_offers(message: Message, order: dict) -> None:
        quote = Quote.loads(order["quote_json"])
        description = "<b>Выберите пакет</b>\n" + (
            ("ID принят. После выбора придёт QR MBANK с готовой суммой в сомах. "
             "Оператор проверит зачисление и оформит лицензию.\n\n" if settings.mbank_qr_enabled else
             "ID принят. Оператор согласует оплату и оформит лицензию после проверки зачисления.\n\n")
            if settings.mode == "operator" else "Файл принят локально. В DriveMods он будет отправлен после оплаты.\n\n")
        buttons = []
        for offer in quote.offers:
            contents = "Русификация и приложения" if offer.plan == "full" else "Русификация без приложений"
            description += f"<b>{offer.plan.upper()} — {usd(offer.retail_cents)}</b>\n{contents}\n"
            if settings.mode == "live":
                description += f"К оплате: {offer.stars} ⭐\n"
            description += "\n"
            buttons.append([InlineKeyboardButton(text=f"Принять условия · {offer.plan.upper()} · {usd(offer.retail_cents)}",
                                                 callback_data=f"choose:{order['id']}:{offer.plan}")])
        description += "Перед оплатой прочитайте /terms. Нажимая кнопку пакета, вы принимаете условия заказа."
        if settings.mode == "demo":
            description = "🧪 <b>ТЕСТОВЫЙ РЕЖИМ — без списаний и настоящих лицензий</b>\n\n" + description
        await message.answer(description, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.message(CommandStart())
    async def start(message: Message):
        text = (f"<b>{escape(settings.bot_name)}</b>\n\n"
                "Отправьте исходный файл <b>device.id</b> или <b>device.id.bin</b> из автомобиля. "
                "Выберите пакет и оплатите счёт. После оплаты я отправлю ID в DriveMods для оформления лицензии.\n\n"
                f"FULL — {usd(settings.full_cost + settings.markup)} · ECO — {usd(settings.eco_cost + settings.markup)}\n"
                "/orders — ваши заказы\n/paymentinfo — цены и способы оплаты\n/terms — условия\n/support — поддержка\n/myid — ваш Telegram ID")
        if settings.mode == "demo":
            text = "🧪 <b>Тестовый режим.</b> Покупки доступны только владельцу, списаний нет.\n\n" + text
        elif settings.mode == "operator":
            payment_text = ("После выбора придёт QR MBANK с готовой суммой. Оператор проверит зачисление и оформит лицензию. "
                            if settings.mbank_qr_enabled else "Оператор согласует оплату, проверит зачисление и оформит лицензию. ")
            prices = (f"FULL — {settings.mbank_qr_full_kgs_minor / 100:g} сом · ECO — {settings.mbank_qr_eco_kgs_minor / 100:g} сом\n"
                      if settings.mbank_qr_enabled and settings.mbank_qr_full_kgs_minor and settings.mbank_qr_eco_kgs_minor else
                      "FULL — 100 USD · ECO — 70 USD\nСумма в сомах согласуется до оплаты.\n")
            text = (f"<b>{escape(settings.bot_name)}</b>\n\nОтправьте <b>device.id</b> или <b>device.id.bin</b> "
                    f"и выберите FULL или ECO. {payment_text}Готовый файл придёт в этот диалог.\n\n{prices}"
                    "/orders — заявки\n/terms — условия\n/support — оператор")
        await message.answer(text)

    @router.message(Command("myid"))
    async def myid(message: Message):
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("terms"))
    async def show_terms(message: Message):
        await message.answer(terms())

    @router.message(Command("support", "paysupport"))
    async def support(message: Message):
        await message.answer("Поддержка заказов: " + escape(settings.support or "контакт пока не настроен") +
                             ". Укажите номер заказа. Вопросы об оплате и возврате решает продавец этого бота.")

    @router.message(Command("paymentinfo"))
    async def payment_info(message: Message):
        if settings.mode == "operator":
            if settings.mbank_qr_enabled:
                prices = (f"FULL — {settings.mbank_qr_full_kgs_minor / 100:g} сом\nECO — {settings.mbank_qr_eco_kgs_minor / 100:g} сом\n\n"
                          if settings.mbank_qr_full_kgs_minor and settings.mbank_qr_eco_kgs_minor else "FULL — 100 USD\nECO — 70 USD\n\n")
                await message.answer("<b>Оплата по QR MBANK</b>\n" + prices +
                                     "После выбора пакета придёт QR с готовой суммой в сомах. "
                                     "Проверьте получателя и сумму в своём банке до перевода. "
                                     "Зачисление проверяет оператор; чек сам по себе не подтверждает оплату.\n/orders — статус, /paysupport — поддержка.")
                return
            await message.answer("<b>Оплата через оператора</b>\nFULL — 100 USD\nECO — 70 USD\n\n"
                                 "После выбора пакета оператор согласует точную сумму и способ оплаты. "
                                 "Зачисление проверяет оператор в банке. Автоматического списания здесь нет.\n"
                                 "Чек не меняет статус заявки. /orders — статус, /paysupport — поддержка.")
            return
        await message.answer(
            "<b>Оплата лицензии</b>\n"
            f"FULL — {usd(settings.full_cost + settings.markup)}\n"
            f"ECO — {usd(settings.eco_cost + settings.markup)}\n\n"
            "Оплата через MBANK и O!Bank пока не подключена. Банковские QR-счета сейчас не выставляются.\n"
            "Сумма в сомах должна быть указана в конкретном счёте до оплаты.\n"
            "Статус заказа: /orders. Поддержка оплаты: /paysupport."
        )

    @router.message(Command("orders"))
    async def orders(message: Message):
        rows = db.recent(message.from_user.id)
        await message.answer("\n\n".join(order_summary(o) for o in rows) or "Пока нет заказов.")

    @router.message(Command("pay"))
    async def payment_qr(message: Message):
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise OrderError("Используйте /pay ID_ЗАКАЗА. Номер заявки: /orders")
            order = operator.customer_qr(parts[1], message.from_user.id)
            await messenger.payment_qr(message.chat.id, operator.qr_caption(order), Path(order["qr_path"]), order["qr_payload"])
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("admin"))
    async def admin_orders(message: Message):
        if not admin(message.from_user.id):
            return
        rows = db.recent(limit=10)
        text = "\n\n".join(order_summary(o) for o in rows) or "Пока нет заказов."
        await message.answer(text + ("\n\n/ops — инструкция оператору\n/status — состояние\n/resend ID — повторить доставку" if settings.mode == "operator" else "\n\n/status — состояние бота и размещение\n/reconcile ID — сверить покупку без повторного списания\n/refund ID — возврат до покупки у поставщика\n/resend ID — повторная отправка готового файла"))

    @router.message(Command("status"))
    async def status(message: Message):
        if not admin(message.from_user.id):
            return
        uploads = settings.mode == "live" and settings.store_uploads_enabled
        purchases = settings.mode == "live" and settings.store_purchases_enabled
        mode_label = {"demo": "тестовый", "live": "автоматический", "operator": "с оператором"}[settings.mode]
        payment_label = ("Автозакупки: отключены; зачисления проверяет оператор" if settings.mode == "operator"
                         else f"Реальные оплаты: {'включены' if purchases else 'отключены'}")
        await message.answer(
            f"<b>{escape(settings.bot_name)}</b>\n"
            f"Размещение: {escape(settings.instance_label)}\n"
            f"Режим: {mode_label}\n"
            f"Отправка ID в DriveMods: {'включена' if uploads else 'отключена'}\n"
            f"{payment_label}\n"
            "MBANK / O!Bank: автоподтверждение не подключено\n"
            f"Покупатель: FULL {usd(settings.full_cost + settings.markup)} / ECO {usd(settings.eco_cost + settings.markup)}\n"
            f"Закупка: FULL {usd(settings.full_cost)} / ECO {usd(settings.eco_cost)}"
        )

    @router.message(Command("reconcile"))
    async def reconcile(message: Message):
        if not admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Используйте /reconcile ID_заказа")
            return
        try:
            await service.reconcile(parts[1], messenger)
            await message.answer("Состояние заказа сверено. Повторная покупка не выполнялась.")
        except OrderError as exc:
            await message.answer(escape(str(exc)))
        except Exception:
            await message.answer("Сверка недоступна. Проверьте доступ к DriveMods; покупка не повторялась.")

    @router.message(Command("resend"))
    async def resend(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Используйте /resend ID_заказа")
            return
        try:
            order = db.get(parts[1], None if admin(message.from_user.id) else message.from_user.id)
            if settings.mode == "operator" and admin(message.from_user.id) and order["state"] == State.REVIEW:
                changed = operator.retry_delivery(order["id"], message.from_user.id)
                await message.answer("Отправка сохранённой лицензии восстановлена. Покупать повторно не нужно."
                                     if changed else "Отправка уже выполняется.")
                return
            if order["state"] != State.DELIVERED:
                raise OrderError("Готовой отправленной лицензии для этого заказа пока нет.")
            output = Path(order["output_path"])
            if order.get("output_sha256"):
                import hashlib
                if hashlib.sha256(output.read_bytes()).hexdigest() != order["output_sha256"]:
                    raise OrderError("Файл изменился после проверки. Повторная отправка заблокирована.")
            await messenger.delivered(order, output, is_demo_order(order))
            if order["chat_id"] != message.chat.id:
                await message.answer("Файл повторно отправлен покупателю.")
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("refund"))
    async def refund(message: Message):
        if not admin(message.from_user.id) or settings.mode != "live":
            return
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise OrderError("Используйте /refund ID_заказа")
            order = db.get(parts[1])
            if order["state"] != State.PAID or order["supplier_ref"]:
                raise OrderError("Автовозврат доступен до начала покупки у поставщика. После начала покупки нужна сверка.")
            await message.answer(f"Вернуть {order['stars']} ⭐ покупателю по заказу <code>{order['id']}</code>?",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить возврат", callback_data="refund:" + order["id"])]]))
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.callback_query(F.data.startswith("refund:"))
    async def confirm_refund(callback: CallbackQuery):
        if not admin(callback.from_user.id) or settings.mode != "live":
            await callback.answer("Недоступно", show_alert=True)
            return
        order_id = callback.data.split(":", 1)[1]
        try:
            order = db.get(order_id)
            if order["supplier_ref"] or not db.update(order_id, State.REFUNDING, (State.PAID,)):
                raise OrderError("Покупка уже началась. Сначала выполните сверку.")
            await callback.answer("Выполняю возврат")
            try:
                await messenger.bot.refund_star_payment(order["user_id"], order["charge_id"])
            except Exception:
                db.update(order_id, State.REVIEW, (State.REFUNDING,), last_error="Неопределённый результат возврата. Нужна проверка платежа Telegram.")
                await callback.message.answer("Ответ Telegram о возврате не получен. Проверьте платёж; покупка заблокирована.")
                return
            db.update(order_id, State.REFUNDED, (State.REFUNDING,))
            db.resolve_receipt(order["charge_id"], "refunded")
            await messenger.bot.send_message(order["chat_id"], f"Возврат по заказу <code>{order_id}</code> выполнен.")
        except OrderError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.message(F.photo)
    async def receipt_photo(message: Message):
        await message.answer(UNVERIFIED_RECEIPT)

    @router.message(F.document)
    async def document(message: Message):
        caption_parts = (message.caption or "").split(maxsplit=1)
        if caption_parts and caption_parts[0].split("@")[0] == "/license":
            await accept_license(message, service, operator)
            return
        doc = message.document
        suffix = Path(doc.file_name or "").suffix.lower()
        if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".webp"} or (doc.mime_type or "").startswith("image/") or doc.mime_type == "application/pdf":
            await message.answer(UNVERIFIED_RECEIPT)
            return
        if settings.mode == "demo" and not admin(message.from_user.id):
            await message.answer("Бот пока тестируется. Доступ к тестовым заказам есть у владельца.")
            return
        if not doc.file_name or Path(doc.file_name).suffix.lower() not in {".bin", ".id"}:
            await message.answer("Отправьте исходный device.id или device.id.bin как файл.")
            return
        if not doc.file_size or doc.file_size > settings.max_upload:
            await message.answer(f"Файл должен быть непустым и не больше {settings.max_upload // 1024} КБ.")
            return
        order_id, path = service.allocate()
        await message.answer("Принимаю файл и проверяю его размер…")
        try:
            await messenger.bot.download(doc, destination=path)
            order, created = await service.upload(order_id, message.from_user.id, message.chat.id, doc.file_name, path)
            if not created:
                # This request's extra copy has no order/payment attached to it.
                path.unlink(missing_ok=True)
                path.parent.rmdir()
                await message.answer("Этот файл уже загружен.\n" + order_summary(order))
            if order["state"] == State.QUOTED:
                await show_offers(message, order)
        except OrderError as exc:
            await message.answer(escape(str(exc)))
        except Exception as exc:
            await message.answer("Проверка файла сейчас недоступна. Оплата не запрашивалась. Попробуйте позже или обратитесь в поддержку.")
            await messenger.notify_admins(f"Ошибка проверки файла {order_id}: {type(exc).__name__}")

    @router.callback_query(F.data.startswith("choose:"))
    async def choose(callback: CallbackQuery):
        try:
            if settings.mode == "demo" and not admin(callback.from_user.id):
                raise OrderError("Тестовый заказ доступен только владельцу.")
            order_id, plan = parse_parts(callback.data, "choose")
            if settings.mode == "operator":
                changed = operator.select(order_id, callback.from_user.id, plan)
                await callback.answer("Заявка передана оператору" if changed else "Пакет уже выбран. Статус: /orders")
                return
            order = db.choose(order_id, callback.from_user.id, plan)
            await callback.answer()
            if settings.mode == "demo":
                await callback.message.answer("🧪 Тест без оплаты. Настоящая лицензия не покупается.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Имитировать оплату (владелец)", callback_data=f"testpay:{order_id}:{plan}")]]))
                return
            await messenger.bot.send_invoice(
                chat_id=order["chat_id"], title=f"{settings.bot_name} · {plan.upper()}",
                description=f"Лицензия для загруженного устройства. Тариф {usd(order['retail_cents'])}. Счёт в Telegram Stars.",
                payload=f"dm:{order_id}:{plan}", provider_token="", currency="XTR",
                prices=[LabeledPrice(label=plan.upper(), amount=order["stars"])], start_parameter="order_" + order_id,
            )
        except OrderError as exc:
            await callback.answer(str(exc), show_alert=True)
        except TelegramAPIError:
            await callback.message.answer("Не удалось отправить счёт. Нажмите кнопку выбранного пакета ещё раз; списания не было.")

    @router.callback_query(F.data.startswith("testpay:"))
    async def testpay(callback: CallbackQuery):
        if settings.mode != "demo" or not admin(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        try:
            order_id, plan = parse_parts(callback.data, "testpay")
            order = db.get(order_id, callback.from_user.id)
            if order["plan"] != plan:
                raise OrderError("Пакет не совпадает.")
            changed = db.paid(order_id, callback.from_user.id, "XTR", order["stars"], "demo-" + order_id)
            if not changed:
                await callback.answer("Тестовая оплата уже обработана. Статус: /orders")
                return
            await callback.answer("Тестовая оплата подтверждена")
            await callback.message.answer("🧪 Имитирую заказ. В результате будет тестовый текстовый файл, не лицензия.")
        except OrderError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.pre_checkout_query()
    async def precheckout(query: PreCheckoutQuery):
        try:
            if settings.mode != "live":
                raise OrderError("Реальные платежи в тестовом режиме отключены.")
            order_id, plan = parse_parts(query.invoice_payload, "dm")
            order = db.get(order_id, query.from_user.id)
            if order["plan"] != plan:
                raise OrderError("Пакет не совпадает со счётом.")
            db.precheckout(order_id, query.from_user.id, query.currency, query.total_amount, query.id)
            await query.answer(ok=True)
        except OrderError as exc:
            await query.answer(ok=False, error_message=str(exc))

    @router.message(F.successful_payment)
    async def payment(message: Message):
        payment = message.successful_payment
        receipt_state = db.record_receipt(payment.telegram_payment_charge_id, message.from_user.id,
                                          payment.invoice_payload, payment.currency, payment.total_amount)
        if receipt_state in {"refunded", "refund_review"}:
            return
        try:
            if settings.mode != "live":
                raise OrderError("Неожиданный реальный платёж в тестовом режиме.")
            order_id, plan = parse_parts(payment.invoice_payload, "dm")
            order = db.get(order_id, message.from_user.id)
            if order["plan"] != plan:
                raise OrderError("Пакет не совпадает со счётом.")
            changed = db.paid(order_id, message.from_user.id, payment.currency, payment.total_amount, payment.telegram_payment_charge_id)
            db.resolve_receipt(payment.telegram_payment_charge_id, "matched")
            if changed:
                await messenger.notify_admins(
                    f"Подтверждена оплата Telegram Stars\nЗаказ: {order_id}\n"
                    f"Покупатель: {message.from_user.id}\nПакет: {plan.upper()}\n"
                    f"Получено: {payment.total_amount} XTR\n"
                    f"Розничный тариф: {usd(order['retail_cents'])}\n"
                    f"Закупка DriveMods: {usd(order['cost_cents'])}\n"
                    f"Операция: {payment.telegram_payment_charge_id}"
                )
                await message.answer(f"Оплата получена. Оформляю лицензию.\nЗаказ <code>{order_id}</code>\nСтатус: /orders")
        except OrderError:
            # A valid Telegram receipt that cannot be matched must never buy a license.
            # Unmatched or second distinct payments are returned to their actual payer.
            try:
                await messenger.bot.refund_star_payment(message.from_user.id, payment.telegram_payment_charge_id)
                db.resolve_receipt(payment.telegram_payment_charge_id, "refunded")
                await message.answer("Платёж не удалось связать с заказом. Stars возвращены; обратитесь в поддержку.")
            except TelegramAPIError:
                db.resolve_receipt(payment.telegram_payment_charge_id, "refund_review")
                await message.answer("Платёж получен, но требует проверки поддержкой. Лицензия повторно не покупалась.")
                await messenger.notify_admins(f"Нужна проверка платежа Telegram пользователя {message.from_user.id}. Charge: {payment.telegram_payment_charge_id}")

    @router.message(F.text)
    async def other_text(message: Message):
        await message.answer("Для заказа отправьте device.id или device.id.bin как файл. "
                             "Текущие заявки: /orders. Оплата и поддержка: /paysupport. "
                             + ("Команды оператора: /ops." if admin(message.from_user.id) and settings.mode == "operator" else ""))

    @router.errors()
    async def handler_error(event: ErrorEvent):
        logging.getLogger(__name__).error("Telegram handler failed: %s", type(event.exception).__name__)
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if message is not None:
            try:
                await message.answer("Не удалось завершить действие. Сохранённый заказ не теряется. "
                                     "Проверьте /orders; оператор может продолжить через /ops и /review ID_ЗАКАЗА.")
            except TelegramAPIError:
                pass
        return True

    return router


async def run_bot(service: Service) -> None:
    bot = make_bot(service.settings.bot_token)
    messenger = TelegramMessenger(bot, service.settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(service, messenger))
    worker = None
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Заказать лицензию"),
            BotCommand(command="orders", description="Мои заказы"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="paysupport", description="Помощь с оплатой"),
            BotCommand(command="paymentinfo", description="Способы оплаты и цены"),
            BotCommand(command="terms", description="Условия покупки"),
            BotCommand(command="myid", description="Мой Telegram ID"),
        ])
        service.db.recover()
        worker = asyncio.create_task(service.worker(messenger))
        # Do not drop pending payment updates or delete the existing webhook silently.
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False, handle_as_tasks=False)
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()
        await service.supplier.close()


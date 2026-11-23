"""Private operator controls, with explicit bank and supplier confirmation."""
from html import escape
import hashlib
from pathlib import Path
from uuid import uuid4

from aiogram import F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .config import cents
from .models import OrderError, State
from .operator_orders import review_token


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


async def show_license_review(message, order):
    order_id = order["id"]
    token = review_token(order, "license")
    await message.answer(
        f"<b>Проверка перед отправкой</b>\nЗаказ: <code>{order_id}</code>\n"
        f"Покупатель: {order['user_id']}\nПакет: {order['plan'].upper()}\n"
        f"ID лицензии DriveMods: {escape(order['store_license_reference'])}\n"
        f"Оплата поставщику: {escape(order['store_payment_reference'])}\n"
        f"Закупка: не выше {order['cost_cents'] / 100:g} USD\n"
        f"SHA256 исходного ID: <code>{order['sha256']}</code>\n\n"
        "Подтвердите в DriveMods, что лицензия относится именно к этому устройству и пакету, "
        "а указанный платёж оплачен в USD в пределах лимита. Бот не проверяет банк и кабинет автоматически.",
        reply_markup=buttons([[("Проверил устройство, пакет и оплату — отправить", f"opfile:{order_id}:{token}")],
                              [("Отменить подготовленный файл", f"opdropfile:{order_id}:{token}")]]))


async def show_payment_review(message, order, settings):
    order_id = order["id"]
    token = review_token(order, "payment")
    net = (f"Чистое зачисление по этой операции: {order['net_usd_cents'] / 100:.2f} USD\n"
           if order.get("net_usd_cents") is not None else "")
    if order.get("received_amount_minor") is not None:
        net = f"Чистое зачисление по этой операции: {order['received_amount_minor'] / 100:.2f} {order['received_currency']}\n"
    currency = "KGS" if order.get("qr_payload") else "USD"
    receiver = f"Счёт получателя: {escape(settings.receiving_bank)} · {currency}\n" if settings.receiving_bank else ""
    await message.answer(
        f"<b>Проверка зачисления в банке</b>\nЗаказ: <code>{order_id}</code>\n"
        f"Покупатель: {order['user_id']}\nПакет: {order['plan'].upper()}\n"
        f"Должно поступить: {order['amount_minor'] / 100:.2f} {order['currency']}\n"
        f"{receiver}{net}"
        f"Операция: {escape(order['bank_reference'])}\n\n"
        "Проверьте в своём банке фактическое зачисление, получателя, сумму и валюту. "
        "Проверьте именно эту операцию по выписке, а не общий баланс карты. "
        "Скриншот, SMS или сообщение клиента этого не подтверждают.",
        reply_markup=buttons([[("Зачисление в банке проверено — подтвердить", f"oppay:{order_id}:{token}")],
                              [("Отменить подтверждение", f"opdroppay:{order_id}:{token}")]]))


async def accept_license(message, service, ops):
    if message.from_user.id not in service.settings.admin_ids or service.settings.mode != "operator":
        await message.answer("Передавать готовые лицензии может только оператор.")
        return
    try:
        parts = (message.caption or "").split()
        if len(parts) != 4 or parts[0].split("@")[0] != "/license":
            raise OrderError("Прикрепите файл .bin с подписью /license ID_ЗАКАЗА ID_ЛИЦЕНЗИИ DM-НОМЕР_ОПЛАТЫ")
        _, order_id, reference, payment = parts
        order = ops.get(order_id, message.from_user.id)
        doc = message.document
        if (doc.mime_type or "").startswith("image/") or doc.mime_type == "application/pdf":
            raise OrderError("Изображение или PDF не является файлом лицензии.")
        if not doc.file_name or Path(doc.file_name).suffix.lower() != ".bin" or not doc.file_size or doc.file_size > service.settings.max_license:
            raise OrderError("Нужен непустой файл лицензии .bin допустимого размера.")
        path = service.order_dir / order_id / ("operator-" + uuid4().hex + ".bin")
        await message.bot.download(doc, destination=path)
        order = ops.stage_license(order_id, message.from_user.id, reference, payment, path)
        await show_license_review(message, order)
    except (OrderError, ValueError) as exc:
        await message.answer(escape(str(exc)))


def register_operator_handlers(router, service, messenger, ops):
    def allowed(user_id):
        return service.settings.mode == "operator" and user_id in service.settings.admin_ids

    @router.message(Command("ops"))
    async def help_operator(message):
        if not allowed(message.from_user.id):
            await message.answer("Команды оператора доступны владельцу. Статус вашей заявки: /orders")
            return
        credit_example = ("/paid ID MBANK ID_ОПЕРАЦИИ ЧИСТАЯ_СУММА_KGS" if service.settings.mbank_qr_enabled else
                          "/paid ID OBANK ID_ОПЕРАЦИИ ЧИСТАЯ_СУММА_USD" if service.settings.require_usd_net_credit else
                          "/paid ID MBANK ID_ОПЕРАЦИИ или /paid ID OBANK ID_ОПЕРАЦИИ")
        qr_guide = ("Для QR MBANK точная сумма уже зафиксирована в заявке в KGS; укажите фактически зачисленную сумму в сомах.\n"
                    if service.settings.mbank_qr_enabled else
                    "Для FULL должно быть зачислено чистыми 100 USD, для ECO — 70 USD; комиссии и конвертацию проверьте в выписке.\n")
        billing_guide = ("2. QR с суммой создаётся после выбора пакета. Клиент может повторить показ командой /pay ID.\n"
                         if service.settings.mbank_qr_enabled else
                         "2. Передайте реквизиты: FULL — /bill ID USD 100.00 реквизиты и получатель; "
                         "ECO — /bill ID USD 70.00 реквизиты и получатель. "
                         "Для KGS сначала согласуйте точную сумму, затем /bill ID KGS СУММА реквизиты и получатель.\n")
        await message.answer(
            "<b>Работа оператора LICENCE</b>\n"
            "1. /admin — новые заявки. /device ID — получить исходный ID.\n"
            f"{billing_guide}"
            "3. Проверьте зачисление в банке: получатель, сумма, валюта, ID операции. "
            f"{credit_example}. Подтвердите кнопкой.\n"
            f"{qr_guide}"
            "4. Оформите и оплатите лицензию в DriveMods: FULL не дороже 90 USD / ECO 60 USD.\n"
            "5. Прикрепите скачанную лицензию .bin с подписью:\n"
            "<code>/license ID_ЗАКАЗА ID_ЛИЦЕНЗИИ DM-НОМЕР_ОПЛАТЫ</code>\n"
            "6. Проверьте устройство, пакет и оплату поставщику, затем нажмите кнопку отправки.\n"
            "/resend ID — повторная доставка готового файла; после ошибки восстановит отправку без новой покупки.\n"
            "/review ID — восстановить кнопки проверки после сбоя или перезапуска.\n"
            "/cancelorder ID причина — отменить неоплаченную заявку.\n"
            "Чеки клиентов не засчитываются как оплата автоматически. Тестовые заказы в этот режим не переводятся.")

    @router.message(Command("device"))
    async def device(message):
        if not allowed(message.from_user.id):
            return
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise OrderError("Используйте /device ID_ЗАКАЗА")
            order = ops.get(parts[1], message.from_user.id)
            path = Path(order["source_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != order["sha256"]:
                raise OrderError("Исходный ID изменён. Отправка заблокирована.")
            await messenger.bot.send_document(message.chat.id, FSInputFile(path, filename="device.id.bin"),
                                              caption=f"Исходный ID\nЗаказ <code>{order['id']}</code>\nПакет: {order['plan'] or 'ещё не выбран'}")
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("bill"))
    async def bill(message):
        if not allowed(message.from_user.id):
            return
        try:
            parts = (message.text or "").split(maxsplit=4)
            if len(parts) != 5:
                raise OrderError("Используйте /bill ID_ЗАКАЗА USD 100.00 реквизиты и получатель для FULL; 70.00 для ECO. Для сомов укажите KGS и согласованную сумму.")
            _, order_id, currency, amount, details = parts
            changed = ops.bill(order_id, message.from_user.id, currency.upper(), cents(amount), details)
            await message.answer("Условия оплаты поставлены в очередь отправки клиенту." if changed else "Эти условия уже отправлялись; повторный счёт не создаётся.")
        except (OrderError, ValueError) as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("paid"))
    async def paid(message):
        if not allowed(message.from_user.id):
            return
        try:
            parts = (message.text or "").split()
            if len(parts) not in {4, 5}:
                raise OrderError("Используйте /paid ID_ЗАКАЗА БАНК ID_ОПЕРАЦИИ ЧИСТАЯ_СУММА. Для QR MBANK сумма в KGS; для остальных заявок — в USD.")
            _, order_id, bank, reference = parts[:4]
            amount = cents(parts[4]) if len(parts) == 5 else None
            existing = ops.get(order_id, message.from_user.id)
            if existing.get("qr_payload"):
                order = ops.propose_payment(order_id, message.from_user.id, bank, reference,
                                            received_amount_minor=amount, received_currency="KGS")
            else:
                order = ops.propose_payment(order_id, message.from_user.id, bank, reference, amount)
            await show_payment_review(message, order, service.settings)
        except (OrderError, ValueError) as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("review"))
    async def review(message):
        if not allowed(message.from_user.id):
            return
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise OrderError("Используйте /review ID_ЗАКАЗА")
            order_id = parts[1]
            order = ops.get(order_id, message.from_user.id)
            if order["state"] == State.OPERATOR_FILE:
                await show_license_review(message, order)
            elif order["state"] == State.OPERATOR_PAYMENT and order["bank_reference"]:
                await show_payment_review(message, order, service.settings)
            elif order["state"] == State.REVIEW and order["approved_by"]:
                await message.answer(f"Заказ <code>{order_id}</code>\nЛицензия проверена, доставка требует повтора.\n"
                                     f"Причина: {escape(order['last_error'] or 'ошибка доставки')}\n"
                                     f"Проверьте, что клиент открыл диалог с ботом, затем /resend {order_id}. "
                                     "Повторная покупка не требуется.")
            else:
                await message.answer(f"Заказ <code>{order['id']}</code>\nПакет: {order['plan'] or 'ещё не выбран'}\n"
                                     "Сейчас нет неподтверждённого платежа или файла. /admin — статус, /ops — следующий шаг.")
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.message(Command("cancelorder"))
    async def cancel_order(message):
        if not allowed(message.from_user.id):
            return
        try:
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) != 3:
                raise OrderError("Используйте /cancelorder ID_ЗАКАЗА причина")
            changed = ops.cancel_unpaid(parts[1], message.from_user.id, parts[2])
            await message.answer("Неоплаченная заявка отменена, клиенту отправится уведомление." if changed else "Заявка уже отменена.")
        except OrderError as exc:
            await message.answer(escape(str(exc)))

    @router.callback_query(F.data.regexp(r"^op(pay|droppay|file|dropfile):[0-9a-f]{32}(:[0-9a-f]{12})?$"))
    async def confirm(callback):
        if not allowed(callback.from_user.id):
            await callback.answer("Только для оператора", show_alert=True)
            return
        try:
            parts = callback.data.split(":")
            kind, order_id = parts[:2]
            if len(parts) != 3:
                raise OrderError("Кнопка устарела. Откройте /review " + order_id)
            token = parts[2]
            if kind in {"oppay", "opdroppay"}:
                changed = ops.confirm_payment(order_id, callback.from_user.id, cancel=kind == "opdroppay", expected_token=token)
                text = "Подтверждение отменено" if kind == "opdroppay" else "Зачисление подтверждено оператором"
            else:
                changed = ops.approve_license(order_id, callback.from_user.id, cancel=kind == "opdropfile", expected_token=token)
                text = "Файл отменён" if kind == "opdropfile" else "Лицензия поставлена в очередь отправки"
            await callback.answer(text if changed else "Уже обработано; повторной отправки нет")
            if changed:
                await callback.message.answer(text + f".\nЗаказ <code>{order_id}</code>\nСтатус: /admin")
        except (OrderError, ValueError) as exc:
            await callback.answer(str(exc), show_alert=True)

"""Audited fulfillment by an operator. This module has no bank/store client."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .config import Settings
from .models import OrderError, Quote, State
from .purchase_policy import check_purchase_budget
from .storage import Database
from .supplier import validate_license


def review_token(order, kind):
    """Bind a Telegram confirmation to the exact candidate shown to the owner."""
    fields = ("bank_reference", "candidate_by", "net_usd_cents", "received_amount_minor", "received_currency", "currency", "amount_minor") if kind == "payment" else (
        "store_license_reference", "store_payment_reference", "staged_by", "output_sha256", "sha256", "plan", "cost_cents")
    body = json.dumps([kind, order["id"], *[order.get(key) for key in fields]], separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


class OperatorOrders:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        with db.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS operator_orders (
                order_id TEXT PRIMARY KEY, currency TEXT, amount_minor INTEGER, details TEXT,
                bank TEXT, bank_reference TEXT UNIQUE, candidate_by INTEGER,
                confirmed_by INTEGER, confirmed_at REAL,
                store_license_reference TEXT UNIQUE, store_payment_reference TEXT UNIQUE,
                staged_by INTEGER, approved_by INTEGER, approved_at REAL
            )""")
            columns = {r["name"] for r in con.execute("PRAGMA table_info(operator_orders)")}
            if "net_usd_cents" not in columns:
                con.execute("ALTER TABLE operator_orders ADD COLUMN net_usd_cents INTEGER")
            for name in ("qr_payload", "qr_path", "qr_sha256", "qr_rate"):
                if name not in columns:
                    con.execute(f"ALTER TABLE operator_orders ADD COLUMN {name} TEXT")
            for name, definition in {"received_amount_minor": "INTEGER", "received_currency": "TEXT"}.items():
                if name not in columns:
                    con.execute(f"ALTER TABLE operator_orders ADD COLUMN {name} {definition}")

    def _enabled(self):
        if self.settings.mode != "operator":
            raise OrderError("Режим оператора не включён.")

    def _admin(self, user_id):
        self._enabled()
        if user_id not in self.settings.admin_ids:
            raise OrderError("Команда доступна только оператору.")

    def _row(self, con, order_id):
        row = con.execute("""SELECT o.*, p.currency, p.amount_minor, p.details, p.bank,
            p.bank_reference, p.candidate_by, p.confirmed_by, p.confirmed_at,
            p.store_license_reference, p.store_payment_reference, p.staged_by, p.approved_by, p.net_usd_cents,
            p.qr_payload, p.qr_path, p.qr_sha256, p.qr_rate, p.received_amount_minor, p.received_currency
            FROM orders o LEFT JOIN operator_orders p ON p.order_id=o.id WHERE o.id=?""", (order_id,)).fetchone()
        if not row or row["workflow"] != "operator":
            raise OrderError("Заказ оператора не найден. Тестовые заказы здесь не обрабатываются.")
        return dict(row)

    def get(self, order_id, admin_id):
        self._admin(admin_id)
        with self.db.connect() as con:
            return self._row(con, order_id)

    def _admins(self, con, order_id, kind, body):
        for admin in self.settings.admin_ids:
            self.db._notice(con, f"op:{order_id}:{kind}:{admin}", admin, body)

    def select(self, order_id, user_id, plan):
        self._enabled()
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["user_id"] != user_id:
                raise OrderError("Это не ваш заказ.")
            if order["state"] == State.OPERATOR_PAYMENT and order["plan"] == plan:
                return False
            if order["state"] != State.QUOTED:
                raise OrderError("Пакет уже выбран. Для изменения обратитесь к оператору.")
            quote = Quote.loads(order["quote_json"])
            if quote.expires_at <= time.time():
                raise OrderError("Предложение истекло. Отправьте тот же ID ещё раз для обновления.")
            offer = next((o for o in quote.offers if o.plan == plan), None)
            if not offer:
                raise OrderError("Пакет недоступен.")
            check_purchase_budget(plan, offer.cost_cents)
            con.execute("UPDATE orders SET state=?,plan=?,cost_cents=?,retail_cents=?,stars=0,terms_at=?,updated_at=? WHERE id=?",
                        (State.OPERATOR_PAYMENT, plan, offer.cost_cents, offer.retail_cents, time.time(), time.time(), order_id))
            con.execute("INSERT INTO operator_orders(order_id) VALUES(?)", (order_id,))
            self.db._event(con, order_id, "operator_order_requested")
            self.db._notice(con, f"op:{order_id}:requested", order["chat_id"],
                            f"Заявка принята оператором.\nЗаказ: {order_id}\nПакет: {plan.upper()} · {offer.retail_cents / 100:g} USD\n"
                            + ("Сейчас придёт QR MBANK с готовой суммой в сомах. Зачисление проверяет оператор.\nСтатус: /orders"
                               if self.settings.mbank_qr_enabled else "Оператор согласует точную сумму и способ оплаты. Сейчас деньги не списываются.\nСтатус: /orders"))
            if self.settings.mbank_qr_enabled:
                self._issue_qr(con, self._row(con, order_id))
            qr_order = self._row(con, order_id)
            payment_step = (f"QR с готовой суммой уже подготовлен: {qr_order['amount_minor'] / 100:.2f} KGS. Получатель MBANK.\n"
                            f"Сверьте поступление по выписке: /paid {order_id} MBANK ID_ОПЕРАЦИИ {qr_order['amount_minor'] / 100:.2f}\n"
                            if self.settings.mbank_qr_enabled else
                            f"Передать реквизиты: /bill {order_id} USD {offer.retail_cents / 100:.2f} реквизиты и получатель\n"
                            "Для оплаты в сомах сначала согласуйте точную сумму.\n")
            self._admins(con, order_id, "new", f"Новая заявка LICENCE\nЗаказ: {order_id}\nПокупатель: {user_id}\n"
                         f"Пакет: {plan.upper()} · {offer.retail_cents / 100:g} USD\nЗакупка: не выше {offer.cost_cents / 100:g} USD\n"
                         f"Получить ID: /device {order_id}\n"
                         + payment_step + "/ops — инструкция оператору")
            return True

    def _issue_qr(self, con, order):
        from .bank_qr import MBankQr
        from .payment_setup import amount_kgs_minor
        try:
            if self.settings.receiving_bank.upper() not in {"MBANK", "МБАНК"}:
                raise ValueError("Получатель QR должен быть настроен как MBANK.")
            if self.settings.mbank_qr_source is None:
                raise ValueError("Для QR нужны реквизиты получателя.")
            fixed = (self.settings.mbank_qr_full_kgs_minor, self.settings.mbank_qr_eco_kgs_minor)
            if any(value is not None for value in fixed):
                if self.settings.mbank_qr_kgs_per_usd is not None or any(type(value) is not int or value <= 0 for value in fixed):
                    raise ValueError("Нужны две положительные фиксированные суммы KGS без одновременного курса.")
                amount = fixed[0 if order["plan"] == "full" else 1]
            else:
                if self.settings.mbank_qr_kgs_per_usd is None:
                    raise ValueError("Нужны согласованные суммы FULL/ECO или курс KGS за USD.")
                amount = amount_kgs_minor(order["retail_cents"], self.settings.mbank_qr_kgs_per_usd)
            if amount > 100_000_000:
                raise ValueError("Сумма QR превышает допустимый размер заказа.")
            qr = MBankQr.read(self.settings.mbank_qr_source).with_amount(amount)
            path = Path(order["source_path"]).parent / "payment-qr.png"
            qr.write_png(path)
        except (OSError, ValueError, ImportError) as exc:
            raise OrderError("Не удалось подготовить QR оплаты. Сумма не выставлена; обратитесь к оператору.") from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        details = f"MBANK · получатель: {qr.recipient}"
        con.execute("""UPDATE operator_orders SET currency='KGS',amount_minor=?,details=?,
                    qr_payload=?,qr_path=?,qr_sha256=?,qr_rate=? WHERE order_id=?""",
                    (amount, details, qr.url(), str(path), digest, self.settings.mbank_qr_kgs_per_usd, order["id"]))
        self.db._event(con, order["id"], "mbank_amount_prefilled_qr_created")
        body = self.qr_caption(self._row(con, order["id"]))
        self.db._notice(con, f"op:{order['id']}:qr", order["chat_id"], body,
                        attachment_path=path, attachment_sha256=digest, payment_url=qr.url())

    @staticmethod
    def qr_caption(order):
        price_note = f"Курс: {order['qr_rate']} KGS за 1 USD\n" if order["qr_rate"] else "Фиксированная сумма для выбранного пакета.\n"
        return (f"QR для оплаты {order['plan'].upper()}\nЗаказ: {order['id']}\n"
                f"Стоимость: {order['retail_cents'] / 100:g} USD\n"
                f"К оплате: {order['amount_minor'] / 100:.2f} KGS\n{price_note}"
                f"{order['details']}\n\nСумма уже записана в QR. Перед переводом проверьте получателя и сумму в банке. "
                "Укажите номер заказа в комментарии, если банк это позволяет. После оплаты сообщите оператору номер заказа. "
                "Зачисление пока проверяет оператор. Чек не подтверждает оплату автоматически.\n"
                f"Повторить показ QR: /pay {order['id']}\nСтатус: /orders")

    def customer_qr(self, order_id, user_id):
        self._enabled()
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["user_id"] != user_id:
                raise OrderError("Это не ваш заказ.")
            if order["state"] != State.OPERATOR_PAYMENT or not order["qr_payload"]:
                raise OrderError("QR для этой заявки сейчас недоступен. Проверьте /orders или обратитесь к оператору.")
            try:
                if hashlib.sha256(Path(order["qr_path"]).read_bytes()).hexdigest() != order["qr_sha256"]:
                    raise ValueError("changed")
            except (OSError, ValueError) as exc:
                raise OrderError("QR изменился или недоступен. Обратитесь к оператору до оплаты.") from exc
            return order

    def bill(self, order_id, admin_id, currency, amount_minor, details):
        self._admin(admin_id)
        if currency not in {"KGS", "USD"} or type(amount_minor) is not int or not 0 < amount_minor <= 100_000_000:
            raise OrderError("Нужна положительная сумма в KGS или USD.")
        if not 5 <= len(details.strip()) <= 1500:
            raise OrderError("Укажите реквизиты и получателя: от 5 до 1500 символов.")
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["state"] != State.OPERATOR_PAYMENT or order["bank_reference"]:
                raise OrderError("Оплата уже проверяется или подтверждена.")
            if currency == "USD" and amount_minor != order["retail_cents"]:
                raise OrderError("Сумма USD должна совпадать с розничной ценой пакета.")
            if order["amount_minor"] is not None:
                if (order["currency"], order["amount_minor"], order["details"]) == (currency, amount_minor, details.strip()):
                    return False
                raise OrderError("Условия оплаты уже переданы клиенту. Нельзя молча менять выставленную сумму.")
            con.execute("UPDATE operator_orders SET currency=?,amount_minor=?,details=? WHERE order_id=?",
                        (currency, amount_minor, details.strip(), order_id))
            self.db._event(con, order_id, f"operator_bill_issued_by:{admin_id}")
            self.db._notice(con, f"op:{order_id}:bill", order["chat_id"],
                            f"Условия оплаты от оператора\nЗаказ: {order_id}\nПакет: {order['plan'].upper()}\n"
                            f"Согласованная сумма: {amount_minor / 100:.2f} {currency}\n\n{details.strip()}\n\n"
                            "Укажите номер заказа в комментарии, если банк это позволяет. "
                            "После оплаты сообщите оператору номер заказа. Чек сам по себе не подтверждает зачисление.\nСтатус: /orders")
            return True

    def cancel_unpaid(self, order_id, admin_id, reason):
        self._admin(admin_id)
        if not 3 <= len(reason.strip()) <= 300:
            raise OrderError("Укажите причину отмены: 3–300 символов.")
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["state"] == State.CANCELLED:
                return False
            if order["state"] not in {State.QUOTED, State.OPERATOR_PAYMENT} or order["bank_reference"] or order["confirmed_at"]:
                raise OrderError("Можно отменить только неоплаченную заявку без подготовленного подтверждения банка.")
            con.execute("UPDATE orders SET state=?,last_error=?,updated_at=? WHERE id=?", (State.CANCELLED, reason.strip(), time.time(), order_id))
            con.execute("UPDATE notification_outbox SET state='cancelled' WHERE id IN (?,?) AND state='pending'",
                        (f"op:{order_id}:qr", f"op:{order_id}:bill"))
            self.db._event(con, order_id, f"operator_unpaid_order_cancelled_by:{admin_id}")
            self.db._notice(con, f"op:{order_id}:cancelled", order["chat_id"],
                            f"Заявка отменена оператором.\nЗаказ: {order_id}\nПричина: {reason.strip()}\n"
                            "Если вы уже переводили деньги, обратитесь к оператору для сверки. Автоматического возврата здесь нет.")
            return True

    def _check_net_credit(self, order, net_usd_cents):
        if self.settings.require_usd_net_credit and net_usd_cents is None:
            raise OrderError("Укажите фактически зачисленную чистую сумму в USD: /paid ID БАНК ID_ОПЕРАЦИИ СУММА_USD")
        if net_usd_cents is not None and (type(net_usd_cents) is not int or net_usd_cents != order["retail_cents"]):
            raise OrderError(f"Чистое зачисление должно составлять {order['retail_cents'] / 100:.2f} USD. Проверьте недоплату, переплату и конвертацию.")

    def _check_credit(self, order, net_usd_cents, received_amount_minor=None, received_currency=None):
        if order.get("qr_payload"):
            if received_amount_minor is None:
                raise OrderError("Укажите фактически зачисленную чистую сумму KGS из выписки MBANK: /paid ID MBANK ID_ОПЕРАЦИИ СУММА_KGS")
            if (net_usd_cents is not None or received_currency != "KGS" or type(received_amount_minor) is not int
                    or received_amount_minor != order["amount_minor"]):
                raise OrderError(f"Чистое зачисление должно составлять ровно {order['amount_minor'] / 100:.2f} KGS. Проверьте сумму и валюту.")
        else:
            if received_amount_minor is not None or received_currency is not None:
                raise OrderError("Для этого заказа подтверждается чистое зачисление USD.")
            self._check_net_credit(order, net_usd_cents)

    def propose_payment(self, order_id, admin_id, bank, reference, net_usd_cents=None, *, received_amount_minor=None, received_currency=None):
        self._admin(admin_id)
        bank = bank.upper().replace("!", "")
        reference = reference.strip().upper()
        if bank not in {"MBANK", "OBANK"} or not re.fullmatch(r"[A-Za-z0-9._:/-]{4,100}", reference):
            raise OrderError("Используйте MBANK или OBANK и настоящий ID операции банка (4–100 символов, без пробелов).")
        key = f"{bank}:{reference}"
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["qr_payload"] and bank != "MBANK":
                raise OrderError("Для этого QR проверяйте входящую операцию в MBANK; банк отправителя не используется.")
            if order["state"] != State.OPERATOR_PAYMENT or not order["amount_minor"]:
                raise OrderError("Сначала согласуйте сумму командой /bill; подтверждать можно только неоплаченный заказ.")
            self._check_credit(order, net_usd_cents, received_amount_minor, received_currency)
            if order["bank_reference"]:
                if (order["bank_reference"] == key and order["candidate_by"] == admin_id and order["net_usd_cents"] == net_usd_cents
                        and order["received_amount_minor"] == received_amount_minor and order["received_currency"] == received_currency):
                    return order
                raise OrderError("Уже подготовлено другое подтверждение. Сначала отмените его кнопкой.")
            try:
                con.execute("UPDATE operator_orders SET bank=?,bank_reference=?,candidate_by=?,net_usd_cents=?,received_amount_minor=?,received_currency=? WHERE order_id=?",
                            (bank, key, admin_id, net_usd_cents, received_amount_minor, received_currency, order_id))
            except sqlite3.IntegrityError as exc:
                raise OrderError("Эта банковская операция уже привязана к другому заказу.") from exc
            self.db._event(con, order_id, f"operator_payment_proposed_by:{admin_id}")
            return self._row(con, order_id)

    def confirm_payment(self, order_id, admin_id, *, cancel=False, expected_token=None):
        self._admin(admin_id)
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if expected_token is not None and not secrets.compare_digest(expected_token, review_token(order, "payment")):
                raise OrderError("Кнопка относится к другому подтверждению. Откройте /review " + order_id)
            if order["confirmed_at"] and not cancel:
                return False
            if order["state"] != State.OPERATOR_PAYMENT or not order["bank_reference"] or order["candidate_by"] != admin_id:
                raise OrderError("Подтверждение недоступно или подготовлено другим оператором.")
            if cancel:
                con.execute("UPDATE operator_orders SET bank=NULL,bank_reference=NULL,candidate_by=NULL,net_usd_cents=NULL,received_amount_minor=NULL,received_currency=NULL WHERE order_id=?", (order_id,))
                self.db._event(con, order_id, "operator_payment_candidate_cancelled")
                return True
            self._check_credit(order, order["net_usd_cents"], order["received_amount_minor"], order["received_currency"])
            con.execute("UPDATE operator_orders SET confirmed_by=?,confirmed_at=? WHERE order_id=?", (admin_id, time.time(), order_id))
            con.execute("UPDATE orders SET state=?,updated_at=? WHERE id=?", (State.OPERATOR_PAID, time.time(), order_id))
            self.db._event(con, order_id, f"operator_bank_payment_verified_by:{admin_id}")
            self.db._notice(con, f"op:{order_id}:paid", order["chat_id"],
                            f"Оператор подтвердил зачисление оплаты.\nЗаказ: {order_id}\nПакет: {order['plan'].upper()}\n"
                            "Оператор оформляет лицензию для вашего устройства. Статус: /orders")
            net_note = f"Чистыми по выписке: {order['net_usd_cents'] / 100:.2f} USD\n" if order["net_usd_cents"] is not None else ""
            if order["received_amount_minor"] is not None:
                net_note = f"Чистыми по выписке: {order['received_amount_minor'] / 100:.2f} {order['received_currency']}\n"
            self._admins(con, order_id, "paid", f"Зачисление проверено оператором {admin_id}\nЗаказ: {order_id}\n"
                         f"Сумма: {order['amount_minor'] / 100:.2f} {order['currency']}\nОперация: {order['bank_reference']}\n"
                         f"{net_note}"
                         f"Оформите {order['plan'].upper()} в DriveMods не дороже {order['cost_cents'] / 100:g} USD.\n"
                         f"ID: /device {order_id}\nПришлите файл лицензии с подписью:\n/license {order_id} ID_ЛИЦЕНЗИИ DM-НОМЕР_ОПЛАТЫ")
            return True

    def stage_license(self, order_id, admin_id, license_reference, payment_reference, path: Path):
        self._admin(admin_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", license_reference) or not re.fullmatch(r"DM-[A-Z0-9]{6,24}", payment_reference):
            raise OrderError("Укажите ID лицензии DriveMods и номер её оплаты вида DM-XXXXXX.")
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["state"] != State.OPERATOR_PAID or not order["confirmed_at"]:
                raise OrderError("Лицензия принимается только после проверки зачисления оператором.")
            self._check_credit(order, order["net_usd_cents"], order["received_amount_minor"], order["received_currency"])
            check_purchase_budget(order["plan"], order["cost_cents"])
            if hashlib.sha256(Path(order["source_path"]).read_bytes()).hexdigest() != order["sha256"]:
                raise OrderError("Исходный ID изменился. Передача лицензии заблокирована.")
            content = path.read_bytes()
            validate_license(content, order["sha256"], self.settings)
            try:
                con.execute("UPDATE operator_orders SET store_license_reference=?,store_payment_reference=?,staged_by=? WHERE order_id=?",
                            (license_reference, payment_reference, admin_id, order_id))
                con.execute("UPDATE orders SET state=?,supplier_ref=?,output_path=?,output_sha256=?,updated_at=? WHERE id=?",
                            (State.OPERATOR_FILE, license_reference, str(path), hashlib.sha256(content).hexdigest(), time.time(), order_id))
            except sqlite3.IntegrityError as exc:
                raise OrderError("Лицензия или оплата DriveMods уже используются в другом заказе.") from exc
            self.db._event(con, order_id, f"operator_license_staged_by:{admin_id}")
            return self._row(con, order_id)

    def approve_license(self, order_id, admin_id, *, cancel=False, expected_token=None):
        self._admin(admin_id)
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if expected_token is not None and not secrets.compare_digest(expected_token, review_token(order, "license")):
                raise OrderError("Кнопка относится к другому файлу. Откройте /review " + order_id)
            if order["approved_by"] and not cancel:
                return False
            if order["state"] != State.OPERATOR_FILE or order["staged_by"] != admin_id:
                raise OrderError("Файл не подготовлен или загружен другим оператором.")
            if cancel:
                con.execute("UPDATE operator_orders SET store_license_reference=NULL,store_payment_reference=NULL,staged_by=NULL WHERE order_id=?", (order_id,))
                con.execute("UPDATE orders SET state=?,supplier_ref=NULL,output_path=NULL,output_sha256=NULL,updated_at=? WHERE id=?",
                            (State.OPERATOR_PAID, time.time(), order_id))
                self.db._event(con, order_id, "operator_license_candidate_cancelled")
                return True
            self._check_credit(order, order["net_usd_cents"], order["received_amount_minor"], order["received_currency"])
            check_purchase_budget(order["plan"], order["cost_cents"])
            content = Path(order["output_path"]).read_bytes()
            validate_license(content, order["sha256"], self.settings)
            if hashlib.sha256(content).hexdigest() != order["output_sha256"]:
                raise OrderError("Файл изменился после загрузки. Отправка заблокирована.")
            if hashlib.sha256(Path(order["source_path"]).read_bytes()).hexdigest() != order["sha256"]:
                raise OrderError("Исходный ID изменился. Отправка заблокирована.")
            con.execute("UPDATE operator_orders SET approved_by=?,approved_at=? WHERE order_id=?", (admin_id, time.time(), order_id))
            con.execute("UPDATE orders SET state=?,attempts=0,next_attempt=0,last_error=NULL,updated_at=? WHERE id=?", (State.READY, time.time(), order_id))
            self.db._event(con, order_id, f"operator_license_and_supplier_payment_verified_by:{admin_id}")
            return True

    def retry_delivery(self, order_id, admin_id):
        """Resume an approved file after delivery failure, without any purchase."""
        self._admin(admin_id)
        with self.db.connect() as con:
            order = self._row(con, order_id)
            if order["state"] in {State.READY, State.SENDING} and order["approved_by"]:
                return False
            if order["state"] != State.REVIEW or not order["confirmed_at"] or not order["approved_by"]:
                raise OrderError("Восстановление доступно только для проверенной лицензии после ошибки доставки.")
            if not order["store_license_reference"] or not order["store_payment_reference"]:
                raise OrderError("Нет подтверждённой лицензии или оплаты DriveMods.")
            self._check_credit(order, order["net_usd_cents"], order["received_amount_minor"], order["received_currency"])
            check_purchase_budget(order["plan"], order["cost_cents"])
            try:
                source = Path(order["source_path"]).read_bytes()
                content = Path(order["output_path"]).read_bytes()
            except (OSError, TypeError) as exc:
                raise OrderError("Файл заказа недоступен. Нужна проверка оператором; повторная покупка не требуется.") from exc
            if hashlib.sha256(source).hexdigest() != order["sha256"] or hashlib.sha256(content).hexdigest() != order["output_sha256"]:
                raise OrderError("Файл заказа изменился после проверки. Повторная отправка заблокирована.")
            validate_license(content, order["sha256"], self.settings)
            con.execute("UPDATE orders SET state=?,attempts=0,next_attempt=0,last_error=NULL,updated_at=? WHERE id=?",
                        (State.READY, time.time(), order_id))
            self.db._event(con, order_id, f"operator_delivery_retry_by:{admin_id}")
            return True

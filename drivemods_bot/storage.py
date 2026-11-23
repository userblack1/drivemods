from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from .models import OrderError, Quote, State


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    filename TEXT NOT NULL, source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, quote_json TEXT, plan TEXT, cost_cents INTEGER,
                    retail_cents INTEGER, stars INTEGER, terms_at REAL,
                    checkout_id TEXT, checkout_until REAL, charge_id TEXT UNIQUE,
                    supplier_ref TEXT UNIQUE, action_url TEXT, output_path TEXT,
                    telegram_file_id TEXT, last_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS orders_work ON orders(state,next_attempt);
                CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS orders_device_user ON orders(user_id,sha256)
                    WHERE state NOT IN ('failed','refunded');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, order_id TEXT NOT NULL,
                    event TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL, currency TEXT NOT NULL, amount INTEGER NOT NULL,
                    resolution TEXT NOT NULL DEFAULT 'received', created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
                );
            """)
            columns = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
            for name, definition in {"workflow": "TEXT NOT NULL DEFAULT 'legacy'", "output_sha256": "TEXT"}.items():
                if name not in columns:
                    con.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
            notice_columns = {r["name"] for r in con.execute("PRAGMA table_info(notification_outbox)")}
            for name in ("attachment_path", "attachment_sha256", "payment_url"):
                if name not in notice_columns:
                    con.execute(f"ALTER TABLE notification_outbox ADD COLUMN {name} TEXT")
            index_sql = con.execute("SELECT sql FROM sqlite_master WHERE name='orders_device_user'").fetchone()[0]
            if "'cancelled'" not in index_sql:
                con.execute("DROP INDEX orders_device_user")
                con.execute("CREATE UNIQUE INDEX orders_device_user ON orders(user_id,sha256) WHERE state NOT IN ('failed','refunded','cancelled')")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _event(self, con, order_id: str, event: str) -> None:
        con.execute("INSERT INTO events(order_id,event,created_at) VALUES(?,?,?)", (order_id, event, time.time()))

    def get(self, order_id: str, user_id: int | None = None) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or (user_id is not None and row["user_id"] != user_id):
                raise OrderError("Заказ не найден.")
            return dict(row)

    def recent(self, user_id: int | None = None, limit: int = 10) -> list[dict]:
        with self.connect() as con:
            if user_id is None:
                rows = con.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
            else:
                rows = con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
            return [dict(r) for r in rows]

    def add(self, order_id: str, user_id: int, chat_id: int, filename: str, path: Path, digest: str, workflow: str = "legacy") -> tuple[dict, bool]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE user_id=? AND sha256=? AND state NOT IN ('failed','refunded','cancelled')", (user_id, digest)).fetchone()
            if row:
                return dict(row), False
            now = time.time()
            con.execute("INSERT INTO orders(id,user_id,chat_id,filename,source_path,sha256,state,created_at,updated_at,workflow) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (order_id, user_id, chat_id, filename, str(path), digest, State.UPLOADED, now, now, workflow))
            self._event(con, order_id, "uploaded")
        return self.get(order_id), True

    def update(self, order_id: str, state: State, expected: tuple[State, ...] | None = None, **fields) -> bool:
        allowed = {"quote_json", "plan", "cost_cents", "retail_cents", "stars", "terms_at", "supplier_ref", "action_url", "output_path", "output_sha256", "telegram_file_id", "last_error", "attempts", "next_attempt"}
        if set(fields) - allowed:
            raise ValueError("Unknown order fields")
        with self.connect() as con:
            params = [state, time.time(), *fields.values(), order_id]
            query = "UPDATE orders SET state=?,updated_at=?" + "".join(f",{name}=?" for name in fields) + " WHERE id=?"
            if expected:
                query += " AND state IN (" + ",".join("?" for _ in expected) + ")"
                params.extend(expected)
            changed = con.execute(query, params).rowcount == 1
            if changed:
                self._event(con, order_id, state)
            return changed

    def choose(self, order_id: str, user_id: int, plan: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
            if not row or row["state"] not in (State.QUOTED, State.INVOICE):
                raise OrderError("Для этого заказа уже нельзя изменить пакет.")
            if row["state"] == State.INVOICE:
                if row["plan"] == plan:
                    return dict(row)
                raise OrderError("Пакет уже выбран. Для изменения обратитесь в поддержку.")
            quote = Quote.loads(row["quote_json"])
            if quote.expires_at < time.time():
                raise OrderError("Предложение истекло. Загрузите файл заново или обратитесь в поддержку.")
            offer = next((o for o in quote.offers if o.plan == plan), None)
            if not offer:
                raise OrderError("Этот пакет недоступен для вашего устройства.")
            con.execute("UPDATE orders SET state=?,plan=?,cost_cents=?,retail_cents=?,stars=?,terms_at=?,updated_at=? WHERE id=?",
                        (State.INVOICE, plan, offer.cost_cents, offer.retail_cents, offer.stars, time.time(), time.time(), order_id))
            self._event(con, order_id, "invoice_prepared")
        return self.get(order_id)

    def precheckout(self, order_id: str, user_id: int, currency: str, amount: int, checkout_id: str) -> None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row["user_id"] != user_id or currency != "XTR" or amount != row["stars"] or amount <= 0:
                raise OrderError("Счёт не соответствует заказу.")
            if not row["terms_at"] or Quote.loads(row["quote_json"]).expires_at < time.time():
                raise OrderError("Счёт истёк. Обратитесь в поддержку.")
            valid = row["state"] == State.INVOICE or (row["state"] == State.CHECKOUT and (row["checkout_id"] == checkout_id or row["checkout_until"] < time.time()))
            if not valid:
                raise OrderError("Этот заказ уже оплачивается или оплачен.")
            con.execute("UPDATE orders SET state=?,checkout_id=?,checkout_until=?,updated_at=? WHERE id=?",
                        (State.CHECKOUT, checkout_id, time.time() + 180, time.time(), order_id))

    def paid(self, order_id: str, user_id: int, currency: str, amount: int, charge: str) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row["user_id"] != user_id or currency != "XTR" or amount != row["stars"] or amount <= 0 or not row["terms_at"] or not charge:
                raise OrderError("Платёж не соответствует заказу; нужна проверка владельцем.")
            if row["charge_id"] == charge:
                return False
            if row["charge_id"] or row["state"] not in (State.INVOICE, State.CHECKOUT):
                raise OrderError("Повторный платёж требует проверки владельцем.")
            try:
                con.execute("UPDATE orders SET state=?,charge_id=?,updated_at=? WHERE id=?", (State.PAID, charge, time.time(), order_id))
            except sqlite3.IntegrityError as exc:
                raise OrderError("Этот платёж уже привязан к другому заказу.") from exc
            self._event(con, order_id, "payment_confirmed")
            return True

    def record_receipt(self, charge: str, user_id: int, payload: str, currency: str, amount: int) -> str:
        with self.connect() as con:
            row = con.execute("SELECT * FROM receipts WHERE charge_id=?", (charge,)).fetchone()
            if row:
                if (row["user_id"], row["payload"], row["currency"], row["amount"]) != (user_id, payload, currency, amount):
                    raise OrderError("Конфликт данных платёжной квитанции.")
                return row["resolution"]
            con.execute("INSERT INTO receipts(charge_id,user_id,payload,currency,amount,created_at) VALUES(?,?,?,?,?,?)", (charge, user_id, payload, currency, amount, time.time()))
            return "received"

    def resolve_receipt(self, charge: str, resolution: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE receipts SET resolution=? WHERE charge_id=?", (resolution, charge))

    def has_confirmed_payment(self, order: dict) -> bool:
        """Recheck the matched provider receipt before any real store mutation.

        Only Telegram Stars is connected to this receipt format. Bank screenshots,
        supplier payments and demo charges cannot authorize a live purchase.
        """
        charge = order.get("charge_id")
        if not charge or charge.startswith("demo-"):
            return False
        with self.connect() as con:
            row = con.execute("SELECT * FROM receipts WHERE charge_id=?", (charge,)).fetchone()
            return bool(row and row["resolution"] == "matched"
                        and row["user_id"] == order["user_id"]
                        and row["payload"] == f"dm:{order['id']}:{order['plan']}"
                        and row["currency"] == "XTR"
                        and type(order.get("stars")) is int
                        and row["amount"] == order["stars"] > 0)

    def claim(self, before: State, after: State) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM orders WHERE state=? AND next_attempt<=? ORDER BY created_at LIMIT 1", (before, time.time())).fetchone()
            if not row:
                return None
            con.execute("UPDATE orders SET state=?,updated_at=? WHERE id=?", (after, time.time(), row["id"]))
            self._event(con, row["id"], after)
            return {**dict(row), "state": after}

    def _notice(self, con, key: str, chat_id: int, body: str, *, attachment_path=None, attachment_sha256=None, payment_url=None) -> None:
        con.execute("""INSERT OR IGNORE INTO notification_outbox
                    (id,chat_id,body,attachment_path,attachment_sha256,payment_url) VALUES(?,?,?,?,?,?)""",
                    (key, chat_id, body, str(attachment_path) if attachment_path else None, attachment_sha256, payment_url))

    def claim_notice(self) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM notification_outbox WHERE state='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            con.execute("UPDATE notification_outbox SET state='sending' WHERE id=?", (row["id"],))
            return dict(row)

    def finish_notice(self, key: str, error: str | None = None, *, skipped: bool = False) -> None:
        with self.connect() as con:
            if error is None:
                con.execute("UPDATE notification_outbox SET state=?,last_error=NULL WHERE id=?", ("skipped" if skipped else "sent", key))
            else:
                row = con.execute("SELECT attempts FROM notification_outbox WHERE id=?", (key,)).fetchone()
                attempt = row["attempts"] + 1
                con.execute("UPDATE notification_outbox SET state=?,attempts=?,next_attempt=?,last_error=? WHERE id=?",
                            ("review" if attempt >= 10 else "pending", attempt, time.time() + min(5 * 2 ** attempt, 300), error, key))

    def recover(self) -> None:
        with self.connect() as con:
            con.execute("UPDATE notification_outbox SET state='pending' WHERE state='sending'")
            # A crash during purchase may have charged the supplier. Never submit it again.
            for row in con.execute("SELECT id FROM orders WHERE state=?", (State.BUYING,)).fetchall():
                con.execute("UPDATE orders SET state=?,last_error=?,updated_at=? WHERE id=?", (State.REVIEW, "Перезапуск во время покупки: требуется сверка с DriveMods.", time.time(), row["id"]))
                self._event(con, row["id"], "purchase_interrupted_review")
            # Re-sending a document can duplicate delivery but cannot repeat a purchase.
            con.execute("UPDATE orders SET state=? WHERE state=?", (State.READY, State.SENDING))
            con.execute("UPDATE orders SET state=?,last_error=? WHERE state=?", (State.REVIEW, "Перезапуск во время возврата. Проверьте платёж Telegram.", State.REFUNDING))


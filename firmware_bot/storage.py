import sqlite3
import asyncio
import json
import time

from .versions import SoftwareVersion


class QueueError(ValueError):
    pass


class Database:
    def __init__(self, path):
        self.changed = asyncio.Event()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, query TEXT NOT NULL, family TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                detail TEXT NOT NULL DEFAULT '', notified INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id)
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(requests)")}
        with self.connection:
            if "variant" not in columns:
                self.connection.execute("ALTER TABLE requests ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
            if "variants" not in columns:
                self.connection.execute("ALTER TABLE requests ADD COLUMN variants TEXT NOT NULL DEFAULT '[]'")
            self.connection.execute("CREATE INDEX IF NOT EXISTS requests_state_order ON requests(state,updated,id)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS requests_user_state ON requests(user_id,state)")

    def enqueue(self, user_id, chat_id, message_id, version: SoftwareVersion, max_queue: int, cooldown: float, *, variants=()):
        now = time.time()
        with self.connection:
            existing = self.connection.execute("SELECT id FROM requests WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()
            if existing:
                return existing["id"], False
            active = self.connection.execute("SELECT id,query,variant FROM requests WHERE user_id=? AND state IN ('queued','searching','delivering')", (user_id,)).fetchone()
            if active:
                if active["query"] == version.query and (not version.variant or active["variant"] == version.variant):
                    return active["id"], False
                raise QueueError(f"Сначала дождитесь ответа на запрос #{active['id']}: {active['query']}. Статус: /request.")
            replacing_choice = self.connection.execute("SELECT 1 FROM requests WHERE user_id=? AND state='awaiting_variant'", (user_id,)).fetchone()
            recent = self.connection.execute("SELECT MAX(created) FROM requests WHERE user_id=?", (user_id,)).fetchone()[0]
            if not replacing_choice and recent is not None and now - recent < cooldown:
                raise QueueError("Подождите несколько секунд перед следующим запросом.")
            count = self.connection.execute("SELECT COUNT(*) FROM requests WHERE state='queued'").fetchone()[0]
            if not variants and count >= max_queue:
                raise QueueError("Очередь заполнена. Повторите запрос немного позже.")
            self.connection.execute("UPDATE requests SET state='cancelled',notified=1,updated=? WHERE user_id=? AND state='awaiting_variant'", (now, user_id))
            state = "awaiting_variant" if variants else "queued"
            cursor = self.connection.execute("INSERT INTO requests(user_id,chat_id,message_id,query,family,state,created,updated,variant,variants) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                             (user_id, chat_id, message_id, version.query, version.family, state, now, now, version.variant or "", json.dumps(list(variants))))
            self.changed.set()
            return cursor.lastrowid, True

    def select_variant(self, request_id, user_id, chat_id, variant: str, max_queue: int):
        with self.connection:
            request = self.connection.execute("SELECT * FROM requests WHERE id=? AND user_id=? AND chat_id=?", (request_id, user_id, chat_id)).fetchone()
            if not request:
                raise QueueError("Этот выбор недоступен. Отправьте свою версию ПО.")
            if request["state"] != "awaiting_variant":
                raise QueueError("Вариант уже выбран или запрос устарел. Статус: /request.")
            if variant not in {"STD_H", "STD_M"} or variant not in json.loads(request["variants"]):
                raise QueueError("Такого варианта нет в этом запросе.")
            SoftwareVersion(request["query"], request["family"]).with_variant(variant)
            count = self.connection.execute("SELECT COUNT(*) FROM requests WHERE state='queued'").fetchone()[0]
            if count >= max_queue:
                raise QueueError("Очередь заполнена. Нажмите кнопку немного позже.")
            self.connection.execute("UPDATE requests SET state='queued',variant=?,updated=? WHERE id=? AND state='awaiting_variant'", (variant, time.time(), request_id))
            self.changed.set()
            return dict(self.connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def recover(self):
        # Do not resend requests with an uncertain supplier/delivery result.
        with self.connection:
            self.connection.execute("UPDATE requests SET state='failed', detail='Обработка прервана перезапуском. Полный комплект не подтверждён. Отправьте версию ещё раз.', updated=? WHERE state IN ('searching','delivering')", (time.time(),))

    def next_request(self):
        row = self.connection.execute("SELECT * FROM requests WHERE state='queued' ORDER BY updated,id LIMIT 1").fetchone()
        return dict(row) if row else None

    def queued(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM requests WHERE state='queued' ORDER BY updated,id")]

    def get(self, request_id):
        row = self.connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def queue_position(self, request_id):
        request = self.get(request_id)
        if not request or request["state"] != "queued":
            return 0
        waiting = self.connection.execute("SELECT COUNT(*) FROM requests WHERE state='queued' AND (updated < ? OR (updated=? AND id<=?))",
                                          (request["updated"], request["updated"], request_id)).fetchone()[0]
        active = self.connection.execute("SELECT COUNT(*) FROM requests WHERE state IN ('searching','delivering')").fetchone()[0]
        return waiting + active

    def counts(self):
        return {row["state"]: row["n"] for row in self.connection.execute("SELECT state,COUNT(*) AS n FROM requests GROUP BY state")}

    def expire_queued(self, timeout):
        with self.connection:
            self.connection.execute("UPDATE requests SET state='failed',detail='Источник слишком долго недоступен или очередь не успела обработать запрос. Отправьте версию ещё раз или обратитесь в поддержку.',updated=? WHERE state='queued' AND created < ?",
                                    (time.time(), time.time() - timeout))

    def update(self, request_id, state, detail=""):
        with self.connection:
            self.connection.execute("UPDATE requests SET state=?,detail=?,updated=? WHERE id=?", (state, detail, time.time(), request_id))
        self.changed.set()

    def unnotified(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM requests WHERE state IN ('failed','not_found','delivered') AND notified=0 ORDER BY id")]

    def notified(self, request_id):
        with self.connection:
            self.connection.execute("UPDATE requests SET notified=1 WHERE id=?", (request_id,))

    def latest(self, user_id):
        row = self.connection.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.connection.close()

"""One-use owner login to DriveMods; saves a private session, never a password.

No upload, order creation, payment or card operation is available from this form.
The browser posts only to loopback. The helper authenticates with the official
store over verified HTTPS and keeps the session in an owner-restricted file.
"""
import argparse
import asyncio
import hmac
import html
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.connect_crypto import SetupHandler, SetupServer, write_url_file, private_paths
from crypto_checkout.drivemods import DriveModsParseClient


def save_session(root, session):
    _, stage = private_paths(Path(root))
    target = stage / "drivemods-session.json"
    if target.is_symlink():
        raise ValueError("Unsafe session destination")
    # Only explicitly authenticated values, never arbitrary browser state.
    payload = json.dumps({"username": session.username, "user_id": session.user_id,
                          "session_token": session.session_token}, ensure_ascii=False).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".store-session-", suffix=".json", dir=stage)
    pending = Path(name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, target)
    finally:
        if fd >= 0:
            os.close(fd)
        pending.unlink(missing_ok=True)


async def authenticate(username, password):
    async with DriveModsParseClient() as client:
        await client.login(username, password)
        return client.export_session()


class StoreSetupHandler(SetupHandler):
    def do_GET(self):
        if not self._allowed():
            return
        state = self.server.state
        self._reply(200, "Подключение DriveMods", """
<p>Войдите в ваш дилерский аккаунт. Логин и пароль будут переданы только
<strong>store.drivemods.org</strong> по HTTPS. Пароль не сохраняется.</p>
<p>На этом компьютере сохранится защищённая сессия для подключения вашего
сервера. Эта форма не создаёт лицензии и не списывает деньги.</p>
<form method="post" action=""" + '"' + html.escape(state.path, quote=True) + '"' + """ autocomplete="off">
<input type="hidden" name="csrf" value=""" + '"' + html.escape(state.csrf, quote=True) + '"' + """>
<label for="username">Логин DriveMods</label>
<input id="username" name="username" required maxlength="1024" autocomplete="username">
<label for="password">Пароль DriveMods</label>
<input id="password" name="password" type="password" required maxlength="1024" autocomplete="current-password">
<label><input style="width:auto" name="remember" type="checkbox" value="yes" required>
Сохранить сессию для подключения моего бота к DriveMods</label>
<button type="submit">Подключить DriveMods</button></form>
<small>Пароль и коды в чат не отправляйте. Одноразовая форма действует 30 минут.</small>""")

    def do_POST(self):
        if not self._allowed(post=True):
            return
        length = self.headers.get("Content-Length", "")
        if (self.headers.get("Transfer-Encoding") or len(length) > 4 or not length.isascii() or not length.isdecimal()
                or not 0 < int(length) <= 4096
                or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
            self._reply(400, "Запрос отклонён")
            return
        try:
            body = self.rfile.read(int(length))
            self._body_read = True
            fields = parse_qs(body.decode("utf-8", "strict"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=4)
            if (set(fields) != {"csrf", "username", "password", "remember"}
                    or any(len(v) != 1 for v in fields.values())
                    or not hmac.compare_digest(fields["csrf"][0].encode(), self.server.state.csrf.encode())
                    or fields["remember"] != ["yes"]):
                self._reply(403, "Подтвердите сохранение сессии")
                return
            username, password = fields["username"][0], fields["password"][0]
            if not 1 <= len(username) <= 1024 or not 1 <= len(password) <= 1024:
                raise ValueError("Invalid credentials")
            session = self.server.authenticate(username, password)
            save_session(self.server.state.root, session)
        except Exception:
            self._reply(400, "Не удалось подключить DriveMods",
                        "<p>Проверьте логин, пароль и доступность сайта. Значения не отображаются. "
                        "Покупки не выполнялись. Вернитесь назад для повторного входа.</p>")
            return
        self.server.state.completed = True
        self._reply(200, "DriveMods подключён",
                    "<p>Сессия сохранена локально в защищённой папке. Пароль не сохранён. "
                    "Лицензии и платёжные документы не создавались, деньги не списывались.</p>")


class StoreSetupServer(SetupServer):
    def __init__(self, root=ROOT, *, authenticator=None, **kwargs):
        super().__init__(root, **kwargs)
        self.RequestHandlerClass = StoreSetupHandler
        self.page_title = "Локальное подключение DriveMods"
        self.authenticate = authenticator or (lambda username, password: asyncio.run(authenticate(username, password)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--url-file", type=Path)
    args = parser.parse_args()
    if not args.serve:
        parser.print_help()
        return
    server = StoreSetupServer()
    if args.url_file:
        write_url_file(ROOT, args.url_file, server.local_url)
    print(server.local_url, flush=True)
    try:
        server.serve_once_setup()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

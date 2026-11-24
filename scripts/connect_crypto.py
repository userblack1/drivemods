"""One-use loopback form that stores Binance reading credentials locally only.

This helper never authenticates with Binance, changes permissions, or deploys.
Its staging directory must already exist with owner-restricted permissions.
"""
import argparse
from dataclasses import dataclass, field
import hmac
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from io import StringIO

from dotenv.parser import parse_stream


ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("BINANCE_READ_API_KEY", "BINANCE_READ_API_SECRET")
MAX_BODY = 4096
MAX_TTL = 1800


def private_paths(root: Path) -> tuple[Path, Path]:
    root = Path(root).resolve()
    target = root / ".crypto.env"
    stage = root / "data" / "new-license-bot"
    if (not stage.is_dir() or stage.is_symlink()
            or stage.resolve() != stage or target.is_symlink()
            or (target.exists() and not target.is_file())):
        raise ValueError("Protected local configuration paths are unavailable")
    if os.name != "nt" and stage.stat().st_mode & 0o077:
        raise ValueError("Credential staging directory is not private")
    # A tracked file must be rejected even if a matching ignore rule exists.
    result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", ".crypto.env"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise ValueError("Local credential file is not excluded from Git")
    return target, stage


def validate_values(values: dict[str, str]) -> None:
    if set(values) != set(FIELDS):
        raise ValueError("Expected the two Binance reading credential fields")
    # HMAC Binance credentials are single-line ASCII. No dotenv escapes or
    # executable interpolation are accepted from the web form.
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value)
           for value in values.values()):
        raise ValueError("Invalid credential format")


def store_credentials(root: Path, values: dict[str, str]) -> None:
    validate_values(values)
    target, stage = private_paths(root)
    original = target.read_bytes() if target.exists() else None
    text = original.decode("utf-8-sig") if original is not None else ""
    output = []
    written = set()
    for binding in parse_stream(StringIO(text)):
        if binding.error:
            raise ValueError("Existing configuration needs local correction")
        if binding.key in FIELDS:
            key = binding.key
            if key not in written:
                output.append(key + "='" + values[key] + "'\n")
                written.add(key)
        else:
            output.append(binding.original.string)
    if output and not output[-1].endswith(("\n", "\r")):
        output[-1] += "\n"
    for key in FIELDS:
        if key not in written:
            output.append(key + "='" + values[key] + "'\n")
    payload = "".join(output).encode("utf-8")
    descriptor, filename = tempfile.mkstemp(prefix=".crypto-", suffix=".env", dir=stage)
    staged = Path(filename)
    try:
        # On Windows the new file inherits the existing restrictive staging
        # directory's DACL. os.replace moves that same secured file to root.
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        current = target.read_bytes() if target.exists() else None
        if current != original or target.is_symlink():
            raise ValueError("Configuration changed during save; retry locally")
        os.replace(staged, target)
        if os.name != "nt":
            target.chmod(0o600)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        staged.unlink(missing_ok=True)


def write_url_file(root: Path, requested: Path, url: str) -> Path:
    """Publish only the loopback capability URL to a new private metadata file."""
    _, stage = private_paths(root)
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = Path(root).resolve() / candidate
    if (candidate.is_symlink() or candidate.parent.is_symlink()
            or candidate.parent.resolve() != stage
            or candidate.suffix.lower() not in {".txt", ".url"}):
        raise ValueError("URL file must be a new text file in the private staging directory")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_-]{43}", parsed.path)):
        raise ValueError("Expected a one-use loopback URL")
    # Exclusive creation protects every pre-existing file, including env files.
    # The URL file inherits the restricted staging DACL on Windows.
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write((url + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return candidate


@dataclass
class SetupState:
    root: Path
    path: str = field(default_factory=lambda: "/" + secrets.token_urlsafe(32))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    deadline: float = 0
    completed: bool = False


PAGE_STYLE = """<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 20px;color:#17212c}
label{display:block;margin:20px 0 4px}input{box-sizing:border-box;width:100%;padding:12px;font:inherit}
button{margin-top:24px;padding:12px 20px;font:inherit;background:#146c58;color:white;border:0;border-radius:6px}
.note{color:#465764}h1{font-size:26px}small{display:block;margin-top:18px}
</style>"""


class SetupServer(HTTPServer):
    def __init__(self, root: Path = ROOT, *, ttl: int = MAX_TTL, clock=time.monotonic,
                 diagnostics: bool = False):
        if not 1 <= ttl <= MAX_TTL:
            raise ValueError("Setup lifetime must be at most 30 minutes")
        private_paths(root)
        self.clock = clock
        self.diagnostics = diagnostics
        self.state = SetupState(Path(root).resolve(), deadline=clock() + ttl)
        super().__init__(("127.0.0.1", 0), SetupHandler)
        self.timeout = 0.5
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.local_url = self.origin + self.state.path

    def diagnostic(self, stage, handler):
        if not self.diagnostics:
            return
        # Only categorical header metadata: never bodies, field values, paths,
        # capabilities, CSRF tokens, arbitrary external hosts or raw exceptions.
        origin = handler.headers.get("Origin")
        if origin is None:
            origin_label = "missing"
        elif origin == "null":
            origin_label = "null"
        elif origin == self.origin:
            origin_label = "same-origin"
        else:
            origin_label = "other-origin"
        host = handler.headers.get("Host")
        host_label = "same-host" if host == f"127.0.0.1:{self.server_port}" else "other-host"
        fetch_site = handler.headers.get("Sec-Fetch-Site")
        if fetch_site not in {None, "same-origin", "same-site", "cross-site", "none"}:
            fetch_site = "other"
        print(json.dumps({"local_setup_denied": stage, "origin": origin_label,
                          "host": host_label, "sec_fetch_site": fetch_site or "missing"}), flush=True)

    def handle_error(self, request, client_address):
        # BaseServer's default traceback may include request/credential values.
        pass

    def serve_once_setup(self):
        while not self.state.completed and self.clock() < self.state.deadline:
            self.handle_request()


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "LocalSetup"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)
        self._body_read = False

    def log_message(self, format, *args):
        pass

    def _reply(self, status, title, body=""):
        if status >= 400 and self.command == "POST" and not self._body_read:
            # Discard a bounded already-sent form before closing the socket.
            # Otherwise Windows may reset the connection before its browser
            # receives the error response. Never retain or log the discarded data.
            length = self.headers.get("Content-Length", "")
            if len(length) <= 4 and length.isascii() and length.isdecimal():
                count = int(length)
                if 0 < count <= MAX_BODY * 2:
                    try:
                        self.rfile.read(count)
                    except (OSError, socket.timeout):
                        pass
            self._body_read = True
        page = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>" + html.escape(getattr(self.server, "page_title", "Локальное подключение Binance")) + "</title>" + PAGE_STYLE
                + "<main><h1>" + title + "</h1>" + body + "</main></html>").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(page)

    def _allowed(self, *, post=False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != expected_host:
            self.server.diagnostic("host", self)
            self._reply(403, "Доступ запрещён")
            return False
        if self.path != self.server.state.path:
            self.server.diagnostic("path", self)
            self._reply(404, "Страница не найдена")
            return False
        if self.server.state.completed or self.server.clock() >= self.server.state.deadline:
            self.server.diagnostic("expired", self)
            self._reply(410, "Страница настройки больше недоступна")
            return False
        origin = self.headers.get("Origin")
        # The in-app browser's sandbox sends an opaque Origin for its local
        # page, while retaining the browser-controlled same-origin Fetch
        # Metadata header. Accept only that exact combination for POST; the
        # loopback Host, unguessable path and one-use CSRF checks still apply.
        opaque_same_origin = (post and origin == "null"
                              and self.headers.get("Sec-Fetch-Site") == "same-origin")
        if ((post and origin != self.server.origin) or (origin and origin != self.server.origin)) and not opaque_same_origin:
            self.server.diagnostic("origin", self)
            self._reply(403, "Доступ запрещён")
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        state = self.server.state
        body = ("<p class='note'>Форма работает только на этом компьютере. Значения сохраняются "
                "в исключённый из Git файл <code>.crypto.env</code>. Запросов к Binance здесь нет.</p>"
                "<p>Используйте ключ только для чтения. Торговля, переводы и вывод должны быть выключены.</p>"
                "<form method='post' action='" + html.escape(state.path, quote=True) + "' autocomplete='off'>"
                "<input type='hidden' name='csrf' value='" + html.escape(state.csrf, quote=True) + "'>")
        for key, label in ((FIELDS[0], "Binance API Key"), (FIELDS[1], "Binance Secret Key")):
            body += ("<label for='" + key + "'>" + label + "</label><input type='password' id='" + key
                     + "' name='" + key + "' required minlength='32' maxlength='256' autocomplete='new-password' "
                     "spellcheck='false' autocapitalize='none'>")
        body += ("<button type='submit'>Сохранить только локально</button></form>"
                 "<small>Страница одноразовая и действует не более 30 минут. Не отправляйте ключи в Telegram или чат.</small>")
        self._reply(200, "Подключение Binance — локально", body)

    def do_POST(self):
        if not self._allowed(post=True):
            return
        length = self.headers.get("Content-Length", "")
        if (self.headers.get("Transfer-Encoding") or len(length) > 4 or not length.isascii() or not length.isdecimal()
                or not 0 < int(length) <= MAX_BODY):
            self._reply(413, "Запрос отклонён")
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self._reply(415, "Формат запроса не поддерживается")
            return
        try:
            body = self.rfile.read(int(length))
            self._body_read = True
            if len(body) != int(length):
                raise ValueError("Incomplete request")
            fields = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=3)
            if set(fields) != {"csrf", *FIELDS} or any(len(values) != 1 for values in fields.values()):
                raise ValueError("Invalid form fields")
            if not hmac.compare_digest(fields["csrf"][0].encode("utf-8"), self.server.state.csrf.encode("ascii")):
                self.server.diagnostic("csrf", self)
                self._reply(403, "Доступ запрещён")
                return
            values = {key: fields[key][0] for key in FIELDS}
            validate_values(values)
        except (ValueError, UnicodeError, socket.timeout):
            self._reply(400, "Проверьте два поля ключей и повторите ввод")
            return
        try:
            store_credentials(self.server.state.root, values)
        except (OSError, ValueError):
            self._reply(500, "Не удалось сохранить закрытый файл", "<p>Значения не выводятся. Проверьте права локальной папки.</p>")
            return
        self.server.state.completed = True
        self._reply(200, "Ключи сохранены на этом компьютере",
                    "<p>Значения не отображаются и не отправлялись в Telegram или чат. "
                    "Права доступа Binance и производственные службы не изменялись.</p>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Start the one-use local form")
    parser.add_argument("--url-file", type=Path,
                        help="Write the local URL to a NEW .txt/.url file in ignored data/new-license-bot")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Print only denial stage and categorical Origin/Host/Fetch-Site metadata")
    args = parser.parse_args()
    if not args.serve:
        parser.print_help()
        return
    server = None
    try:
        server = SetupServer(diagnostics=args.diagnostics)
        if args.url_file:
            write_url_file(ROOT, args.url_file, server.local_url)
    except (OSError, ValueError):
        if server is not None:
            server.server_close()
        parser.exit(1, "Protected local setup is unavailable; no credentials displayed.\n")
    print(server.local_url, flush=True)
    try:
        server.serve_once_setup()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

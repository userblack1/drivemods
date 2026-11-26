"""Local setup never sends credentials off-host or exposes their values."""
from contextlib import contextmanager
import ctypes
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import urlencode

from dotenv import dotenv_values
import pytest

from scripts.connect_crypto import FIELDS, MAX_BODY, SetupServer, store_credentials, write_url_file


@pytest.fixture
def root(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (tmp_path / ".gitignore").write_text("*.env\n", encoding="utf-8")
    stage = tmp_path / "data" / "new-license-bot"
    stage.mkdir(parents=True, mode=0o700)
    return tmp_path


def generated_values():
    # Generated per test, never a real account credential or a stored fixture.
    return {key: secrets.token_urlsafe(48) for key in FIELDS}


@contextmanager
def serving(root, **kwargs):
    server = SetupServer(root, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(server, method="GET", *, path=None, fields=None, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    if fields is not None:
        body = urlencode(fields)
    actual_headers = {}
    if method == "POST":
        actual_headers.update(Origin=server.origin)
        actual_headers["Content-Type"] = "application/x-www-form-urlencoded"
    actual_headers.update(headers or {})
    try:
        connection.request(method, path or server.state.path, body=body, headers=actual_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
        return result
    finally:
        connection.close()


def form_fields(server, values):
    return {"csrf": server.state.csrf, **values}


def test_atomic_save_preserves_other_configuration_and_multiline_values(root, monkeypatch):
    target = root / ".crypto.env"
    target.write_text("# Kept comment\nCRYPTO_NETWORK=TRON\n"
                      "EXISTING_MULTILINE='first\nBINANCE_READ_API_KEY=not-a-setting\nlast'\n"
                      "BINANCE_READ_API_KEY=\nBINANCE_READ_API_KEY=\n"
                      "CRYPTO_LIVE_PURCHASES_ENABLED=false", encoding="utf-8")
    values = generated_values()
    real_replace = os.replace
    observed = []
    def secure_replace(source, destination):
        staged = Path(source)
        assert staged.parent.resolve() == (root / "data/new-license-bot").resolve()
        assert Path(destination) == target
        if os.name != "nt":
            assert staged.stat().st_mode & 0o777 == 0o600
        observed.append(staged)
        real_replace(source, destination)
    monkeypatch.setattr(os, "replace", secure_replace)
    store_credentials(root, values)
    parsed = dotenv_values(target)
    assert parsed["CRYPTO_NETWORK"] == "TRON"
    assert parsed["CRYPTO_LIVE_PURCHASES_ENABLED"] == "false"
    assert parsed["EXISTING_MULTILINE"] == "first\nBINANCE_READ_API_KEY=not-a-setting\nlast"
    assert all(parsed[key] == values[key] for key in FIELDS)
    assert target.read_text().startswith("# Kept comment\n")
    assert len(observed) == 1 and not observed[0].exists()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_save_failure_leaves_existing_file_and_cleans_staging(root, monkeypatch, capsys):
    target = root / ".crypto.env"
    original = b"CRYPTO_NETWORK=TRON\n"
    target.write_bytes(original)
    values = generated_values()
    def failure(*args):
        raise OSError("simulated filesystem failure")
    monkeypatch.setattr(os, "replace", failure)
    with pytest.raises(OSError):
        store_credentials(root, values)
    assert target.read_bytes() == original
    assert not list((root / "data/new-license-bot").iterdir())
    assert all(value not in str(capsys.readouterr()) for value in values.values())


def test_tracked_env_file_is_rejected_even_with_ignore_rule(root):
    target = root / ".crypto.env"
    target.write_text("CRYPTO_NETWORK=TRON\n")
    subprocess.run(["git", "-C", str(root), "add", "-f", ".crypto.env"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with pytest.raises(ValueError, match="excluded from Git"):
        store_credentials(root, generated_values())


def test_form_is_masked_and_success_is_one_time_without_echo_or_logs(root, capsys):
    values = generated_values()
    with serving(root) as server:
        status, headers, page = request(server)
        assert status == 200
        assert page.count("type='password'") == 2
        assert "no-store" in headers["Cache-Control"]
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert not any(value in page for value in values.values())
        status, headers, saved_page = request(server, "POST", fields=form_fields(server, values))
        assert status == 200 and server.state.completed
        assert all(value not in saved_page for value in values.values())
        assert request(server)[0] == 410
        assert request(server, "POST", fields=form_fields(server, generated_values()))[0] == 410
    assert all(dotenv_values(root / ".crypto.env")[key] == values[key] for key in FIELDS)
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.parametrize("headers", [
    {"Origin": "https://outside.invalid"},
    {"Origin": "null"},
    {"Host": "outside.invalid"},
    {"Origin": ""},
])
def test_cross_site_or_rebound_requests_cannot_save(root, headers):
    with serving(root) as server:
        assert request(server, "POST", fields=form_fields(server, generated_values()), headers=headers)[0] == 403
        assert not server.state.completed
    assert not (root / ".crypto.env").exists()


def test_missing_origin_wrong_capability_and_wrong_csrf_are_rejected(root):
    with serving(root) as server:
        values = generated_values()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = urlencode(form_fields(server, values))
        connection.request("POST", server.state.path, body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
        assert request(server, "POST", path="/", fields=form_fields(server, values))[0] == 404
        fields = form_fields(server, values)
        fields["csrf"] = secrets.token_urlsafe(32)
        assert request(server, "POST", fields=fields)[0] == 403
        fields["csrf"] = "неверный-токен"
        assert request(server, "POST", fields=fields)[0] == 403
    assert not (root / ".crypto.env").exists()


def test_expiration_is_enforced_even_for_correct_fields(root):
    now = [100.0]
    with serving(root, ttl=60, clock=lambda: now[0]) as server:
        now[0] = 160.0
        assert request(server)[0] == 410
        assert request(server, "POST", fields=form_fields(server, generated_values()))[0] == 410
    assert not (root / ".crypto.env").exists()


@pytest.mark.parametrize("kind", ["extra", "duplicate", "invalid_credentials", "oversized", "wrong_type", "chunked"])
def test_malformed_requests_do_not_create_configuration(root, kind):
    with serving(root) as server:
        fields = form_fields(server, generated_values())
        headers = {}
        body = None
        expected = 400
        if kind == "extra":
            fields["unexpected"] = "1"
        elif kind == "duplicate":
            body = urlencode(fields) + "&csrf=" + server.state.csrf
            fields = None
        elif kind == "invalid_credentials":
            fields[FIELDS[0]] = "bad\nformat"
        elif kind == "oversized":
            body = "a" * (MAX_BODY + 1)
            fields, expected = None, 413
        elif kind == "wrong_type":
            headers["Content-Type"], expected = "application/json", 415
        elif kind == "chunked":
            headers["Transfer-Encoding"], expected = "chunked", 413
        assert request(server, "POST", fields=fields, body=body, headers=headers)[0] == expected
        assert not server.state.completed
    assert not (root / ".crypto.env").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation")
def test_windows_move_retains_the_staging_files_dacl(root, monkeypatch):
    from ctypes import wintypes
    get_info = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_info.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_info.restype = wintypes.DWORD
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    def dacl(path):
        acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        result = get_info(str(path), 1, 4, None, None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        assert result == 0 and acl.value
        try:
            header = ctypes.string_at(acl, 4)
            size = int.from_bytes(header[2:4], "little")
            return ctypes.string_at(acl, size)
        finally:
            local_free(descriptor)
    actual_replace = os.replace
    before = []
    def observe_move(source, destination):
        before.append(dacl(source))
        actual_replace(source, destination)
    monkeypatch.setattr(os, "replace", observe_move)
    store_credentials(root, generated_values())
    assert before == [dacl(root / ".crypto.env")]


def test_url_file_is_private_and_never_overwrites_existing_file(root):
    server = SetupServer(root)
    try:
        relative = Path("data/new-license-bot/crypto-setup-url.txt")
        target = write_url_file(root, relative, server.local_url)
        assert target.read_text(encoding="ascii").strip() == server.local_url
        if os.name != "nt":
            assert target.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            write_url_file(root, relative, server.local_url)
        assert target.read_text(encoding="ascii").strip() == server.local_url
        assert not (root / ".crypto.env").exists()
    finally:
        server.server_close()


@pytest.mark.parametrize("requested", ["url.txt", "data/url.txt", "data/new-license-bot/bot.env",
                                       "data/new-license-bot/../url.txt"])
def test_url_file_cannot_leave_private_directory_or_target_configuration(root, requested):
    server = SetupServer(root)
    try:
        with pytest.raises(ValueError):
            write_url_file(root, Path(requested), server.local_url)
    finally:
        server.server_close()


def test_url_file_rejects_a_remote_url(root):
    with pytest.raises(ValueError):
        write_url_file(root, Path("data/new-license-bot/crypto-setup-url.txt"),
                       "https://outside.invalid/" + secrets.token_urlsafe(32))


def test_diagnostics_show_denial_metadata_without_credentials_tokens_or_paths(root, capsys):
    values = generated_values()
    with serving(root, diagnostics=True) as server:
        response = request(server, "POST", fields=form_fields(server, values), headers={"Origin": "null"})
        assert response[0] == 403
        output = capsys.readouterr().out
        metadata = json.loads(output)
        assert metadata == {"local_setup_denied": "origin", "origin": "null",
                            "host": "same-host", "sec_fetch_site": "missing"}
        assert server.state.path not in output and server.state.csrf not in output
        assert all(value not in output for value in values.values())
        assert request(server, path="/not-the-capability")[0] == 404
        assert json.loads(capsys.readouterr().out)["local_setup_denied"] == "path"


def test_opaque_origin_same_origin_fetch_metadata_still_requires_csrf_and_exact_path(root):
    headers = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    values = generated_values()
    with serving(root) as server:
        fields = form_fields(server, values)
        fields["csrf"] = secrets.token_urlsafe(32)
        assert request(server, "POST", fields=fields, headers=headers)[0] == 403
        assert not (root / ".crypto.env").exists()
        assert request(server, "POST", path="/wrong", fields=form_fields(server, values), headers=headers)[0] == 404
        assert request(server, "POST", fields=form_fields(server, values),
                       headers={**headers, "Host": "outside.invalid"})[0] == 403
        assert not (root / ".crypto.env").exists()
        status, response_headers, page = request(server, "POST", fields=form_fields(server, values), headers=headers)
        assert status == 200 and server.state.completed
        assert "Access-Control-Allow-Origin" not in response_headers
        assert all(value not in page for value in values.values())
    assert all(dotenv_values(root / ".crypto.env")[key] == values[key] for key in FIELDS)


@pytest.mark.parametrize("site", [None, "cross-site", "same-site", "none", ""])
def test_opaque_origin_without_same_origin_fetch_metadata_remains_rejected(root, site):
    with serving(root) as server:
        headers = {"Origin": "null"}
        if site is not None:
            headers["Sec-Fetch-Site"] = site
        assert request(server, "POST", fields=form_fields(server, generated_values()), headers=headers)[0] == 403
        assert not server.state.completed
    assert not (root / ".crypto.env").exists()


def test_explicit_external_origin_is_rejected_even_if_fetch_metadata_says_same_origin(root):
    with serving(root) as server:
        assert request(server, "POST", fields=form_fields(server, generated_values()),
                       headers={"Origin": "https://outside.invalid", "Sec-Fetch-Site": "same-origin"})[0] == 403
    assert not (root / ".crypto.env").exists()

import json
import tarfile

from drivemods_bot.server_demo import server_settings
from scripts.build_server_bundle import build


def test_server_entry_point_cannot_enable_store_mutations(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.setenv("STORE_UPLOADS_ENABLED", "true")
    monkeypatch.setenv("STORE_PURCHASES_ENABLED", "true")
    monkeypatch.setenv("DRIVEMODS_BRIDGE_URL", "https://must-not-be-used.example")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = server_settings()
    assert settings.mode == "demo"
    assert settings.store_uploads_enabled is False
    assert settings.store_purchases_enabled is False
    assert settings.bridge_url == "" and settings.bridge_token == ""


def test_server_bundle_contains_only_application_and_secret_templates(tmp_path):
    root = tmp_path / "source"
    (root / "drivemods_bot").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "drivemods_bot/__init__.py").write_text("")
    (root / "requirements.lock").write_text("test==1")
    (root / "deploy/nextmod-license-bot.service").write_text("service")
    (root / "deploy/server.env.example").write_text("BOT_TOKEN=")
    (root / ".env").write_text("BOT_TOKEN=PRIVATE")
    (root / "private.bin").write_bytes(b"PRIVATE DEVICE")
    target = build(root, tmp_path / "server.tar.gz")
    with tarfile.open(target) as archive:
        names = archive.getnames()
        assert ".env" not in names and "private.bin" not in names
        assert "deploy/server.env.example" in names
        assert all(not item.islnk() and not item.issym() for item in archive.getmembers())

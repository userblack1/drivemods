import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from drivemods_bot.config import Settings
from drivemods_bot.field_order import complete_packet, prepare_packet
from drivemods_bot.models import OrderError
from drivemods_bot.readiness import doctor
from scripts.build_release import build


def test_field_packet_is_local_and_repeated_input_returns_same_packet(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "device.bin"
    source.write_bytes(b"SYNTHETIC FIELD TEST ID")
    with patch("httpx.AsyncClient.post", side_effect=AssertionError("No network allowed")):
        first = prepare_packet(settings, source, "full")
        again = prepare_packet(settings, source, "full")
    assert first == again
    manifest = json.loads((first / "order.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "prepared_local_only"
    assert manifest["supplier_cost_usd_cents"] == 9000
    assert manifest["retail_usd_cents"] == 10000
    assert (first / "device.id.bin").read_bytes() == source.read_bytes()
    with pytest.raises(OrderError):
        prepare_packet(settings, source, "eco")


def test_field_packet_rejects_input_file_as_a_finished_license(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "device.bin"
    source.write_bytes(b"SYNTHETIC ID")
    packet = prepare_packet(settings, source, "full")
    with pytest.raises(ValueError):
        complete_packet(settings, packet.name, source, "store-order", "payment-order")


def test_field_packet_records_download_without_claiming_vehicle_verification(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "device.bin"
    source.write_bytes(b"SYNTHETIC ID")
    packet = prepare_packet(settings, source, "full")
    output = tmp_path / "generated.bin"
    output.write_bytes(b"\x00UNIT TEST ONLY - NOT A VALID LICENSE")
    result = complete_packet(settings, packet.name, output, "store-order", "payment-order")
    manifest = json.loads((packet / "order.json").read_text(encoding="utf-8"))
    assert result.name == "device.license.bin"
    assert manifest["status"] == "downloaded_pending_vehicle_check"
    assert manifest["license_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_readiness_never_exposes_credentials_or_calls_purchase(tmp_path):
    secret = "123456:DO_NOT_LEAK_TEST_TOKEN"
    settings = Settings(data_dir=tmp_path, bot_token=secret, bridge_token="secret-bridge-test")
    with patch("httpx.AsyncClient.post", side_effect=AssertionError("No POST allowed")):
        report = asyncio.run(doctor(settings))
    text = json.dumps(report)
    assert report["preparation_ok"] is True
    assert report["purchase_verified"] is False
    assert secret not in text and "secret-bridge-test" not in text
    assert report["store_uploads_enabled"] is False


def test_release_excludes_secrets_device_files_and_browser_profiles(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / ".env").write_text("BOT_TOKEN=DO_NOT_PACKAGE_THIS")
    (root / ".env.example").write_text("BOT_TOKEN=")
    (root / "README.md").write_text("Test release")
    (root / "credentials.ps1").write_text("PRIVATE UNSOLICITED SCRIPT")
    (root / "device.id.bin").write_bytes(b"PRIVATE ID")
    (root / "data").mkdir()
    (root / "data" / "private.sqlite3").write_bytes(b"PRIVATE DATABASE")
    target = build(root, tmp_path / "release.zip")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert names == ["Drivemods-field-kit/.env.example", "Drivemods-field-kit/README.md", "Drivemods-field-kit/manifest.json"]
        manifest = json.loads(archive.read("Drivemods-field-kit/manifest.json"))
        assert manifest["secrets_included"] is False
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.read("Drivemods-field-kit/" + name)).hexdigest() == digest


from pathlib import Path
import subprocess

import pytest

from scripts.setup_dev_env import create


def test_dev_setup_never_overwrites_existing_configuration(tmp_path):
    target = tmp_path / ".env"
    target.write_text("existing private configuration", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create(target)
    assert target.read_text(encoding="utf-8") == "existing private configuration"


def test_dev_configuration_cannot_enable_production_purchases(tmp_path):
    from dotenv import dotenv_values

    target = tmp_path / ".env"
    create(target)
    values = dotenv_values(target)
    assert values["BOT_TOKEN"] == ""
    assert values["APP_MODE"] == "demo"
    assert values["DATA_DIR"] == "data/dev"
    assert values["STORE_UPLOADS_ENABLED"] == values["STORE_PURCHASES_ENABLED"] == "false"


def test_all_environment_examples_contain_only_empty_values():
    root = Path(__file__).resolve().parents[1]
    examples = list(root.glob("*.env.example")) + list((root / "deploy").glob("*.env.example"))
    assert examples
    for path in examples:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                assert "=" in line and line.split("=", 1)[1] == "", path.name


def test_audit_reports_only_metadata_and_scans_binary_bytes():
    import json
    from scripts.handoff_audit import inspect

    secret = ("9" * 9 + ":" + "Z" * 35).encode()
    report = inspect("image.png", b"\x00\xff" + secret)
    assert any(item["rule"] == "telegram-token" for item in report)
    assert secret.decode() not in json.dumps(report)


def test_audit_catches_secret_in_unreachable_git_object(tmp_path):
    import json
    from scripts.handoff_audit import audit

    def run(*args, payload=None):
        result = subprocess.run(["git", "-C", str(tmp_path), *args], input=payload, capture_output=True)
        assert result.returncode == 0
        return result.stdout

    run("init", "-b", "main")
    (tmp_path / "README.md").write_text("Clean source", encoding="utf-8")
    run("add", "README.md")
    secret = ("8" * 9 + ":" + "Y" * 35).encode()
    run("hash-object", "-w", "--stdin", payload=secret)
    result = audit(tmp_path, history=True)
    assert result["git_objects"] >= 2
    assert not result["ok"]
    assert any(item["file"].startswith("object:") for item in result["findings"])
    assert secret.decode() not in json.dumps(result)

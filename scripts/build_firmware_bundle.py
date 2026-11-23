"""Build firmware-only server sources using a strict whitelist."""
from pathlib import Path
import hashlib
import json
import tarfile


def build(root: Path, output: Path) -> Path:
    root = root.resolve()
    paths = sorted([
        *root.glob("firmware_bot/*.py"), root / "drivemods_bot/__init__.py",
        root / "drivemods_bot/locking.py", root / "requirements.lock",
        root / "requirements.firmware.lock", root / ".firmware.env.example",
        root / "deploy/nextmode-sw.service", root / "deploy/nextmode-sw-redirect.service", root / "docs/firmware-bot.md",
        root / "scripts/validate_firmware_live.py",
        root / "scripts/validate_firmware_choices.py",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with tarfile.open(output, "w:gz") as archive:
        for path in paths:
            if not path.is_file() or path.is_symlink():
                raise ValueError("Missing or unsafe bundle source: " + str(path))
            name = path.relative_to(root).as_posix()
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.add(path, arcname=name, recursive=False)
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    output.with_suffix(".sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n", encoding="ascii")
    return output


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    print(build(root, root / "release/nextmode-sw-server.tar.gz"))

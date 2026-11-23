"""Whitelist-only release. Never package .env, device IDs, data, or browser profiles."""
import hashlib
import json
from pathlib import Path
import zipfile


def build(root: Path, destination: Path, include_wheels: bool = True) -> Path:
    root = root.resolve()
    files = []
    for pattern in ("start.ps1", "stop.ps1", "prepare.ps1", "verify.ps1", "start.cmd", "check-before-trip.cmd", "pyproject.toml", "requirements.lock", "README.md", ".env.example",
                    "drivemods_bot/*.py", "firmware_bot/*.py", ".firmware.env.example", "requirements.firmware.lock", "sw.ps1", "connect-sw.ps1", "stop-sw.ps1", "start-sw.cmd",
                    "tests/*.py", "docs/*.md", "scripts/*.py"):
        files.extend(root.glob(pattern))
    if include_wheels:
        files.extend(root.glob("wheelhouse/*.whl"))
    files = sorted(set(p for p in files if p.is_file()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    checksums = {}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            checksums[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.write(path, "Drivemods-field-kit/" + relative)
        archive.writestr("Drivemods-field-kit/manifest.json", json.dumps({
            "version": "0.2.0", "platform": "Windows x64 / Python 3.12", "files": checksums,
            "live_store_verified": False, "device_ids_included": False, "secrets_included": False,
        }, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".sha256").write_text(digest + "  " + destination.name + "\n", encoding="ascii")
    return destination


if __name__ == "__main__":
    workspace = Path(__file__).resolve().parent.parent
    output = build(workspace, workspace / "release" / "Drivemods-field-kit.zip")
    print(output)
    print(f"Size: {output.stat().st_size / 1024 / 1024:.1f} MB")


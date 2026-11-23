"""Create a Linux source bundle without credentials or Windows dependencies."""
from pathlib import Path
import hashlib
import json
import tarfile


def build(root: Path, output: Path) -> Path:
    root = root.resolve()
    paths = sorted([*root.glob("drivemods_bot/*.py"), root / "requirements.lock",
                    root / "deploy/nextmod-license-bot.service", root / "deploy/server.env.example"])
    operator_unit = root / "deploy/nextmod-license-bot-operator.conf"
    if operator_unit.is_file():
        paths.append(operator_unit)
    for name in ('drivemods-bank-notices.service', 'drivemods-bank-notice-forwarder.service',
                 'drivemods-bank-notices.nginx.conf'):
        optional = root / 'deploy' / name
        if optional.is_file():
            paths.append(optional)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with tarfile.open(output, "w:gz") as archive:
        for path in paths:
            name = path.relative_to(root).as_posix()
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.add(path, arcname=name, recursive=False)
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    print(build(root, root / "release/nextmod-server-demo.tar.gz"))

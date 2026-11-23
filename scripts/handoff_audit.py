"""Read-only source/index/all-object audit. Findings never contain matched values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


RULES = {
    "telegram-token": re.compile(rb"\b[0-9]{6,14}:[A-Za-z0-9_-]{30,}\b"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "aws-key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "credential-url": re.compile(rb"https?://[^\s/:'\"<>]+:[^\s/@'\"<>]+@"),
}
PRIVATE_PARTS = {"data", "release", "backups", "_private_backups", ".venv", "venv", "wheelhouse", "__pycache__", ".pytest_cache"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".bin", ".log", ".key", ".pem", ".p12", ".pfx", ".session", ".zip", ".7z", ".bundle"}


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    if any(part in PRIVATE_PARTS for part in path.parts):
        return True
    if "assets/branding/references/" in name:
        return True
    if path.suffix.lower() in PRIVATE_SUFFIXES or ".session" in path.name or path.name.startswith("id_rsa") or path.name.startswith("id_ed25519"):
        return True
    if path.name.endswith(".env.example"):
        return False
    return bool(re.search(r"(?:^\.|\.)env(?:\.|$)", path.name))


def inspect(name: str, payload: bytes, *, check_path=True) -> list[dict]:
    findings = []
    if check_path and private_path(name):
        findings.append({"file": name, "rule": "private-file", "line": 0})
    # Binary files are scanned too; image metadata can contain credentials.
    for rule, pattern in RULES.items():
        for match in pattern.finditer(payload):
            findings.append({"file": name, "rule": rule, "line": payload[:match.start()].count(b"\n") + 1})
    if name.endswith(".env.example"):
        for number, line in enumerate(payload.decode("utf-8-sig").splitlines(), 1):
            if line.strip() and not line.lstrip().startswith("#"):
                if "=" not in line or line.split("=", 1)[1] != "":
                    findings.append({"file": name, "rule": "nonempty-env-example", "line": number})
    return findings


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        # Git errors may contain URLs/credentials. Do not forward subprocess output.
        raise RuntimeError("Git command failed; inspect repository state locally")
    return result.stdout


def indexed_blobs(root: Path) -> dict[str, bytes]:
    entries = []
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        if stage != b"0" or mode == b"120000":
            raise RuntimeError("Unmerged or symlink index needs manual review")
        entries.append((name.decode("utf-8"), oid))
    result = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                            input=b"".join(oid + b"\n" for _, oid in entries), capture_output=True)
    if result.returncode:
        raise RuntimeError("Could not read index blobs")
    blobs = {}
    offset = 0
    for name, _ in entries:
        end = result.stdout.index(b"\n", offset)
        _, kind, size = result.stdout[offset:end].split()
        if kind != b"blob":
            raise RuntimeError("Non-blob index entry needs manual review")
        offset = end + 1
        blobs[name] = result.stdout[offset:offset + int(size)]
        offset += int(size) + 1
    return blobs


def audit(root: Path, *, directory=False, history=False) -> dict:
    findings = []
    count = 0
    if directory:
        paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)
    else:
        index = indexed_blobs(root)
        paths = sorted(index)
    for name in paths:
        path = root / name
        if path.is_symlink():
            findings.append({"file": name, "rule": "symlink-not-reviewed", "line": 0})
            continue
        if path.exists():
            findings.extend(inspect(name, path.read_bytes()))
            count += 1
        if not directory:
            findings.extend(inspect("index/" + name, index[name]))
    objects = 0
    if history:
        # Includes reachable and unreachable objects, not only current branch log.
        raw = git(root, "cat-file", "--batch-all-objects", "--batch")
        offset = 0
        while offset < len(raw):
            end = raw.index(b"\n", offset)
            object_id, kind, size = raw[offset:end].decode("ascii").split()
            offset = end + 1
            payload = raw[offset:offset + int(size)]
            offset += int(size) + 1
            objects += 1
            if kind in {"blob", "commit", "tag"}:
                findings.extend(inspect("object:" + object_id, payload, check_path=False))
        for ref in git(root, "rev-list", "--all", "--reflog").decode("ascii").splitlines():
            for name in git(root, "ls-tree", "-r", "--name-only", "-z", ref).decode("utf-8").strip("\0").split("\0"):
                if name and private_path(name):
                    findings.append({"file": "commit:" + ref + "/" + name, "rule": "private-history-file", "line": 0})
    unique = {json.dumps(item, sort_keys=True): item for item in findings}
    return {"files": count, "git_objects": objects, "findings": list(unique.values()), "ok": not unique}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--directory", action="store_true", help="Scan a curated source-only directory, never the private working tree")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    try:
        report = audit(args.root.resolve(), directory=args.directory, history=args.history)
    except (RuntimeError, OSError, ValueError):
        parser.exit(2, "Audit could not complete; no secret contents printed.\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

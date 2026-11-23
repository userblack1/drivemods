"""Local SSH transport for the owner's single VDSka deployment.

Credentials stay in ignored data/, outside command arguments and releases.
The first connection pins this server's key; changes are rejected thereafter.
"""
from contextlib import contextmanager
import base64
import hashlib
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "data/ssh-libs"))
import paramiko
from dotenv import dotenv_values


def redact(text):
    for filename in (ROOT / "data/server-access.env", ROOT / ".env", ROOT / ".firmware.env"):
        values = dotenv_values(filename)
        for name, value in values.items():
            if value and any(word in name for word in ("PASSWORD", "PASSPHRASE", "TOKEN", "API_HASH")):
                text = text.replace(value, "[REDACTED]")
    return re.sub(r"\b\d{6,14}:[A-Za-z0-9_-]{25,}", "[REDACTED BOT TOKEN]", text)


@contextmanager
def connect():
    values = dotenv_values(ROOT / "data/server-access.env")
    host = values.get("SSH_HOST")
    if host != "89.169.7.15":
        raise RuntimeError("Unexpected server; refusing connection")
    known = ROOT / "data/vdska-known_hosts"
    known.touch(exist_ok=True)
    client = paramiko.SSHClient()
    system_known = Path.home() / ".ssh/known_hosts"
    if system_known.exists():
        client.load_system_host_keys(str(system_known))
    client.load_host_keys(str(known))

    class PinFirstKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, ssh_client, hostname, key):
            if hostname not in {host, f"[{host}]:{int(values.get('SSH_PORT') or 22)}"}:
                raise paramiko.SSHException("Unexpected SSH destination")
            ssh_client.get_host_keys().add(hostname, key.get_name(), key)
            ssh_client.save_host_keys(str(known))
            digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            print(f"Pinned {hostname} {key.get_name()} SHA256:{digest}", flush=True)

    client.set_missing_host_key_policy(PinFirstKey())
    try:
        client.connect(host, port=int(values.get("SSH_PORT") or 22),
                       username=values.get("SSH_USER"), password=values.get("SSH_PASSWORD") or None,
                       key_filename=values.get("SSH_KEY_PATH") or None,
                       passphrase=values.get("SSH_KEY_PASSPHRASE") or None,
                       allow_agent=False, look_for_keys=False,
                       timeout=15, banner_timeout=15, auth_timeout=20)
        client.get_transport().set_keepalive(30)
        yield client
    finally:
        client.close()


def run(client, script, timeout=60):
    stdin, stdout, stderr = client.exec_command("bash -se", timeout=timeout)
    stdin.write(script.replace("\r\n", "\n"))
    stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace")
    errors = stderr.read().decode("utf-8", "replace")
    status = stdout.channel.recv_exit_status()
    print(redact(output), end="", flush=True)
    if errors:
        print(redact(errors), end="", file=sys.stderr, flush=True)
    if status:
        raise RuntimeError(f"Remote script exited {status}")
    return output


if __name__ == "__main__":
    script = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
    with connect() as ssh:
        run(ssh, script, timeout=600)

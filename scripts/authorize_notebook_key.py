"""Owner-side enrollment of a second notebook's PUBLIC Ed25519 key.

Preview is the default. --apply adds only this key to the owner's existing root
authorized_keys; no bot, sshd configuration, credentials or service is restarted.
The private key must be generated and retained on the second notebook.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import struct


def validate_key(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or len(text) > 8192:
        raise ValueError("Provide one public Ed25519 key, never a private key")
    parts = lines[0].split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("Expected an ssh-ed25519 public key without options")
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise ValueError("Invalid public key encoding") from None
    prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if len(raw) != len(prefix) + 32 or not raw.startswith(prefix):
        raise ValueError("Invalid public Ed25519 key structure")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return parts[1], fingerprint


def remote_script(key: str, fingerprint: str, label: str, apply: bool) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
        raise ValueError("Use a short ASCII notebook label")
    # Validate again before constructing a remote command. Only JSON literals,
    # never shell-interpolated user text, enter the Python script.
    checked, actual = validate_key("ssh-ed25519 " + key)
    if actual != fingerprint:
        raise ValueError("Public key fingerprint mismatch")
    payload = json.dumps(dict(key=checked, fingerprint=actual, label=label, apply=apply))
    code = '''import base64,fcntl,hashlib,json,os,pwd,stat,subprocess,tempfile,time
from pathlib import Path
cfg=json.loads(PAYLOAD)
if os.geteuid()!=0: raise RuntimeError('Enrollment requires the existing owner root account')
home=Path(pwd.getpwuid(0).pw_dir)
folder=home/'.ssh'
target=folder/'authorized_keys'
for path in (folder,target):
 if path.is_symlink(): raise RuntimeError('Refusing symlink in SSH enrollment paths')
effective=dict(line.split(' ',1) for line in subprocess.check_output(['/usr/sbin/sshd','-T'],text=True).splitlines() if ' ' in line)
if effective.get('pubkeyauthentication')!='yes' or effective.get('permitrootlogin') not in ('yes','prohibit-password','without-password'):
 raise RuntimeError('Existing SSH policy does not permit this owner key login; policy was not changed')
paths=effective.get('authorizedkeysfile','').split()
if not any(p in ('.ssh/authorized_keys','%h/.ssh/authorized_keys',str(target)) for p in paths):
 raise RuntimeError('Nonstandard authorized_keys path requires owner review')
def present(text):
 for line in text.splitlines():
  parts=line.split()
  for i,part in enumerate(parts[:-1]):
   if part=='ssh-ed25519' and parts[i+1]==cfg['key']: return True
 return False
old=target.read_text() if target.exists() else ''
exists=present(old)
if cfg['apply'] and not exists:
 folder.mkdir(mode=0o700,exist_ok=True)
 with (folder/'.drivemods-enrollment.lock').open('a') as lock:
  os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
  old=target.read_text() if target.exists() else ''
  if not present(old):
   if target.exists():
    backup=folder/('authorized_keys.before-'+cfg['label']+'-'+str(time.time_ns()))
    with backup.open('x') as out: out.write(old)
    backup.chmod(0o600)
   value=old+('' if not old or old.endswith('\\n') else '\\n')+'ssh-ed25519 '+cfg['key']+' '+cfg['label']+'\\n'
   fd,temp=tempfile.mkstemp(prefix='.authorized_keys-',dir=folder)
   try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as out: out.write(value);out.flush();os.fsync(out.fileno())
    os.replace(temp,target)
   finally:
    if os.path.exists(temp): os.unlink(temp)
  folder.chmod(0o700)
exists=present(target.read_text()) if target.exists() else False
print(json.dumps(dict(host='89.169.7.15',user='root',fingerprint=cfg['fingerprint'],applied=cfg['apply'],key_present=exists,scope='owner root access',bot_restarted=False,second_notebook_login_verified=False)))
'''.replace("PAYLOAD", repr(payload))
    return "python3 - <<'DRIVEMODS_ENROLLMENT'\n" + code + "\nDRIVEMODS_ENROLLMENT\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key-file", type=Path, required=True)
    parser.add_argument("--label", default="drivemods-notebook-2")
    parser.add_argument("--apply", action="store_true", help="Grant the owner-level root access requested by the owner")
    args = parser.parse_args()
    if args.public_key_file.suffix != ".pub":
        parser.error("Choose a .pub file; never pass a private key")
    key, fingerprint = validate_key(args.public_key_file.read_text(encoding="utf-8-sig"))
    script = remote_script(key, fingerprint, args.label, args.apply)
    # This transport and its private credentials exist only on the current
    # authorized owner's notebook; they are never copied to the new notebook.
    from scripts.server_ops import ROOT, connect, run
    if not (ROOT / "data/vdska-known_hosts").is_file():
        raise RuntimeError("A previously verified server host key is required")
    with connect() as ssh:
        run(ssh, script, timeout=30)


if __name__ == "__main__":
    main()

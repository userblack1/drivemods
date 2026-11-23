"""Owner-authorized QR rollout to the existing, pinned VDSka server.

Secrets and the recipient QR travel separately from the source release. Staging
never polls Telegram and never submits an ID or payment to DriveMods.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex

from scripts.server_ops import ROOT, connect, run


PRIVATE = ROOT / "data/bank-qr/mbank"
RELEASE = ROOT / "release/nextmod-mbank-qr-prepared.tar.gz"


def stage():
    stage_path = "/root/nextmod-mbank-qr-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    files = {
        "source.tar.gz": RELEASE,
        "tests.tar.gz": PRIVATE / "stage-tests.tar.gz",
        "source-manifest.json": RELEASE.with_suffix(".manifest.json"),
        "test-manifest.json": PRIVATE / "stage-tests.manifest.json",
        "source.txt": PRIVATE / "original-payload.txt",
    }
    for path in files.values():
        if not path.is_file():
            raise RuntimeError(f"Missing prepared input: {path.name}")
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    with connect() as ssh:
        ssh.get_transport().sock.settimeout(20)
        run(ssh, f"install -d -m 700 {shlex.quote(stage_path)}\n", timeout=20)
        with ssh.open_sftp() as sftp:
            for name, path in files.items():
                target = stage_path + "/" + name
                sftp.put(str(path), target)
                sftp.chmod(target, 0o600)
            with sftp.file(stage_path + "/upload-hashes.json", "w") as handle:
                handle.write(json.dumps(hashes))
            sftp.chmod(stage_path + "/upload-hashes.json", 0o600)
        run(ssh, f"cd {shlex.quote(stage_path)}\npython3 - <<'PY'\n" + r'''
from pathlib import Path
import hashlib, json, tarfile
root=Path.cwd()
for name,digest in json.loads((root/'upload-hashes.json').read_text()).items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest, name
for name in ('source.tar.gz','tests.tar.gz'):
    with tarfile.open(name) as archive:
        archive.extractall(root/'candidate', filter='data')
for manifest in ('source-manifest.json','test-manifest.json'):
    for name,digest in json.loads((root/manifest).read_text()).items():
        path=(root/'candidate'/name).resolve()
        assert path.is_relative_to(root/'candidate')
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest, name
print('Uploaded archives and extracted files verified')
PY
''', timeout=25)
    (PRIVATE / "rollout.json").write_text(json.dumps({"stage": stage_path, "hashes": hashes,
        "validated": False, "installed": False}, indent=2) + "\n", encoding="utf-8")
    print("Stage:", stage_path)


def validate():
    record = json.loads((PRIVATE / "rollout.json").read_text(encoding="utf-8"))
    path = record["stage"]
    if not path.startswith("/root/nextmod-mbank-qr-") or not path.replace("/", "").replace("-", "").isalnum():
        raise RuntimeError("Unexpected staging path")
    with connect() as ssh:
        ssh.get_transport().sock.settimeout(20)
        run(ssh, f"cd {shlex.quote(path)}/candidate\n" + r'''
/usr/bin/pip3 install --no-index --no-deps --no-compile --disable-pip-version-check --target /opt/nextmod-license-bot/.venv/lib/python3.12/site-packages wheelhouse/qrcode-8.2-py3-none-any.whl wheelhouse/pypng-0.20220715.0-py3-none-any.whl
/opt/nextmod-license-bot/.venv/bin/python -m pytest -q tests
/opt/nextmod-license-bot/.venv/bin/python - <<'PY'
import os,json
from pathlib import Path
from dotenv import dotenv_values
os.environ.update({k:v for k,v in dotenv_values('/etc/nextmod-license-bot.env').items() if v is not None})
os.environ.update(MBANK_QR_ENABLED='true',RECEIVING_BANK='MBANK',MBANK_QR_FULL_KGS='9000.00',MBANK_QR_ECO_KGS='6500.00',MBANK_QR_KGS_PER_USD='',MBANK_QR_SOURCE_PATH=str(Path('../source.txt').resolve()),DATA_DIR=str(Path('../smoke-data').resolve()))
from drivemods_bot.server_operator import server_settings
from drivemods_bot.bank_qr import MBankQr
s=server_settings()
assert not s.problems(),s.problems()
assert (s.mbank_qr_full_kgs_minor,s.mbank_qr_eco_kgs_minor)==(900000,650000)
assert s.mode=='operator' and not s.store_uploads_enabled and not s.store_purchases_enabled
qr=MBankQr.read(s.mbank_qr_source)
for amount in (900000,650000):
    generated=qr.with_amount(amount)
    assert MBankQr.parse(generated.url()).amount_minor==amount
    assert generated.recipient==qr.recipient
print(json.dumps(dict(configuration_ok=True,full_kgs=9000,eco_kgs=6500,auto_charges=False)))
PY
''', timeout=110)
    record["validated"] = True
    record["validated_at_utc"] = datetime.now(timezone.utc).isoformat()
    (PRIVATE / "rollout.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def install():
    record = json.loads((PRIVATE / "rollout.json").read_text(encoding="utf-8"))
    if not record.get("validated"):
        raise RuntimeError("The staged build must pass validation before installation")
    path = record["stage"]
    if not path.startswith("/root/nextmod-mbank-qr-") or not path.replace("/", "").replace("-", "").isalnum():
        raise RuntimeError("Unexpected staging path")
    with connect() as ssh:
        ssh.get_transport().sock.settimeout(20)
        output = run(ssh, f"cd {shlex.quote(path)}\n/opt/nextmod-license-bot/.venv/bin/python - <<'PY'\n" + r'''
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,os,pwd,shutil,sqlite3,subprocess,time
from dotenv import dotenv_values,set_key
stage=Path.cwd()
app=Path('/opt/nextmod-license-bot')
data=Path('/var/lib/nextmod-license-bot')
env=Path('/etc/nextmod-license-bot.env')
unit='nextmod-license-bot.service'
def command(*args):
    return subprocess.check_output(args,text=True,timeout=45).strip()
assert 'server_operator' in command('systemctl','show',unit,'--property=ExecStart','--value')
original=dotenv_values(env)
assert original.get('APP_MODE')=='operator'
assert Path(original['DATA_DIR'])==data
assert original.get('REQUIRE_USD_NET_CREDIT')=='true'
assert original.get('STORE_PURCHASES_ENABLED')=='false'
assert original.get('STORE_UPLOADS_ENABLED')=='false'
firmware_pid=command('systemctl','show','nextmode-sw.service','--property=MainPID','--value')
assert firmware_pid!='0'
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup=Path('/var/backups/nextmod-license-bot')/('mbank-qr-'+stamp)
backup.mkdir(mode=0o700,parents=True,exist_ok=False)
shutil.copytree(app/'drivemods_bot',backup/'drivemods_bot')
shutil.copy2(env,backup/'server.env')
(backup/'server.env').chmod(0o600)
if (app/'requirements.lock').exists(): shutil.copy2(app/'requirements.lock',backup/'requirements.lock')
source=data/'bank-qr/mbank/source.txt'
if source.exists(): shutil.copy2(source,backup/'previous-qr-source.txt')
manifest=json.loads((stage/'source-manifest.json').read_text())
for name,digest in manifest.items():
    assert hashlib.sha256((stage/'candidate'/name).read_bytes()).hexdigest()==digest,name
subprocess.run(['systemctl','stop',unit],check=True,timeout=45)
try:
    with sqlite3.connect(data/'orders.sqlite3') as src,sqlite3.connect(backup/'orders.sqlite3') as dst:
        src.backup(dst)
    (backup/'orders.sqlite3').chmod(0o600)
    before=sqlite3.connect(backup/'orders.sqlite3').execute('select count(*) from orders').fetchone()[0]
    for name in manifest:
        if not (name.startswith('drivemods_bot/') or name=='requirements.lock'): continue
        target=app/name
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.qr-update')
        shutil.copyfile(stage/'candidate'/name,temporary)
        temporary.chmod(0o644)
        os.chown(temporary,0,0)
        os.replace(temporary,target)
    user=pwd.getpwnam('nextmod-bot')
    for folder in (data/'bank-qr',data/'bank-qr/mbank'):
        folder.mkdir(mode=0o700,exist_ok=True)
        folder.chmod(0o700)
        os.chown(folder,user.pw_uid,user.pw_gid)
    temporary=source.with_suffix('.new')
    shutil.copyfile(stage/'source.txt',temporary)
    temporary.chmod(0o600)
    os.chown(temporary,user.pw_uid,user.pw_gid)
    os.replace(temporary,source)
    temporary=env.with_suffix('.qr-update')
    shutil.copy2(env,temporary)
    changes=dict(RECEIVING_BANK='MBANK',MBANK_QR_ENABLED='true',MBANK_QR_SOURCE_PATH=str(source),
        MBANK_QR_KGS_PER_USD='',MBANK_QR_FULL_KGS='9000.00',MBANK_QR_ECO_KGS='6500.00')
    for key,value in changes.items(): set_key(temporary,key,value,quote_mode='always')
    temporary.chmod(0o600)
    os.chown(temporary,0,0)
    assert {k:v for k,v in dotenv_values(temporary).items() if k not in changes}=={k:v for k,v in original.items() if k not in changes}
    os.replace(temporary,env)
    for file in data.glob('orders.sqlite3*'):
        os.chown(file,user.pw_uid,user.pw_gid)
    subprocess.run(['systemctl','start',unit],check=True,timeout=45)
    required={'qr_payload','qr_path','qr_sha256','received_amount_minor','received_currency'}
    for attempt in range(25):
        con=sqlite3.connect('file:'+str(data/'orders.sqlite3')+'?mode=ro',uri=True)
        cols={r[1] for r in con.execute('pragma table_info(operator_orders)')}
        con.close()
        if required<=cols: break
        time.sleep(1)
    assert required<=cols, 'Bot startup did not finish the QR schema migration'
    assert command('systemctl','is-active',unit)=='active', 'License service is not active'
    assert command('systemctl','is-enabled',unit)=='enabled', 'License service is not enabled'
    assert command('systemctl','show',unit,'--property=NRestarts','--value')=='0', 'Unexpected restart'
    assert command('systemctl','show','nextmode-sw.service','--property=MainPID','--value')==firmware_pid, 'Firmware service PID changed'
    assert command('systemctl','is-active','nextmode-sw.service')=='active', 'Firmware service is not active'
    con=sqlite3.connect('file:'+str(data/'orders.sqlite3')+'?mode=ro',uri=True)
    assert con.execute('select count(*) from orders').fetchone()[0]>=before
    con.close()
    report=dict(installed=True,backup=str(backup),mode='operator',full_kgs=9000,eco_kgs=6500,
        auto_charges=False,firmware_pid_unchanged=True,license_pid=int(command('systemctl','show',unit,'--property=MainPID','--value')),
        installed_at_utc=datetime.now(timezone.utc).isoformat())
    (stage/'installed.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
except BaseException:
    subprocess.run(['systemctl','stop',unit],check=False,timeout=45)
    for old in (backup/'drivemods_bot').glob('*.py'): shutil.copy2(old,app/'drivemods_bot'/old.name)
    if (backup/'requirements.lock').exists(): shutil.copy2(backup/'requirements.lock',app/'requirements.lock')
    shutil.copy2(backup/'server.env',env)
    if (backup/'previous-qr-source.txt').exists(): shutil.copy2(backup/'previous-qr-source.txt',source)
    subprocess.run(['systemctl','start',unit],check=False,timeout=45)
    print(json.dumps(dict(installed=False,rollback_attempted=True,backup=str(backup))))
    raise
PY
''', timeout=110)
    report = json.loads(output.strip().splitlines()[-1])
    record.update(report)
    (PRIVATE / "rollout.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "validate", "install"))
    args = parser.parse_args()
    {"stage": stage, "validate": validate, "install": install}[args.action]()

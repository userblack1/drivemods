"""Install an isolated, capture-only iPhone notification channel on the owner's VPS."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import shlex
import tarfile
import time
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from scripts.server_ops import ROOT, connect, run


PUBLIC_URL = 'https://alphatrade-auto.com/drivemods-bank-notice'
OUT = ROOT / 'data/bank-notices'


def stage():
    OUT.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    info = {'stamp': stamp, 'stage': '/root/bank-notice-probe-' + stamp,
            'backup': '/var/backups/nextmod-license-bot/bank-notice-probe-' + stamp,
            'mode': 'capture_only', 'endpoint': PUBLIC_URL}
    keys = OUT / 'phone-setup.env'
    if not keys.exists():
        keys.write_text('BANK_NOTICE_WRITE_TOKEN=' + secrets.token_urlsafe(32) + '\n', encoding='utf-8')
    token = dotenv_values(keys)['BANK_NOTICE_WRITE_TOKEN']
    setup = ('MBANK — личная настройка тестового канала\n\n'
             'Этот канал принимает уведомления. Он НЕ подтверждает оплату и НЕ покупает лицензию.\n\n'
             f'Адрес: {PUBLIC_URL}\nМетод: POST\n'
             f'Заголовок Authorization: Bearer {token}\n'
             'Тело запроса: JSON\nПоле text (тип Текст): переменная «Уведомление» из MBANK.\n\n'
             'Для технической проверки кнопкой запуска вместо переменной можно временно указать:\n'
             'text: Проверка подключения iPhone\nkind: transport_test\n'
             'После проверки удалите поле kind и верните переменную «Уведомление» в text.\n\n'
             'Ключ разрешает только отправку данных в этот тестовый канал. Не пересылайте этот файл клиентам.\n')
    (OUT / 'MBANK-setup.txt').write_text(setup, encoding='utf-8')
    source = OUT / 'sources.tar.gz'
    paths = [*ROOT.glob('drivemods_bot/*.py'), ROOT/'tests/test_bank_notices.py',
             ROOT/'deploy/drivemods-bank-notices.service', ROOT/'deploy/drivemods-bank-notice-forwarder.service',
             ROOT/'deploy/drivemods-bank-notices.nginx.conf']
    with tarfile.open(source, 'w:gz') as archive:
        for path in paths:
            archive.add(path,arcname=path.relative_to(ROOT).as_posix(),recursive=False)
    info['sources_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    (OUT/'deployment.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    with connect() as ssh:
        ssh.get_transport().sock.settimeout(20)
        run(ssh, f"install -d -m 700 {info['stage']}\n")
        with ssh.open_sftp() as sftp:
            for path in [source, keys, OUT/'MBANK-setup.txt']:
                sftp.put(str(path), info['stage'] + '/' + path.name)
                sftp.chmod(info['stage'] + '/' + path.name,0o600)
        run(ssh, f"""cd {info['stage']}
test "$(sha256sum sources.tar.gz | cut -d ' ' -f 1)" = {info['sources_sha256']}
tar -xzf sources.tar.gz
PYTHONPATH={info['stage']} /opt/nextmod-license-bot/.venv/bin/python -m pytest tests/test_bank_notices.py -q
nginx -t
""", timeout=90)
    print('Staged and tested capture-only receiver; credentials were not printed',flush=True)
    return info


INSTALL = r'''
import grp,json,os,pwd,re,shutil,sqlite3,subprocess,sys
from pathlib import Path
import secrets
from dotenv import dotenv_values

stage,backup=map(Path,sys.argv[1:])
app=Path('/opt/drivemods-bank-notices')
site=Path('/etc/nginx/sites-enabled/korea-cars').resolve()
assert site.is_relative_to('/etc/nginx')
assert not app.exists(), 'An installation already exists; do not rotate an active phone key'
assert not backup.exists()
backup.mkdir(parents=True,mode=0o700)
shutil.copy2(site,backup/'nginx-site.conf')
main_pid=subprocess.check_output(['systemctl','show','nextmod-license-bot.service','--property=MainPID','--value'],text=True).strip()
sw_pid=subprocess.check_output(['systemctl','show','nextmode-sw.service','--property=MainPID','--value'],text=True).strip()
assert int(main_pid)>0 and int(sw_pid)>0
config=dotenv_values('/etc/nextmod-license-bot.env')
assert config['BOT_TOKEN'].split(':')[0]=='9227738530'
assert '7112746989' in config['ADMIN_IDS'].split(',')
before=sqlite3.connect('file:/var/lib/nextmod-license-bot/orders.sqlite3?mode=ro',uri=True)
old_counts=before.execute('select state,count(*) from orders group by state').fetchall()
before.close()
subprocess.run(['nginx','-t'],check=True)
try:
 pwd.getpwnam('drivemods-bank-notices')
except KeyError:
 subprocess.run(['useradd','--system','--home-dir','/var/lib/drivemods-bank-notices','--shell','/usr/sbin/nologin','drivemods-bank-notices'],check=True)
app.mkdir(mode=0o755)
for name in ['bank_notices.py','bank_notice_forwarder.py']:
 shutil.copy2(stage/'drivemods_bot'/name,app/name)
 os.chmod(app/name,0o644)
write=dotenv_values(stage/'phone-setup.env')['BANK_NOTICE_WRITE_TOKEN']
read=secrets.token_urlsafe(32)
receiver=Path('/etc/drivemods-bank-notices.env')
receiver.write_text('BANK_NOTICE_WRITE_TOKEN='+write+'\nBANK_NOTICE_READ_TOKEN='+read+
 '\nBANK_NOTICE_DB=/var/lib/drivemods-bank-notices/notices.sqlite3\nBANK_NOTICE_EXPECTED_CARD=2260\n')
os.chmod(receiver,0o600)
forwarder=Path('/etc/drivemods-bank-notice-forwarder.env')
forwarder.write_text('BANK_NOTICE_READ_TOKEN='+read+'\nBANK_NOTICE_OWNER_ID=7112746989\n'
 'BANK_NOTICE_FORWARD_DB=/var/lib/nextmod-license-bot/bank-notice-forwarder.sqlite3\n')
os.chmod(forwarder,0o600)
units=['drivemods-bank-notices.service','drivemods-bank-notice-forwarder.service']
snippet=Path('/etc/nginx/snippets/drivemods-bank-notices.conf')
try:
 for unit in units:
  dest=Path('/etc/systemd/system')/unit
  assert not dest.exists(), 'Unexpected existing unit'
  shutil.copy2(stage/'deploy'/unit,dest)
  os.chmod(dest,0o644)
 assert not snippet.exists()
 shutil.copy2(stage/'deploy/drivemods-bank-notices.nginx.conf',snippet)
 os.chmod(snippet,0o644)
 text=site.read_text()
 name='server_name alphatrade-auto.com www.alphatrade-auto.com;'
 assert text.count(name)==2
 first=text.index(name)
 # This host has one HTTPS server followed by its Certbot HTTP redirect.
 assert re.search(r'listen\s+443\s+ssl',text[first:])
 assert text.index('listen 443 ssl') < text.rindex(name)
 text=text[:first]+text[first:].replace(name,name+'\n    include /etc/nginx/snippets/drivemods-bank-notices.conf;',1)
 site.write_text(text)
 subprocess.run(['nginx','-t'],check=True)
 subprocess.run(['systemctl','daemon-reload'],check=True)
 subprocess.run(['systemctl','enable','--now',*units],check=True)
 subprocess.run(['systemctl','reload','nginx'],check=True)
 for unit in units:
  subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
 assert subprocess.check_output(['systemctl','show','nextmod-license-bot.service','--property=MainPID','--value'],text=True).strip()==main_pid
 assert subprocess.check_output(['systemctl','show','nextmode-sw.service','--property=MainPID','--value'],text=True).strip()==sw_pid
except BaseException:
 subprocess.run(['systemctl','disable','--now',*units],check=False)
 shutil.copy2(backup/'nginx-site.conf',site)
 if subprocess.run(['nginx','-t']).returncode==0:
  subprocess.run(['systemctl','reload','nginx'])
 raise
report={'mode':'capture_only','license_pid_unchanged':main_pid,'firmware_pid_unchanged':sw_pid,
        'order_counts_before':old_counts,'receiver_loopback_port':8790}
(stage/'install-report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
'''


def install(info):
    with connect() as ssh:
        ssh.get_transport().sock.settimeout(20)
        output=run(ssh, '/opt/nextmod-license-bot/.venv/bin/python - '+shlex.quote(info['stage'])+' '+
                   shlex.quote(info['backup'])+" <<'PY'\n"+INSTALL+"\nPY\n",timeout=120)
        (OUT/'install-result.txt').write_text(output,encoding='utf-8')
    info['installed']=True
    (OUT/'deployment.json').write_text(json.dumps(info,indent=2),encoding='utf-8')


def probe(info):
    token=dotenv_values(OUT/'phone-setup.env')['BANK_NOTICE_WRITE_TOKEN']
    payload={'text':'Проверка серверного канала '+info['stamp'],'kind':'transport_test','event_id':str(uuid4())}
    with httpx.Client(timeout=20,follow_redirects=False) as client:
        denied=client.post(PUBLIC_URL,json=payload)
        assert denied.status_code==401, f'Unexpected unauthenticated status: {denied.status_code}'
        result=client.post(PUBLIC_URL,json=payload,headers={'Authorization':'Bearer '+token})
        assert result.status_code==200, f'Unexpected capture status: {result.status_code}'
        body=result.json()
        assert body['status']=='captured' and body['payment_confirmed'] is False and body['automatic_purchase'] is False
        duplicate=client.post(PUBLIC_URL,json=payload,headers={'Authorization':'Bearer '+token})
        assert duplicate.json()['status']=='duplicate'
        assert duplicate.json()['notice_id']==body['notice_id']
        assert client.get(PUBLIC_URL).status_code==403
    info['https_probe']={**body,'unauthenticated_rejected':True,'duplicate_rejected':True,'public_get_denied':True}
    (OUT/'deployment.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    print(json.dumps(info['https_probe'],ensure_ascii=False),flush=True)


if __name__=='__main__':
    info=stage()
    install(info)
    probe(info)

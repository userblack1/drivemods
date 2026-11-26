"""The local store connector retains only an explicitly authenticated session."""
from contextlib import contextmanager
import http.client
import json
import os
import secrets
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from scripts.connect_store import StoreSetupServer, save_session


@pytest.fixture
def root(tmp_path):
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'.gitignore').write_text('*.env\n/data/\n')
    (tmp_path/'data/new-license-bot').mkdir(parents=True,mode=0o700)
    return tmp_path


@contextmanager
def server(root, auth):
    s=StoreSetupServer(root,authenticator=auth)
    worker=threading.Thread(target=s.serve_forever,daemon=True)
    worker.start()
    try:yield s
    finally:
        s.shutdown();s.server_close();worker.join(3)


def post(s, **changes):
    fields={'csrf':s.state.csrf,'username':'owner','password':secrets.token_urlsafe(20),'remember':'yes'}
    fields.update(changes.pop('fields',{}))
    headers={'Origin':s.origin,'Content-Type':'application/x-www-form-urlencoded'}
    headers.update(changes.pop('headers',{}))
    c=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=3)
    try:
        c.request('POST',changes.get('path',s.state.path),urlencode(fields),headers)
        r=c.getresponse();return r.status,r.read().decode(),fields
    finally:c.close()


def identity():
    return SimpleNamespace(username='owner',user_id='ownUser1',session_token=secrets.token_urlsafe(32))


def test_login_saves_session_not_password(root):
    i=identity(); seen=[]
    def login(u,p):seen.append((u,p));return i
    with server(root,login) as s:
        status,html,fields=post(s)
        assert status==200 and 'DriveMods подключён' in html
        saved=json.loads((root/'data/new-license-bot/drivemods-session.json').read_text())
        assert saved==vars(i) and seen==[('owner',fields['password'])]
        assert fields['password'] not in json.dumps(saved) and i.session_token not in html
        assert post(s)[0]==410
    if os.name!='nt':assert (root/'data/new-license-bot/drivemods-session.json').stat().st_mode&0o777==0o600


@pytest.mark.parametrize('changes',[
    {'fields':{'csrf':'wrong'}}, {'fields':{'remember':'no'}},
    {'headers':{'Origin':'https://example.invalid'}},
    {'headers':{'Origin':'null','Sec-Fetch-Site':'cross-site'}},
    {'path':'/wrong'}, {'headers':{'Host':'example.invalid'}},
])
def test_invalid_requests_never_authenticate(root,changes):
    def no_login(*_):pytest.fail('Authentication must not be called')
    with server(root,no_login) as s:
        status,_,_=post(s,**changes)
        assert status in (403,404)
        assert not (root/'data/new-license-bot/drivemods-session.json').exists()


def test_expected_iab_same_origin_proxy_supported(root):
    with server(root,lambda *_:identity()) as s:
        assert post(s,headers={'Origin':'null','Sec-Fetch-Site':'same-origin'})[0]==200


def test_auth_failure_does_not_disclose_or_save(root):
    secret=secrets.token_urlsafe(32)
    def fail(*_):raise RuntimeError(secret)
    with server(root,fail) as s:
        status,html,_=post(s)
        assert status==400 and secret not in html
        assert not s.state.completed
        assert not (root/'data/new-license-bot/drivemods-session.json').exists()


def test_save_session_refuses_symlink(root,tmp_path):
    dest=root/'data/new-license-bot/drivemods-session.json'
    target=root/'elsewhere';target.write_text('unchanged')
    try:dest.symlink_to(target)
    except OSError:pytest.skip('Symlinks unavailable')
    with pytest.raises(ValueError):save_session(root,identity())
    assert target.read_text()=='unchanged'

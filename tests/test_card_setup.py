from contextlib import contextmanager
import http.client,json,subprocess,threading
from urllib.parse import urlencode
import pytest
from scripts.connect_card import CardSetupServer
from crypto_checkout.card_vault import OwnerCardVault,_luhn


@pytest.fixture
def root(tmp_path):
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'.gitignore').write_text('*.env\n/data/\n')
    (tmp_path/'data/new-license-bot').mkdir(parents=True,mode=0o700)
    return tmp_path


def synthetic_card():
    # Locally generated validation fixture, not an owner/bank card.
    return next('42424200000'+str(i)+'6994' for i in range(10) if _luhn('42424200000'+str(i)+'6994'))


@contextmanager
def serving(root):
    s=CardSetupServer(root);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
    try:yield s
    finally:s.shutdown();s.server_close();t.join(3)


def post(s,**overrides):
    data={'csrf':s.state.csrf,'pan':synthetic_card(),'expiry':'12/39','cvc':'123','name':'TEST OWNER','consent':'yes'}
    data.update(overrides)
    c=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=3)
    try:
        c.request('POST',s.state.path,urlencode(data),{'Origin':s.origin,'Content-Type':'application/x-www-form-urlencoded'})
        r=c.getresponse();return r.status,r.read().decode()
    finally:c.close()


def test_explicit_local_consent_seals_only_ciphertext_and_rejects_replay(root):
    with serving(root) as s:
        status,body=post(s)
        assert status==200 and synthetic_card() not in body and '123' not in body
        stage=root/'data/new-license-bot'
        assert synthetic_card().encode() not in (stage/'owner-card.card-vault').read_bytes()
        vault=OwnerCardVault(stage/'owner-card.key',stage/'owner-card.card-vault')
        assert vault.configured
        assert post(s)[0]==410


@pytest.mark.parametrize('changes',[{'consent':'no'},{'csrf':'invalid'},{'pan':'invalid'},{'expiry':'01/20'}])
def test_invalid_form_never_saves_payment_credentials(root,changes):
    with serving(root) as s:
        assert post(s,**changes)[0] in (400,403)
        assert not list((root/'data/new-license-bot').iterdir())

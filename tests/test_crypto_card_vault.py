from datetime import datetime,timezone
import json,os
from pathlib import Path
import pytest
from crypto_checkout.card_vault import OwnerCard,OwnerCardVault,CardVaultError


def sample_card():
    # Stripe's public test card, never an owner's real card.
    return OwnerCard('4242424242424242','12/39','123','TEST OWNER')


@pytest.fixture
def paths(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path/'owner.key',tmp_path/'owner.enc'


def test_encrypted_roundtrip_contains_no_plaintext_and_repr_hides_card(paths):
    key,enc=paths;card=sample_card()
    vault=OwnerCardVault.provision(card,key,enc,consent=True,expected_last4='4242')
    assert card.pan not in repr(card) and card.cvc not in repr(card)
    assert card.pan.encode() not in enc.read_bytes() and b'TEST OWNER' not in enc.read_bytes()
    loaded=vault.load();assert loaded.pan==card.pan and loaded.last4=='4242'
    loaded.clear();assert not loaded.pan and not loaded.cvc
    if os.name!='nt':assert key.stat().st_mode&0o777==enc.stat().st_mode&0o777==0o600


def test_explicit_consent_required_and_authorized_card_enforced(paths):
    key,enc=paths
    with pytest.raises(CardVaultError):OwnerCardVault.provision(sample_card(),key,enc,expected_last4='4242')
    with pytest.raises(CardVaultError):OwnerCardVault.provision(sample_card(),key,enc,consent=True)
    assert not key.exists() and not enc.exists()


def test_tampering_wrong_key_and_duplicate_provision_never_expose_or_replace(paths,tmp_path):
    key,enc=paths
    vault=OwnerCardVault.provision(sample_card(),key,enc,consent=True,expected_last4='4242')
    original=enc.read_bytes()
    with pytest.raises(CardVaultError):OwnerCardVault.provision(sample_card(),key,enc,consent=True,expected_last4='4242')
    assert enc.read_bytes()==original
    enc.write_bytes(original[:-4]+b'AAAA')
    assert not vault.configured
    with pytest.raises(CardVaultError) as e:vault.load()
    assert sample_card().pan not in str(e.value) and '123' not in str(e.value)


@pytest.mark.parametrize('changes',[
    {'pan':'4242424242424243'},{'expiry':'13/39'},{'expiry':'01/20'},
    {'expiry':'12/2039'},{'cvc':'12'},{'cvc':'1234'},{'name':'OWNER\nother'},
])
def test_invalid_or_expired_values_fail_before_any_file(paths,changes):
    key,enc=paths;card=sample_card()
    for k,v in changes.items():setattr(card,k,v)
    with pytest.raises(CardVaultError):OwnerCardVault.provision(card,key,enc,consent=True,expected_last4='4242')
    assert not key.exists() and not enc.exists()


def test_lazy_constructor_creates_no_directories_and_does_not_require_library(tmp_path):
    base=tmp_path/'not-created';vault=OwnerCardVault(base/'key',base/'card')
    assert not vault.configured and not base.exists()


@pytest.mark.skipif(os.name=='nt',reason='POSIX credential mode check')
def test_broad_credentials_or_directory_are_rejected(paths):
    key,enc=paths
    vault=OwnerCardVault.provision(sample_card(),key,enc,consent=True,expected_last4='4242')
    key.chmod(0o644);assert not vault.configured
    key.chmod(0o600);key.parent.chmod(0o755);assert not vault.configured

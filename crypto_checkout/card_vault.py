"""Encrypted, owner-authorized purchasing card; never a client card collector.

Provision only with the owner's explicit consent. Keys and ciphertext belong in
separate restricted credential files outside source archives, logs and Git.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re


class CardVaultError(ValueError):
    pass


def _private_path(path, *, must_exist=True):
    path=Path(path).absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise CardVaultError('Card credential path is unsafe')
    parent=path.parent
    if not parent.is_dir() or os.name!='nt' and parent.stat().st_mode&0o077:
        raise CardVaultError('Card credential directory must be owner-restricted')
    if must_exist:
        if not path.is_file() or not 0<path.stat().st_size<=16384:
            raise CardVaultError('Card credential file is unavailable')
        if os.name!='nt' and (path.stat().st_mode&0o077 or path.stat().st_uid!=os.geteuid()):
            raise CardVaultError('Card credentials must be restricted to the service owner')
    return path


def _fernet(key):
    try:
        from cryptography.fernet import Fernet
        return Fernet(key)
    except Exception:
        raise CardVaultError('Encrypted card credentials are unavailable') from None


def _luhn(pan):
    total=0
    for i,c in enumerate(reversed(pan)):
        n=int(c)
        if i%2:n=n*2-9 if n>4 else n*2
        total+=n
    return total%10==0


@dataclass(repr=False)
class OwnerCard:
    pan: str
    expiry: str
    cvc: str
    name: str

    @property
    def last4(self):return self.pan[-4:] if self.pan else ''

    def clear(self):
        # Drop references; Python cannot promise erasure of immutable copies.
        self.pan=self.expiry=self.cvc=self.name=''

    def validate(self, *, expected_last4='6994', now=None):
        now=now or datetime.now(timezone.utc)
        if (not isinstance(self.pan,str) or not self.pan.isascii()
                or not re.fullmatch(r'4[0-9]{12,18}',self.pan) or not _luhn(self.pan)
                or self.last4!=expected_last4):
            raise CardVaultError('The purchasing card does not match the authorized card')
        m=re.fullmatch(r'(0[1-9]|1[0-2])/([0-9]{2})',self.expiry or '')
        if not m or (2000+int(m[2]),int(m[1]))<(now.year,now.month):
            raise CardVaultError('The purchasing card expiry is invalid')
        if not isinstance(self.cvc,str) or not re.fullmatch(r'[0-9]{3}',self.cvc):
            raise CardVaultError('The purchasing card verification field is invalid')
        if (not isinstance(self.name,str) or not 0<len(self.name)<=120
                or self.name.strip()!=self.name or any(ord(c)<32 for c in self.name)):
            raise CardVaultError('The purchasing card holder field is invalid')
        return self


class OwnerCardVault:
    def __init__(self, key_path=None, vault_path=None, *, expected_last4='6994'):
        self.key_path=Path(key_path) if key_path else None
        self.vault_path=Path(vault_path) if vault_path else None
        self.expected_last4=expected_last4

    def __repr__(self):return 'OwnerCardVault(credentials=[REDACTED])'

    def load(self):
        if not self.key_path or not self.vault_path:
            raise CardVaultError('The owner purchasing card is not configured')
        card=None
        try:
            key=_private_path(self.key_path).read_bytes()
            encrypted=_private_path(self.vault_path).read_bytes()
            raw=_fernet(key).decrypt(encrypted)
            data=json.loads(raw)
            if not isinstance(data,dict) or set(data)!={'pan','expiry','cvc','name'}:
                raise ValueError('Invalid card payload')
            card=OwnerCard(**data).validate(expected_last4=self.expected_last4)
            return card
        except Exception:
            if card:card.clear()
            raise CardVaultError('The encrypted purchasing card could not be verified') from None

    @property
    def configured(self):
        try:card=self.load()
        except CardVaultError:return False
        card.clear()
        return True

    @classmethod
    def provision(cls, card, key_path, vault_path, *, consent=False, expected_last4='6994'):
        if consent is not True:
            raise CardVaultError('Explicit owner consent is required before saving a purchasing card')
        card.validate(expected_last4=expected_last4)
        key_path=_private_path(key_path,must_exist=False)
        vault_path=_private_path(vault_path,must_exist=False)
        if key_path==vault_path or key_path.exists() or vault_path.exists():
            raise CardVaultError('Use new separate credential files; existing cards are not overwritten')
        try:
            from cryptography.fernet import Fernet
            key=Fernet.generate_key()
            payload=json.dumps({'pan':card.pan,'expiry':card.expiry,'cvc':card.cvc,'name':card.name},
                               ensure_ascii=False).encode('utf-8')
            encrypted=Fernet(key).encrypt(payload)
        except Exception:
            raise CardVaultError('Card encryption is unavailable') from None
        created=[]
        try:
            for path,content in ((key_path,key),(vault_path,encrypted)):
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                created.append(path)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(content);stream.flush();os.fsync(stream.fileno())
            result=cls(key_path,vault_path,expected_last4=expected_last4)
            if not result.configured:raise CardVaultError('Card credential verification failed')
            return result
        except Exception:
            for path in created:path.unlink(missing_ok=True)
            raise CardVaultError('The purchasing card was not saved') from None

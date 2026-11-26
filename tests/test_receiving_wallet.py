"""Synthetic wallets only; no customer addresses, keys, transfers or purchases."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from uuid import uuid4

import pytest

bip = pytest.importorskip('bip_utils')
from crypto_checkout.ledger import Ledger
from crypto_checkout.receiving_wallet import ReceivingWallet
from crypto_checkout.demo import synthetic_receipt
from crypto_checkout.tron import parse_solid_receipt


def wallet_fixture(seed=b'SYNTHETIC WALLET TEST SEED ONLY!'):
    account=bip.Bip44.FromSeed(seed,bip.Bip44Coins.TRON).Purpose().Coin().Account(0)
    first=account.Change(bip.Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
    return account,first,ReceivingWallet(account.PublicKey().ToExtended(),first)


def issue(book,folder,wallet,index):
    source=folder/f'{index}.id';source.write_bytes(f'SYNTHETIC DEVICE {index}'.encode())
    return book.invoice(uuid4().hex,index+1,'full',source,hashlib.sha256(source.read_bytes()).hexdigest(),
                        1,now_ms=1_000_000,receiving_wallet=wallet,request_key='test:'+str(index))


def test_250_concurrent_equal_amounts_bind_reverse_order_payments_and_restart(tmp_path):
    _,_,wallet=wallet_fixture()
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True,require_exchange_credit=False)
    with ThreadPoolExecutor(max_workers=20) as pool:
        rows=list(pool.map(lambda i:issue(book,tmp_path,wallet,i),range(250)))
    assert len({r['address'] for r in rows})==250
    assert {r['amount_atoms'] for r in rows}=={100_000_000}
    book=Ledger(book.path,simulation=True,require_exchange_credit=False)
    for i,row in reversed(list(enumerate(rows))):
        tx=f'{i+1:064x}'
        t=parse_solid_receipt(synthetic_receipt(row['address'],row['amount_atoms'],tx,
                            timestamp_ms=row['created_ms']+1),tx)[0]
        assert book.observe(t)=='confirmed'
        assert book.observe(t)=='duplicate'
        assert book.get(row['id'])['txid']==tx
    new=issue(book,tmp_path,wallet,251)
    assert new['address'] not in {r['address'] for r in rows}


def test_public_derivation_matches_private_wallet_recovery(tmp_path):
    account,_,wallet=wallet_fixture()
    for i in (0,1,99,100,10000):
        derived=account.Change(bip.Bip44Changes.CHAIN_EXT).AddressIndex(i)
        assert wallet.address(i)==derived.PublicKey().ToAddress()
    with pytest.raises(ValueError):ReceivingWallet(account.PrivateKey().ToExtended(),wallet.address(0))
    with pytest.raises(ValueError):ReceivingWallet(account.PublicKey().ToExtended(),wallet.address(1))
    with pytest.raises(ValueError):ReceivingWallet(account.Change(bip.Bip44Changes.CHAIN_EXT).PublicKey().ToExtended(),wallet.address(0))


def test_failed_invoice_rolls_back_index_and_wallet_cannot_be_replaced(tmp_path):
    _,_,wallet=wallet_fixture()
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True)
    with pytest.raises(RuntimeError):
        with book.connection() as db:
            assert wallet.allocate(db,'test')==wallet.address(1)
            raise RuntimeError('synthetic interrupted reservation')
    first=issue(book,tmp_path,wallet,0)
    assert first['address']==wallet.address(1)
    _,_,other=wallet_fixture(b'OTHER SYNTHETIC WALLET TEST SEED!')
    with pytest.raises(ValueError,match='migration'):issue(book,tmp_path,other,1)
    next_row=issue(book,tmp_path,wallet,2)
    assert next_row['address']==wallet.address(2)


def test_late_and_extra_payments_to_old_address_never_credit_new_customer(tmp_path):
    _,_,wallet=wallet_fixture()
    book=Ledger(tmp_path/'orders.sqlite3',simulation=True,require_exchange_credit=False)
    old,new=[issue(book,tmp_path,wallet,i) for i in (0,1)]
    tx='a'*64
    late=parse_solid_receipt(synthetic_receipt(old['address'],old['amount_atoms'],tx,
                          timestamp_ms=old['expires_ms']+1),tx)[0]
    assert book.observe(late)=='late_payment_review'
    assert book.get(new['id'])['state']=='awaiting_payment'
    wrong=replace(late,txid='b'*64,address=new['address'],amount_atoms=70_000_000)
    assert book.observe(wrong)=='unmatched'
    assert book.get(new['id'])['state']=='awaiting_payment'


def test_offline_export_contains_only_public_wallet_configuration():
    from scripts.export_tron_watch import export_public
    mnemonic=bip.Bip39MnemonicGenerator().FromEntropy(hashlib.sha256(b'SYNTHETIC EXPORT TEST ONLY').digest())
    root=bip.Bip44.FromSeed(bip.Bip39SeedGenerator(mnemonic).Generate(),bip.Bip44Coins.TRON)
    account=root.Purpose().Coin().Account(0)
    first=account.Change(bip.Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
    public=export_public(str(mnemonic),'',first)
    assert set(public)=={'network','token','account_path','account_xpub','first_address','fingerprint'}
    assert public['account_xpub']==account.PublicKey().ToExtended()
    assert account.PrivateKey().ToExtended() not in repr(public)
    assert str(mnemonic) not in repr(public)
    with pytest.raises(ValueError):export_public(str(mnemonic),'wrong extra passphrase',first)


def test_owner_cli_exports_public_config_without_echoing_recovery_words(tmp_path,monkeypatch,capsys):
    import json
    from types import SimpleNamespace
    from scripts import export_tron_watch as tool
    mnemonic=str(bip.Bip39MnemonicGenerator().FromEntropy(hashlib.sha256(b'LOCAL CLI SYNTHETIC TEST').digest()))
    account=bip.Bip44.FromSeed(bip.Bip39SeedGenerator(mnemonic).Generate(),bip.Bip44Coins.TRON).Purpose().Coin().Account(0)
    first=account.Change(bip.Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress()
    folder=tmp_path/'data/new-license-bot';folder.mkdir(parents=True)
    dest=folder/'public.json'
    monkeypatch.setattr(tool,'ROOT',tmp_path)
    monkeypatch.setattr(tool.sys,'stdin',SimpleNamespace(isatty=lambda:True))
    monkeypatch.setattr(tool.sys,'argv',['export','--output',str(dest),'--expected-address',first])
    answers=iter([mnemonic,''])
    monkeypatch.setattr(tool.getpass,'getpass',lambda prompt:next(answers))
    assert tool.main()==0
    payload=dest.read_text()
    assert mnemonic not in payload+capsys.readouterr().out
    assert json.loads(payload)['account_xpub']==account.PublicKey().ToExtended()
    # Existing configuration is rejected before requesting any secret again.
    monkeypatch.setattr(tool.getpass,'getpass',lambda prompt:pytest.fail('must not ask for words'))
    assert tool.main()==1


def test_mixed_old_binance_and_new_wallet_payments_preserve_credit_policy(tmp_path):
    from crypto_checkout.demo import TEST_ADDRESS
    from crypto_checkout.binance import DepositCredit
    _,_,wallet=wallet_fixture()
    book=Ledger(tmp_path/'mixed.sqlite3',simulation=True,require_exchange_credit=True)
    source=tmp_path/'old.id';source.write_bytes(b'SYNTHETIC OLD DEVICE')
    old=book.invoice(uuid4().hex,900,'eco',source,hashlib.sha256(source.read_bytes()).hexdigest(),1,
                     shared_address=TEST_ADDRESS,allow_amount_suffix=True,now_ms=1_000_000)
    new=issue(book,tmp_path,wallet,0)
    book=Ledger(book.path,simulation=True,require_exchange_credit=True)
    for i,row in enumerate([new,old]):
        tx=f'{i+1:064x}'
        t=parse_solid_receipt(synthetic_receipt(row['address'],row['amount_atoms'],tx,
                              timestamp_ms=row['created_ms']+1),tx)[0]
        assert book.observe(t)=='confirmed'
        assert book.observe(t)=='duplicate'
        if row['id']==new['id']:assert book.get(new['id'])['state']=='paid'
        else:
            assert book.get(old['id'])['state']=='chain_confirmed'
            book.confirm_exchange(old['id'],DepositCredit('TEST-OLD-CREDIT',tx,row['address'],row['amount_atoms']))
    assert book.get(old['id'])['state']=='paid'
    assert book.get(new['id'])['payment_policy']=='tron_wallet'
    assert book.get(old['id'])['payment_policy']=='binance'
    assert book.require_exchange_credit is True


def test_wallet_bootstrap_requires_exact_explicit_owner_verification(tmp_path):
    from types import SimpleNamespace
    import json
    from crypto_checkout.application import _receiving_wallet,ConfigurationError
    account,first,wallet=wallet_fixture()
    cfg=tmp_path/'public.json';proof=tmp_path/'proof.json'
    public={'network':'TRON','token':'USDT','account_path':"m/44'/195'/0'",
            'account_xpub':account.PublicKey().ToExtended(),'first_address':first,'fingerprint':wallet.fingerprint}
    verified={'fingerprint':wallet.fingerprint,'first_address':first,
              'first_child_address':wallet.address(1),'owner_confirmed':True}
    for p,d in [(cfg,public),(proof,verified)]:p.write_text(json.dumps(d));p.chmod(0o600)
    settings=SimpleNamespace(receiving_wallet_file=cfg,wallet_verification_file=proof)
    assert _receiving_wallet(settings).address(1)==wallet.address(1)
    verified['owner_confirmed']=False;proof.write_text(json.dumps(verified))
    with pytest.raises(ConfigurationError):_receiving_wallet(settings)
    settings.wallet_verification_file=None
    with pytest.raises(ConfigurationError):_receiving_wallet(settings)


def test_checkout_wallet_issues_exact_prices_without_cent_suffix(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from crypto_checkout.checkout import CheckoutService,invoice_text
    from crypto_checkout.demo import TEST_ADDRESS
    _,_,wallet=wallet_fixture()
    class Chain:
        async def height(self):return 1
    book=Ledger(tmp_path/'orders.sqlite3',simulation=False)
    service=CheckoutService(book,tmp_path/'uploads',Chain(),SimpleNamespace(ready=True),
                             address=TEST_ADDRESS,receiving_wallet=wallet)
    async def customers():
        return await asyncio.gather(*(service.choose(service.upload(i+1,'device.id',f'DEVICE{i}'.encode(),f'tg:{i}')['id'],
                                      i+1,'full' if i%2 else 'eco') for i in range(120)))
    rows=asyncio.run(customers())
    assert len({r['address'] for r in rows})==120
    assert {r['amount_atoms'] for r in rows}=={100_000_000,70_000_000}
    assert all(r['payment_policy']=='tron_wallet' for r in rows)
    assert all('Binance' not in invoice_text(r) for r in rows)

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

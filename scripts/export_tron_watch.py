"""Owner-run offline export of public receiving configuration.

Run only on the owner's trusted local computer. No network, private export,
wallet creation, signing, deployment or bot startup. Never run on the VPS.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def export_public(mnemonic, passphrase, expected_first):
    from bip_utils import Bip39SeedGenerator,Bip44,Bip44Coins
    from crypto_checkout.receiving_wallet import ReceivingWallet
    account=Bip44.FromSeed(Bip39SeedGenerator(mnemonic).Generate(passphrase),
                           Bip44Coins.TRON).Purpose().Coin().Account(0)
    xpub=account.PublicKey().ToExtended()
    wallet=ReceivingWallet(xpub,expected_first)
    return {'network':'TRON','token':'USDT','account_path':"m/44'/195'/0'",
            'account_xpub':xpub,'first_address':expected_first,'fingerprint':wallet.fingerprint}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    mnemonic=passphrase=''
    try:
        output=args.output.absolute()
        # Export must stay outside versioned source, and never overwrite.
        private=ROOT/'data/new-license-bot'
        if output.parent!=private or not private.is_dir() or output.exists():
            raise ValueError('Choose a new output in the protected local configuration folder')
        if not sys.stdin.isatty():raise ValueError('Interactive owner terminal required')
        print('Local OFFLINE public-only export. Do not enter secrets in chat or on the VPS.')
        expected=input('First TRON receiving address shown by your own wallet: ').strip()
        mnemonic=getpass.getpass('Wallet recovery phrase (hidden, not stored): ')
        passphrase=getpass.getpass('BIP39 extra passphrase, empty if unused (hidden): ')
        public=export_public(mnemonic,passphrase,expected)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(public,f,indent=2);f.flush();os.fsync(f.fileno())
        print('Public configuration exported. No funds moved and no private key was exported.')
    except Exception:
        print('Export not completed. Check the wallet/path and protected destination. No secret values printed.')
        return 1
    finally:
        mnemonic=passphrase=''
    return 0


if __name__=='__main__':raise SystemExit(main())

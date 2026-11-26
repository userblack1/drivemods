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
    parser.add_argument('--expected-address',help='Public first address confirmed in the owner wallet')
    parser.add_argument('--pause',action='store_true',help='Keep the owner terminal open after export')
    args=parser.parse_args()
    mnemonic=passphrase=''
    try:
        output=args.output.absolute()
        # Export must stay outside versioned source, and never overwrite.
        private=ROOT/'data/new-license-bot'
        if output.parent!=private or not private.is_dir() or output.exists():
            raise ValueError('Choose a new output in the protected local configuration folder')
        if any(p.is_symlink() for p in (output,*output.parents)):
            raise ValueError('Credential paths must not use symbolic links')
        if not sys.stdin.isatty():raise ValueError('Interactive owner terminal required')
        print('Локальный экспорт только публичного ключа. Сетевых запросов и переводов нет.')
        print('Фраза не сохраняется. Не вводите её в чат, браузер или на сервере.')
        expected=args.expected_address or input('Первый адрес TRON из вашего кошелька: ').strip()
        from crypto_checkout.tron import address_bytes
        address_bytes(expected)
        print('Сверьте первый адрес с TronLink: '+expected)
        mnemonic=getpass.getpass('Резервные слова через пробел (ввод скрыт): ')
        passphrase=getpass.getpass('Дополнительная фраза BIP39, обычно пусто — Enter (не пароль TronLink): ')
        public=export_public(mnemonic,passphrase,expected)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(public,f,indent=2);f.flush();os.fsync(f.fileno())
        print('Готово. Сохранён только публичный ключ. Приватные ключи не экспортировались.')
    except Exception:
        print('Экспорт не завершён. Проверьте слова, адрес и папку. Значения не выводятся.')
        return 1
    finally:
        mnemonic=passphrase=''
        if args.pause and sys.stdin.isatty():
            input('Нажмите Enter, чтобы закрыть окно: ')
    return 0


if __name__=='__main__':raise SystemExit(main())

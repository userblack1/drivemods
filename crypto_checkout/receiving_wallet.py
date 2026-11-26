"""Watch-only TRON address allocation. No seed, signing or network operations.

Not enabled by the production bootstrap. A wallet recovery/withdrawal proof and
an explicit change of payment confirmation policy are required before cutover.
"""
import hashlib

from .tron import address_bytes


class ReceivingWallet:
    def __init__(self, account_xpub, expected_first_address):
        from bip_utils import Bip44, Bip44Coins, Bip44Changes
        address_bytes(expected_first_address)
        try:
            account = Bip44.FromExtendedKey(account_xpub, Bip44Coins.TRON)
            if not account.IsPublicOnly() or account.Level() != 3:
                raise ValueError()
            self._branch = account.Change(Bip44Changes.CHAIN_EXT)
            if self.address(0) != expected_first_address:
                raise ValueError()
        except Exception:
            raise ValueError('A matching TRON account public key is required') from None
        self.fingerprint = hashlib.sha256(account_xpub.encode('ascii')).hexdigest()

    def __repr__(self):
        return 'ReceivingWallet(public_configuration=[REDACTED])'

    def address(self, index):
        if type(index) is not int or not 0 <= index < 2**31:
            raise ValueError('Invalid public address index')
        result = self._branch.AddressIndex(index).PublicKey().ToAddress()
        address_bytes(result)
        return result

    def allocate(self, db, invoice_id):
        """Called only inside the invoice's BEGIN IMMEDIATE transaction.

        Counter and invoice commit together. Index zero is reserved for owner
        recovery/withdrawal verification; customer invoices start at index one.
        One ledger is bound to one wallet to avoid accidental wallet replacement.
        """
        marker = db.execute("SELECT value FROM metadata WHERE key='receiving_wallet'").fetchone()
        if marker and marker[0] != self.fingerprint:
            raise ValueError('Changing the receiving wallet requires a reviewed migration')
        db.execute("INSERT OR IGNORE INTO metadata VALUES('receiving_wallet',?)", (self.fingerprint,))
        counter = db.execute("SELECT value FROM metadata WHERE key='receiving_wallet_next'").fetchone()
        index = int(counter[0]) if counter else 1
        result = self.address(index)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('receiving_wallet_next',?)", (str(index+1),))
        return result

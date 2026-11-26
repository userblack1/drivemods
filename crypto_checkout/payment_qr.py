"""Local TRON address QR; never imply bank apps understand an amount URI."""
from io import BytesIO
import re
import time

import qrcode
from qrcode.image.pure import PyPNGImage

from .tron import address_bytes


def payment_details(invoice, *, now_ms=None):
    """Validate the same immutable invoice used by chain verification."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if (not isinstance(invoice.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', invoice['id'])
            or invoice.get('state') != 'awaiting_payment'
            or type(invoice.get('expires_ms')) is not int or invoice['expires_ms'] <= now_ms
            or invoice.get('plan') not in {'full', 'eco'}):
        raise ValueError('Only a current unpaid invoice can have a payment QR')
    address = invoice.get('address')
    address_bytes(address)
    atoms = invoice.get('amount_atoms')
    if (type(atoms) is not int or atoms <= 0 or atoms % 10_000
            or atoms // 1_000_000 != {'full': 100, 'eco': 70}[invoice['plan']]):
        raise ValueError('Unexpected invoice amount')
    amount = f'{atoms // 1_000_000}.{atoms % 1_000_000 // 10_000:02d}'
    return address, amount


def payment_qr(invoice, *, now_ms=None):
    address, _ = payment_details(invoice, now_ms=now_ms)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=14, border=4)
    # My O! documents scanning a recipient address only. A made-up amount URI
    # could be rejected or interpreted as the wrong asset. Keep the amount in
    # the invoice caption and a copy button until wallet support is verified.
    qr.add_data(address)
    qr.make(fit=True)
    output = BytesIO()
    qr.make_image(image_factory=PyPNGImage).save(output)
    return output.getvalue()

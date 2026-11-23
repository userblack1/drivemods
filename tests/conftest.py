import hashlib
from urllib.parse import quote

import pytest


@pytest.fixture
def bank_qr_source(tmp_path):
    # Synthetic, non-customer recipient. No real payment link is published.
    body = "00020101021132440012c2c.mbank.kg01020210129960000000001302125204999953034175907TEST QR"
    tail = hashlib.sha256(body.encode()).hexdigest()[-4:].upper()
    path = tmp_path / "synthetic-mbank-qr.txt"
    path.write_text("https://pay.payqr.kg/qr/#" + quote(body + "6304" + tail, safe="-._~"), encoding="utf-8")
    return path

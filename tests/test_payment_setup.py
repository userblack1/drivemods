from decimal import Decimal

import pytest

from drivemods_bot.config import Settings
from drivemods_bot.payment_setup import amount_kgs_minor, readiness


def test_confirmed_prices_and_margin_do_not_depend_on_an_assumed_exchange_rate():
    report = readiness(Settings())
    assert report["plans"]["full"] == dict(retail_usd_cents=10000, supplier_usd_cents=9000,
                                            gross_margin_usd_cents=1000, preview_kgs_minor=None)
    assert report["plans"]["eco"] == dict(retail_usd_cents=7000, supplier_usd_cents=6000,
                                           gross_margin_usd_cents=1000, preview_kgs_minor=None)
    assert report["bank_acceptance"] == "not_connected" and report["preview_only"]
    assert report["receipt_images_confirm_payment"] is False


def test_explicit_rate_is_only_a_preview_with_exact_minor_currency_amounts():
    # Synthetic arithmetic example, not a current bank exchange rate.
    report = readiness(Settings(), "87.50")
    assert report["plans"]["full"]["preview_kgs_minor"] == 875000
    assert report["plans"]["eco"]["preview_kgs_minor"] == 612500
    assert report["bank_acceptance"] == "not_connected"
    assert amount_kgs_minor(1, "87.50") == 88


@pytest.mark.parametrize("rate", ["", "NaN", "Infinity", "-Infinity", "-1", "0", "1.0000001", "1e999999", 87.5, Decimal("87.5")])
def test_invalid_or_implicit_rate_is_rejected(rate):
    with pytest.raises(ValueError):
        amount_kgs_minor(10000, rate)


@pytest.mark.parametrize("amount", [0, -1, True, 100.0, "100", 10**20])
def test_payment_calculation_never_accepts_invalid_usd_cents(amount):
    with pytest.raises(ValueError):
        amount_kgs_minor(amount, "87.5")

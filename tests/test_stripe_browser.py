"""Offline buyer-browser fixtures only. No real Stripe page, card or payment."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from crypto_checkout.store_adapter import CheckoutSession, PaymentAuthorization
from crypto_checkout.stripe_browser import BrowserStripeExecutor, StripeCheckoutError, verify_pay_label


def synthetic_pan():
    for digit in range(10):
        pan = '40000000000' + str(digit) + '6994'
        nums = [int(x) for x in pan]
        total = sum((n * 2 - 9 if n > 4 else n * 2) if (len(nums)-i) % 2 == 0 else n
                    for i, n in enumerate(nums))
        if total % 10 == 0: return pan
    raise AssertionError('Missing synthetic fixture')


class Card:
    def __init__(self):
        self.pan, self.expiry, self.cvc, self.name, self.last4 = synthetic_pan(), '12/30', '123', 'TEST OWNER', '6994'
        self.cleared = False
    def clear(self):
        self.pan = self.expiry = self.cvc = self.name = self.last4 = ''
        self.cleared = True


class Vault:
    configured = True
    def __init__(self): self.cards = []
    def load(self):
        card = Card()
        self.cards.append(card)
        return card


class Locator:
    def __init__(self, page, selector): self.page, self.selector = page, selector
    async def wait_for(self, **kwargs): pass
    async def get_attribute(self, name):
        assert name == 'src'
        return self.page.frame_src
    async def count(self):
        if self.selector == 'usd-checkbox': return 1 if self.page.usd_label else 0
        if self.selector == 'kg-checkbox': return 1 if self.page.usd_label else 0
        if self.selector == '#billingCountry': return 1 if self.page.country_present else 0
        return 1
    async def evaluate(self, expression):
        assert self.selector == '#billingCountry' and expression == 'el => el.tagName'
        return self.page.country_tag
    async def select_option(self, value):
        assert self.selector == '#billingCountry'
        self.page.country_selections.append(value)
        if not self.page.country_sticks: self.page.country = value
    async def input_value(self):
        assert self.selector == '#billingCountry'
        return self.page.country
    async def is_checked(self):
        return self.page.usd_checked if self.selector == 'usd-checkbox' else self.page.kg_checked
    async def check(self):
        assert self.selector == 'usd-checkbox'
        self.page.usd_checked = True
        if not self.page.currency_sticks: self.page.kg_checked = False
    async def aria_snapshot(self):
        import json
        return '- checkbox ' + json.dumps(self.page.usd_label) + (' [checked]' if self.page.usd_checked else '')
    async def fill(self, value):
        self.page.fills.append(self.selector)
        if self.selector == '#billingName' and self.page.after_fill_label:
            self.page.usd_label = self.page.after_fill_label
        if self.selector == '#billingName' and self.page.country_changes_after_fill:
            self.page.country = 'RU'
    async def inner_text(self): return self.page.body if self.selector == 'body' else self.page.label
    async def is_enabled(self): return True
    async def click(self, **kwargs):
        self.page.clicks += 1
        if self.page.fail_click: raise TimeoutError('LOST RESPONSE WITH PRIVATE DETAILS')


class Page:
    def __init__(self):
        self.urls, self.fills, self.clicks = [], [], 0
        self.body, self.label = 'DriveMods USD Total US$90.00', 'Pay US$90.00'
        self.complete, self.fail_click, self.mount = True, False, ''
        self.frame_src = 'https://checkout.stripe.com/embedded-checkout/cs_live_TESTSESSION'
        self.usd_label, self.usd_checked, self.kg_checked, self.currency_sticks = None, False, True, False
        self.after_fill_label = None
        self.country, self.country_tag, self.country_present, self.country_sticks = 'RU', 'SELECT', True, False
        self.country_changes_after_fill, self.country_selections = False, []
    async def goto(self, url, **kwargs): self.urls.append(url)
    async def route(self, url, handler): self.mount = url
    def frame_locator(self, selector):
        assert selector == 'iframe[title="Embedded checkout"]'
        return self
    def locator(self, selector): return Locator(self, selector)
    def get_by_role(self, role, **kwargs):
        if role == 'checkbox':
            return Locator(self, 'kg-checkbox' if 'KG' in kwargs['name'].pattern else 'usd-checkbox')
        return Locator(self, role)
    async def wait_for_function(self, *args, **kwargs): pass
    async def evaluate(self, expression):
        assert expression == 'window.checkoutCompleted === true'
        return self.complete


class Factory:
    available = True
    def __init__(self): self.page, self.opens, self.closes = Page(), 0, 0
    @asynccontextmanager
    async def __call__(self):
        self.opens += 1
        try: yield self.page
        finally: self.closes += 1


def setup(tmp_path):
    vault, factory = Vault(), Factory()
    executor = BrowserStripeExecutor(vault, 'pk_live_' + 'PUBLICTESTONLY' * 3,
                                     tmp_path / 'new-private/card-journal.sqlite3', browser_factory=factory)
    session = CheckoutSession('cs_live_TESTSESSION', 'payment001', 9000, 'USD',
                              'cs_live_TESTSESSION_secret_TESTONLY')
    auth = PaymentAuthorization('a' * 32, 'license001', 'payment001', 'full', 9000)
    return vault, factory, executor, session, auth


def test_constructor_and_readiness_do_not_load_card_create_journal_or_launch_browser(tmp_path):
    vault, factory, executor, _, _ = setup(tmp_path)
    assert executor.configured and not executor.automatic_payment_ready
    assert not vault.cards and factory.opens == 0 and not (tmp_path / 'new-private').exists()


def test_explicit_blank_probe_creates_no_payment_journal_and_loads_no_card(tmp_path):
    vault, factory, executor, _, _ = setup(tmp_path)
    assert asyncio.run(executor.probe_runtime()) is True
    assert executor.automatic_payment_ready and factory.page.urls == ['about:blank']
    assert not vault.cards and not (tmp_path / 'new-private').exists()


def test_success_returns_pending_only_and_replayed_attempt_never_clicks_twice(tmp_path):
    vault, factory, executor, session, auth = setup(tmp_path)
    async def go():
        await executor.probe_runtime()
        assert await executor.start(session, auth, 'license:' + auth.invoice_id) == 'pending'
        assert await executor.start(session, auth, 'license:' + auth.invoice_id) == 'action_required'
    asyncio.run(go())
    assert factory.page.clicks == 1 and vault.cards[0].cleared
    assert factory.page.fills == ['#cardNumber', '#cardExpiry', '#cardCvc', '#billingName']
    assert factory.page.country == 'KG' and factory.page.country_selections == ['KG']
    raw = (tmp_path / 'new-private/card-journal.sqlite3').read_bytes()
    assert synthetic_pan().encode() not in raw and session.client_secret.encode() not in raw


@pytest.mark.parametrize('change', [{'total_cents': 9001}, {'currency': 'KGS'},
                                  {'payment_id': 'different'}, {'client_secret': 'unknown-session'}])
def test_changed_session_never_loads_card_or_opens_payment_page(tmp_path, change):
    vault, factory, executor, session, auth = setup(tmp_path)
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(replace(session, **change), auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert not vault.cards and factory.page.clicks == 0 and not (tmp_path / 'new-private').exists()


@pytest.mark.parametrize('body,label', [('Total 9000 KGS', 'Pay 9000 KGS'),
    ('USD Total US$100.00', 'Pay US$100.00'), ('Total $90.00', 'Pay $90.00'),
    ('USD Total US$90.00', 'Pay'), ('USD Total US$90.00', 'Subscribe US$90.00')])
def test_actual_ui_currency_final_amount_and_action_are_required(tmp_path, body, label):
    vault, factory, executor, session, auth = setup(tmp_path)
    factory.page.body, factory.page.label = body, label
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0 and vault.cards[0].cleared


def test_timeout_after_financial_click_is_not_retried_even_after_executor_restart(tmp_path):
    vault, factory, executor, session, auth = setup(tmp_path)
    factory.page.fail_click = True
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError) as exc:
            await executor.start(session, auth, 'license:' + auth.invoice_id)
        assert 'PRIVATE' not in str(exc.value)
        restarted = BrowserStripeExecutor(vault, 'pk_live_' + 'PUBLICTESTONLY' * 3,
            tmp_path / 'new-private/card-journal.sqlite3', browser_factory=factory)
        await restarted.probe_runtime()
        assert await restarted.start(session, auth, 'license:' + auth.invoice_id) == 'action_required'
    asyncio.run(go())
    assert factory.page.clicks == 1
    with sqlite3.connect(tmp_path / 'new-private/card-journal.sqlite3') as db:
        assert db.execute('SELECT phase FROM stripe_attempts').fetchone()[0] == 'submitting'


def test_challenge_returns_action_required_without_reading_otp_or_claiming_paid(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.complete = False
    async def go():
        await executor.probe_runtime()
        assert await executor.start(session, auth, 'license:' + auth.invoice_id) == 'action_required'
    asyncio.run(go())
    assert factory.page.clicks == 1 and len(factory.page.fills) == 4


def test_ten_distinct_authorized_orders_are_submitted_once_each(tmp_path):
    vault, factory, executor, session, auth = setup(tmp_path)
    async def go():
        await executor.probe_runtime()
        for index in range(10):
            item = replace(auth, invoice_id=f'{index+1:032x}', license_id=f'license{index}', payment_id=f'payment{index}')
            checkout = replace(session, payment_id=item.payment_id, session_id=f'cs_live_TEST{index}',
                               client_secret=f'cs_live_TEST{index}_secret_TESTONLY')
            assert await executor.start(checkout, item, 'license:' + item.invoice_id) == 'pending'
            assert await executor.start(checkout, item, 'license:' + item.invoice_id) == 'action_required'
    asyncio.run(go())
    assert factory.page.clicks == 10 and len(vault.cards) == 10 and all(card.cleared for card in vault.cards)


@pytest.mark.parametrize('source', ['https://stripe.example/checkout', 'http://checkout.stripe.com/',
                                  'https://checkout.stripe.com.evil.example/', 'https://owner@checkout.stripe.com/',
                                  'https://js.stripe.com/v3/other-page.html',
                                  'https://js.stripe.com.evil.example/v3/embedded-checkout-inner-a.html'])
def test_card_fields_are_never_filled_in_an_unverified_frame(tmp_path, source):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.frame_src = source
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert not factory.page.fills and factory.page.clicks == 0


@pytest.mark.parametrize('name,value', [
    ('DEBUG', 'pw:api'), ('DEBUG', '*'), ('DEBUG', 'pw*'),
    ('DEBUG', 'app:cache'), ('DEBUG', '0'), ('DEBUG', ' '),
    ('PWDEBUG', '1'), ('PWDEBUG', '0'), ('PWDEBUG', ' '),
])
def test_debug_card_logging_configuration_blocks_executor_before_card_or_browser(tmp_path, monkeypatch, name, value):
    monkeypatch.delenv('DEBUG', raising=False)
    monkeypatch.delenv('PWDEBUG', raising=False)
    vault, factory, executor, session, auth = setup(tmp_path)
    async def go():
        assert await executor.probe_runtime()
        opened = factory.opens
        monkeypatch.setenv(name, value)
        assert not executor.configured and not executor.automatic_payment_ready
        assert not await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError, match='not configured'):
            await executor.start(session, auth, 'license:' + auth.invoice_id)
        assert factory.opens == opened
    asyncio.run(go())
    assert not vault.cards and not factory.page.fills and factory.page.clicks == 0
    assert not (tmp_path / 'new-private').exists()


def test_empty_debug_settings_allow_explicit_blank_browser_probe_without_card(tmp_path, monkeypatch):
    monkeypatch.setenv('DEBUG', '')
    monkeypatch.setenv('PWDEBUG', '')
    vault, factory, executor, _, _ = setup(tmp_path)
    assert executor.configured
    assert asyncio.run(executor.probe_runtime()) is True
    assert not vault.cards and factory.page.clicks == 0


def test_a_reserved_session_cannot_be_reused_for_another_invoice(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    async def go():
        await executor.probe_runtime()
        await executor.start(session, auth, 'license:' + auth.invoice_id)
        other = replace(auth, invoice_id='b' * 32)
        with pytest.raises(StripeCheckoutError, match='another invoice'):
            await executor.start(session, other, 'license:' + other.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 1


@pytest.mark.parametrize('option,button', [(' US 90,00 $', 'Оплатить'), ('US $90.00', 'Pay')])
def test_observed_real_stripe_js_frame_and_plain_pay_button_require_exact_usd_selection(tmp_path, option, button):
    _, factory, executor, session, auth = setup(tmp_path)
    page = factory.page
    page.frame_src = 'https://js.stripe.com/v3/embedded-checkout-inner-a1b2c3d4.html'
    page.usd_label, page.label, page.body = option, button, 'KG 8 186.61 KGS US 90,00 $'
    async def go():
        await executor.probe_runtime()
        assert await executor.start(session, auth, 'license:' + auth.invoice_id) == 'pending'
    asyncio.run(go())
    assert page.usd_checked and not page.kg_checked and page.clicks == 1


@pytest.mark.parametrize('option', ['US $100.00', 'US 60,00 $', 'USD 90.01', 'US $90.00 fee $5.00',
                                   'KG 90,00 $', 'US 90,00 €', 'US 9 000,00 $'])
def test_plain_payment_button_rejects_wrong_or_ambiguous_currency_option(tmp_path, option):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.usd_label, factory.page.label = option, 'Pay'
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0


def test_plain_pay_without_selected_usd_amount_proof_remains_blocked(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.label = 'Pay'
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0


def test_local_currency_still_checked_prevents_click_even_with_usd_checked(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.usd_label, factory.page.label, factory.page.currency_sticks = 'US $90.00', 'Pay', True
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0


def test_eco_observed_usd_selection_allows_plain_pay_only_for_sixty_dollars(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.usd_label, factory.page.label = 'US 60,00 $', 'Оплатить'
    session, auth = replace(session, total_cents=6000), replace(auth, plan='eco', max_cents=6000)
    async def go():
        await executor.probe_runtime()
        assert await executor.start(session, auth, 'license:' + auth.invoice_id) == 'pending'
    asyncio.run(go())
    assert factory.page.clicks == 1


def test_currency_amount_is_rechecked_after_card_entry_before_submission(tmp_path):
    _, factory, executor, session, auth = setup(tmp_path)
    factory.page.usd_label, factory.page.label, factory.page.after_fill_label = 'US $90.00', 'Pay', 'US $95.00'
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0


@pytest.mark.parametrize('problem', ['missing', 'not_native', 'cannot_select', 'changed_after_fill'])
def test_owner_billing_country_must_be_selected_and_remain_kg_before_pay(tmp_path, problem):
    _, factory, executor, session, auth = setup(tmp_path)
    if problem == 'missing': factory.page.country_present = False
    if problem == 'not_native': factory.page.country_tag = 'DIV'
    if problem == 'cannot_select': factory.page.country_sticks = True
    if problem == 'changed_after_fill': factory.page.country_changes_after_fill = True
    async def go():
        await executor.probe_runtime()
        with pytest.raises(StripeCheckoutError): await executor.start(session, auth, 'license:' + auth.invoice_id)
    asyncio.run(go())
    assert factory.page.clicks == 0

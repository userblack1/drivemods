"""An owner's card checkout through official Stripe.js; never a paid-status oracle.

Construction creates no files or browser. Provider readiness may briefly
validate its encrypted card locally; a browser probe receives no card data.
An attempted submission is durably reserved and is never automatically repeated.
"""
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


_ORIGIN = 'https://drivemods-owner-checkout.invalid/'
_LIMITS = {'full': 9000, 'eco': 6000}


class StripeCheckoutError(RuntimeError):
    pass


class _PlaywrightFactory:
    def __init__(self, executable):
        self.executable = Path(executable) if executable else None

    @property
    def available(self):
        try:
            return bool(self.executable and self.executable.is_file()
                        and os.access(self.executable, os.X_OK)
                        and importlib.util.find_spec('playwright.async_api'))
        except (ImportError, ValueError, OSError):
            return False

    @asynccontextmanager
    async def __call__(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, executable_path=str(self.executable),
                                                     chromium_sandbox=True)
            try:
                # No persistent profile, downloads, tracing, HAR or screenshots.
                context = await browser.new_context(accept_downloads=False, locale='en-US')
                try:
                    yield await context.new_page()
                finally:
                    await context.close()
            finally:
                await browser.close()


async def probe_browser_runtime(executable):
    """Explicit blank-browser check, independent of any card or payment setup."""
    factory=_PlaywrightFactory(executable)
    if not factory.available:
        return False
    try:
        async with factory() as page:
            await page.goto('about:blank',wait_until='domcontentloaded',timeout=60000)
        return True
    except Exception:
        return False


def _script_value(value):
    return json.dumps(value).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _card_valid(card, authorized_last4):
    pan, expiry, cvc, name = card.pan, card.expiry, card.cvc, card.name
    if (authorized_last4 != '6994' or card.last4 != authorized_last4
            or not isinstance(pan, str) or not re.fullmatch(r'[0-9]{13,19}', pan)
            or not pan.startswith('4')
            or pan[-4:] != authorized_last4 or not re.fullmatch(r'(0[1-9]|1[0-2])/[0-9]{2}', expiry)
            or not isinstance(cvc, str) or not re.fullmatch(r'[0-9]{3}', cvc)
            or not isinstance(name, str) or not 1 <= len(name) <= 100
            or any(ord(c) < 32 for c in name)):
        raise StripeCheckoutError('Authorized owner card is unavailable or mismatched')
    digits = [int(x) for x in pan]
    total = sum((n * 2 - 9 if n > 4 else n * 2) if (len(digits) - i) % 2 == 0 else n
                for i, n in enumerate(digits))
    if total % 10:
        raise StripeCheckoutError('Authorized owner card failed local validation')
    now = datetime.now(timezone.utc)
    if (2000 + int(expiry[3:]), int(expiry[:2])) < (now.year, now.month):
        raise StripeCheckoutError('Authorized owner card has expired')


def verify_pay_label(body, label, cents):
    amount = f'{cents // 100}.{cents % 100:02d}'
    body, label = ' '.join(body.split()), ' '.join(label.split())
    # The actual submission label must have the exact final authorized amount.
    # USD must also be explicit in the visible checkout, never inferred from $.
    if not re.search(r'\bUSD\b|US\$', body):
        raise StripeCheckoutError('Stripe currency is not visibly verified as USD')
    forms = (f'Pay US${amount}', f'Pay ${amount}', f'Pay {amount} USD', f'Pay US$ {amount}',
             f'Pay ${amount} USD')
    if label not in forms:
        raise StripeCheckoutError('Stripe final amount or currency does not match authorization')


def verify_usd_option(label, cents):
    """Exact observed Stripe USD country/currency option, including ru commas."""
    if not isinstance(label, str):
        raise StripeCheckoutError('Stripe USD option is not readable')
    label = ' '.join(label.split())
    patterns = (r'US\s+(?:US\$|\$)\s*([0-9]+[.,][0-9]{2})',
                r'US\s+([0-9]+[.,][0-9]{2})\s*\$',
                r'USD\s+(?:US\$|\$)?\s*([0-9]+[.,][0-9]{2})')
    matches = [re.fullmatch(pattern, label) for pattern in patterns]
    values = [match[1].replace(',', '.') for match in matches if match]
    if values != [f'{cents // 100}.{cents % 100:02d}']:
        raise StripeCheckoutError('Stripe USD option amount does not match authorization')


def valid_frame_origin(value):
    try:
        source = urlsplit(value or '')
        if (source.scheme != 'https' or source.username or source.password
                or source.port not in (None, 443)):
            return False
        return (source.hostname == 'checkout.stripe.com' or source.hostname == 'js.stripe.com'
                and re.fullmatch(r'/v3/embedded-checkout-inner-[A-Za-z0-9_-]+\.html', source.path) is not None)
    except (ValueError, TypeError):
        return False


async def _option_label(locator):
    # Stripe's flag contributes its alt text (US) to the accessible name, while
    # label.textContent can contain only the amount. Use the browser's computed
    # accessibility name rather than reconstructing it from partial text.
    snapshot = await locator.aria_snapshot()
    match = re.match(r'^\s*-\s+checkbox\s+("(?:[^"\\]|\\.)*")', snapshot)
    if not match:
        raise StripeCheckoutError('Stripe USD accessible label is not verified')
    try:
        return json.loads(match[1])
    except (ValueError, TypeError):
        raise StripeCheckoutError('Stripe USD accessible label is not readable') from None


async def _usd_selected(frame, cents, *, select=False):
    usd = frame.get_by_role('checkbox', name=re.compile(r'^(?:US\s|USD\b|Pay in USD\b)'))
    count = await usd.count()
    if count == 0:
        return False
    if count != 1:
        raise StripeCheckoutError('Stripe USD currency option is ambiguous')
    verify_usd_option(await _option_label(usd), cents)
    if select and not await usd.is_checked():
        await usd.check()
    if not await usd.is_checked():
        raise StripeCheckoutError('Stripe USD currency option is not selected')
    kg = frame.get_by_role('checkbox', name=re.compile(r'^(?:KG\s|KGS\b)'))
    kg_count = await kg.count()
    if kg_count > 1 or kg_count == 1 and await kg.is_checked():
        raise StripeCheckoutError('Stripe local currency remains selected')
    return True


async def _billing_country(frame, expected, *, select=False):
    country = frame.locator('#billingCountry')
    if await country.count() != 1 or await country.evaluate('el => el.tagName') != 'SELECT':
        raise StripeCheckoutError('Stripe billing country selector is not verified')
    if select:
        await country.select_option(expected)
    if await country.input_value() != expected:
        raise StripeCheckoutError('Stripe billing country does not match the owner card')


class BrowserStripeExecutor:
    def __init__(self, card_provider, publishable_key, journal_path, *, browser_factory=None,
                 browser_executable=None, timeout_ms=60000, billing_country='KG'):
        self._provider, self._key = card_provider, publishable_key
        self._path = Path(journal_path)
        self._factory = browser_factory or _PlaywrightFactory(browser_executable)
        self._runtime_verified = False
        if not isinstance(billing_country, str) or not re.fullmatch(r'[A-Z]{2}', billing_country):
            raise ValueError('Invalid explicit owner billing country')
        self._billing_country = billing_country
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 180000:
            raise ValueError('Invalid Stripe browser timeout')
        self._timeout = timeout_ms

    def __repr__(self):
        return 'BrowserStripeExecutor(card=[REDACTED], checkout=official_Stripe_js)'

    @property
    def configured(self):
        try:
            return (self._provider.configured is True and isinstance(self._key, str)
                    and re.fullmatch(r'pk_live_[A-Za-z0-9]{20,}', self._key) is not None
                    # Playwright debug accepts wildcards such as '*' and 'pw*'
                    # and can log fill() values. Any debug setting is unsafe
                    # around card input, including non-Playwright patterns.
                    and not os.getenv('PWDEBUG') and not os.getenv('DEBUG')
                    and self._factory.available is True)
        except Exception:
            return False

    @property
    def automatic_payment_ready(self):
        return self.configured and self._runtime_verified

    async def probe_runtime(self):
        self._runtime_verified = False
        if not self.configured:
            return False
        try:
            async with self._factory() as page:
                await page.goto('about:blank', wait_until='domcontentloaded', timeout=self._timeout)
            self._runtime_verified = True
            return True
        except Exception:
            return False

    async def close(self):
        self._runtime_verified = False

    def _reserve(self, key, binding, session_sha):
        if any(path.is_symlink() for path in (self._path, *self._path.parents)):
            raise StripeCheckoutError('Unsafe Stripe journal path')
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self._path, timeout=10)) as db:
            if os.name != 'nt': self._path.chmod(0o600)
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS stripe_attempts('
                       'key TEXT PRIMARY KEY,binding_sha TEXT NOT NULL,phase TEXT NOT NULL,'
                       'session_sha TEXT NOT NULL UNIQUE)')
            if {row[1] for row in db.execute('PRAGMA table_info(stripe_attempts)')} != {
                    'key', 'binding_sha', 'phase', 'session_sha'}:
                raise StripeCheckoutError('Legacy Stripe attempt journal needs explicit reconciliation')
            previous = db.execute('SELECT binding_sha,phase FROM stripe_attempts WHERE key=?', (key,)).fetchone()
            if previous:
                db.rollback()
                if previous[0] != binding:
                    raise StripeCheckoutError('Stripe attempt identity changed')
                return False
            if db.execute('SELECT 1 FROM stripe_attempts WHERE session_sha=?', (session_sha,)).fetchone():
                raise StripeCheckoutError('Stripe session is already reserved for another invoice')
            db.execute("INSERT INTO stripe_attempts VALUES(?,?,'reserved',?)", (key, binding, session_sha))
            db.commit()
            return True

    def _phase(self, key, before, after):
        with closing(sqlite3.connect(self._path, timeout=10)) as db:
            with db:
                changed = db.execute('UPDATE stripe_attempts SET phase=? WHERE key=? AND phase=?',
                                     (after, key, before)).rowcount
                if changed != 1:
                    raise StripeCheckoutError('Stripe attempt is reserved by another operation')

    def _validate(self, session, authorization, key):
        if (not re.fullmatch(r'[0-9a-f]{32}', authorization.invoice_id)
                or key != 'license:' + authorization.invoice_id
                or authorization.plan not in _LIMITS or authorization.currency != 'USD'
                or type(authorization.max_cents) is not int or authorization.max_cents != _LIMITS[authorization.plan]
                or session.currency != 'USD' or type(session.total_cents) is not int
                or session.total_cents != authorization.max_cents
                or not authorization.license_id or not authorization.payment_id
                or session.payment_id != authorization.payment_id or authorization.card_last4 != '6994'
                or not re.fullmatch(r'cs_live_[A-Za-z0-9]+', session.session_id)
                or not re.fullmatch(re.escape(session.session_id) + r'_secret_[A-Za-z0-9]+', session.client_secret)):
            raise StripeCheckoutError('Stripe session does not match the authorized invoice and limit')

    async def start(self, session, authorization, idempotency_key):
        self._validate(session, authorization, idempotency_key)
        if not self.automatic_payment_ready:
            raise StripeCheckoutError('Owner card or browser runtime is not configured')
        binding = hashlib.sha256(json.dumps({
            'invoice': authorization.invoice_id, 'license': authorization.license_id,
            'payment': authorization.payment_id, 'plan': authorization.plan, 'amount': session.total_cents,
            'session': session.session_id, 'currency': session.currency,
            'secret_sha': hashlib.sha256(session.client_secret.encode()).hexdigest(),
        }, sort_keys=True).encode()).hexdigest()
        if not self._reserve(idempotency_key, binding, hashlib.sha256(session.session_id.encode()).hexdigest()):
            return 'action_required'  # Reconcile upstream; never retry a reserved checkout.
        card = None
        try:
            card = self._provider.load()
            _card_valid(card, authorization.card_last4)
            html = ('<!doctype html><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
                    '<div id="checkout"></div>'
                    '<script src="https://js.stripe.com/v3/"></script><script>'
                    'window.checkoutCompleted=false;'
                    '(async()=>{const stripe=Stripe(' + _script_value(self._key) + ',{locale:"en"});'
                    'const checkout=await stripe.initEmbeddedCheckout({clientSecret:'
                    + _script_value(session.client_secret)
                    + ',onComplete:()=>{window.checkoutCompleted=true;}});checkout.mount("#checkout");})();'
                    '</script>')
            async with self._factory() as page:
                await page.route(_ORIGIN, lambda route: route.fulfill(status=200, content_type='text/html', body=html))
                await page.goto(_ORIGIN, wait_until='domcontentloaded', timeout=self._timeout)
                outer_frame = page.locator('iframe[title="Embedded checkout"]')
                await outer_frame.wait_for(state='visible', timeout=self._timeout)
                if not valid_frame_origin(await outer_frame.get_attribute('src')):
                    raise StripeCheckoutError('Embedded payment frame origin is not verified as Stripe')
                frame = page.frame_locator('iframe[title="Embedded checkout"]')
                number = frame.locator('#cardNumber')
                await number.wait_for(state='visible', timeout=self._timeout)
                await _billing_country(frame, self._billing_country, select=True)
                await _usd_selected(frame, session.total_cents, select=True)
                await number.fill(card.pan)
                await frame.locator('#cardExpiry').fill(card.expiry)
                await frame.locator('#cardCvc').fill(card.cvc)
                await frame.locator('#billingName').fill(card.name)
                pay = frame.get_by_role('button', name=re.compile(r'^(?:Pay|Оплатить)(?:\s|$)'))
                if await pay.count() != 1:
                    raise StripeCheckoutError('Stripe payment button is not uniquely verified')
                body = await frame.locator('body').inner_text()
                label = ' '.join((await pay.inner_text()).split())
                # Actual Embedded Checkout has a plain Pay/Оплатить button.
                # It is accepted only with separately observed exact selected
                # USD option. Re-read after filling in case the UI changed.
                selected_usd = await _usd_selected(frame, session.total_cents)
                if label in {'Pay', 'Оплатить'}:
                    if not selected_usd:
                        raise StripeCheckoutError('Plain payment button lacks exact selected USD proof')
                else:
                    verify_pay_label(body, label, session.total_cents)
                if not await pay.is_enabled():
                    raise StripeCheckoutError('Stripe payment form is not complete')
                await _billing_country(frame, self._billing_country)
                # Persist before the only financial click, not after its response.
                self._phase(idempotency_key, 'reserved', 'submitting')
                await pay.click(timeout=self._timeout)
                self._phase(idempotency_key, 'submitting', 'submitted')
                try:
                    await page.wait_for_function('window.checkoutCompleted === true || '
                        'Array.from(document.querySelectorAll("iframe")).some(x=>'
                        '/3ds|challenge|authentication/i.test(x.title))', timeout=self._timeout)
                except Exception:
                    return 'action_required'
                if await page.evaluate('window.checkoutCompleted === true'):
                    return 'pending'  # Only authenticated supplier records prove paid.
                return 'action_required'  # Never inspect or fill OTP/challenge fields.
        except StripeCheckoutError:
            raise
        except Exception:
            raise StripeCheckoutError('Stripe browser execution is uncertain; reconcile before further action') from None
        finally:
            if card is not None:
                card.clear()
            clear = getattr(self._provider, 'clear', None)
            if clear:
                clear()

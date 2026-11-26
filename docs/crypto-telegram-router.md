# Prepared crypto Telegram checkout

`crypto_checkout.bot` is a new, separate router and runner. It has not replaced
the running operator license bot. Importing this module starts no poller and
reads no credentials. The current merchant integration does not yet prove
unattended card payment or customer-device binding, so customer invoice issuance
and the new bot runner must remain blocked.

## Customer flow after readiness is verified

The router accepts only private messages where the Telegram sender ID equals
the private chat ID. `/start` explains the file, package and payment flow.

1. The customer sends an original Device ID file (`.bin` or `.id`, maximum
   1 MiB). The file stays local at this step; no merchant upload occurs.
2. The router offers FULL with a base price of 100 USDT and ECO with a base
   price of 70 USDT. It explicitly explains the additional 0.01–0.99 USDT used
   to distinguish transfers to the shared Binance address.
3. Selecting a package calls `CheckoutService.choose` with the authenticated
   customer's ID. The service checks ownership, current readiness, chain anchor
   and durable invoice identity. Repeated callbacks return the same invoice.
4. The invoice displays the exact payable USDT amount, TRON/TRC20 network,
   receiving address, expiry in UTC and `/status` command. It does not claim
   a wallet QR pre-fills an amount; this router currently supplies no QR.
5. Independent chain and Binance confirmation advances the durable order.
   A photo, receipt or statement that the customer paid never changes payment
   state. Purchase and file delivery belong to `RuntimeWorker` and `Engine`.
6. `/orders` shows only that customer's latest ten orders. `/status ID` shows
   only an owned order; `/status` selects the latest owned order. Expired
   invoices are displayed as expired. If readiness is lost, payment addresses
   are no longer redisplayed as payable.

Device filenames never become filesystem destinations. `CheckoutService`
allocates a random upload directory and fixed `device.id.bin` name. Download
size is bounded while streaming even if file metadata understates it. Visible
PDF/image receipt files are rejected as device IDs. Repeated Telegram messages
have a durable per-user message key; a different file cannot silently replace
an existing upload identity. These checks do not certify device compatibility;
the merchant still needs to verify the exact paid device and requested package.

## Explicit startup boundary

The caller constructs the real `CheckoutService`, `RuntimeWorker`, verified
merchant adapter/executor and Telegram delivery client. It may then call:

```python
await crypto_checkout.bot.run(
    checkout,
    worker,
    token=private_bot_token,
    expected_bot_id=9258163194,
    expected_username='DriveMods_licensebot',
    exclusive_poller_confirmed=True,
    stop=shutdown_event,
)
```

This is an integration example, not a command to launch the prepared bot now.
Keep real credentials in protected environment files outside Git. There is no
dynamic factory import or automatic startup based on environment flags here.

Before constructing `Bot` or making a token probe, the runner requires shared
live ledger identity, full worker readiness, enabled unique-amount invoices and
explicit confirmation of exclusive poller ownership. Deployment must verify
that the previous service using this token is stopped before setting
`exclusive_poller_confirmed=True`. An empty webhook response alone cannot prove
another long-polling instance is absent. The current services must stay running
until a separately reviewed, ready cutover is performed.

After the gate passes, `getMe` must match the expected bot ID and username. An
existing webhook blocks startup; it is never deleted automatically. The router
is registered once. The worker must acquire its OS lock and finish recovery
before `worker.started` is signaled and the single Telegram poller starts.
Worker, poller and shutdown signal share one lifecycle. Shutdown stops polling,
drains/cancels current worker jobs safely, releases the lock and closes the bot
session. No additional `getUpdates` poller runs inside delivery or the worker.

## Verification

```sh
python -m pytest tests/test_crypto_bot.py tests/test_crypto_runtime.py -q
```

Tests drive the actual aiogram dispatcher with an offline recording session:
callback ownership, group/foreign-chat rejection, exact and repeated invoices,
file path and streaming size limits, receipt rejection, private status,
readiness loss, expiry, bot identity/webhook checks, explicit poller ownership
and acquisition of the worker lock before polling. They do not send real
messages or start a production poller.

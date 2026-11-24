# Payment and license worker

`crypto_checkout.runtime` provides an explicit worker for the isolated crypto
ledger. Importing it starts no tasks and loads no credentials. It does not
replace or restart either existing license bot. The caller constructs the
verified chain, exchange, store and Telegram adapters and starts one worker.

## Components

- `ConcurrentWatcher(ledger, tron, exchange, concurrency=4)` verifies chain
  receipts using bounded parallel jobs. Shared receiving-address history and
  transaction receipts are deduplicated within a scan. A failed history,
  receipt or exchange read is isolated and retried by a later scan. Known
  `chain_confirmed` transactions do not depend on indexer history remaining
  available. All credit approval still goes through the ledger's exact
  address, amount, time, block and unique-deposit checks.
- `RateLimitedExchange(reader, minimum_interval=1)` serializes authenticated
  deposit reads, with at least one second between request starts by default.
  `ConcurrentWatcher` automatically wraps an unwrapped exchange adapter.
  The Binance reader still independently checks its permissions.
- `TelegramDelivery(token)` uploads the validated output file with the
  official Bot API `sendDocument` method. Its only destination is the
  positive private `user_id` stored in that exact invoice. It checks the
  output path and SHA-256 again, confirms the returned chat and document,
  hides token URLs in errors and the `httpx` INFO logger, and respects a valid
  Telegram `retry_after` response before the next delivery attempt. It never
  starts another `getUpdates` poller. Supply the token privately; do not put
  it in commands or tracked files.
- `RuntimeWorker(ledger, watcher, engine)` independently schedules payment
  scans and fulfillment. An unfinished store call does not block chain
  scanning or another customer's already prepared file delivery. Card/store
  operations use one durable supplier slot; downloads and delivery have a
  separate bounded pool. Duplicate scheduling of an active invoice is suppressed.

## Supplier's one-unpaid-order quota

The supplier account currently permits only one unpaid license. Serializing
individual API calls is insufficient: another customer's preparation must not
run between the first customer's preparation and its payment confirmation.
Before scheduling a purchase job, the worker atomically reserves
`supplier_slot_invoice` in the existing ledger metadata and appends an audit
event. Only this invoice can advance supplier preparation/payment. Other paid
customers remain in the durable FIFO queue rather than becoming tasks waiting
behind an asyncio lock.

An already unfinished preparation, inspection, charge, supplier wait,
authentication challenge or purchase review has priority over an unstarted
paid invoice. Ownership survives process restarts and scheduling rounds. A
missing supplier reference after an uncertain write still holds the slot.
The slot releases only after the owner reaches a post-payment state
(`download_pending` or later) with its recorded store payment reference. The
release is audited. A lost owner or a post-payment state missing payment proof
blocks new supplier creation safely; neither is silently reassigned.

After that proof, the next customer may begin its supplier purchase while the
previous customer's download or Telegram delivery continues. Receipt scanning
and already verified file delivery remain independent even if a supplier slot
is waiting on 3DS or an uncertain operation. A challenge/review can therefore
delay later purchases until resolved, as required by the supplier's quota; it
does not authorize another charge or an additional unpaid supplier license.
The reservation belongs to this ledger and worker. A separate application
using the same dealer account must not bypass the shared supplier limit.

## Production gate

`ProductionReadiness.inspect(ledger, store, delivery)` reports structured
prerequisites. A live purchase worker refuses startup unless all are verified:

- Real, separate ledger with mandatory exchange credit verification.
- Real store adapter with explicit `automatic_payment_ready is True` and
  `merchant_auth_verified is True`, plus independently verified
  `merchant_device_binding_verified is True`.
- Real delivery adapter.
- Explicit `engine.live_purchase_enabled` configuration.

Those attributes are an integration gate, not proof that the merchant offers
unattended card charging. They must only be set after the actual merchant
checkout and payment mechanism have been verified. A saved Visa/Link card
alone does not satisfy that requirement. The unconfigured store cannot pass
this gate. There is no fallback to simulated purchases or generated license
placeholders.

## Crash and retry behavior

Before recovery, `RuntimeWorker.run()` acquires a nonblocking OS file lock
next to the ledger (`*.worker.lock` by default). Another process cannot run
recovery or purchases for that ledger while the first worker holds the lock.
The lock file remains after shutdown and must not be manually deleted while
a worker runs. The caller must use the same ledger and lock path for every
worker instance; independently calling `Engine.step` bypasses this runner's
purchase serialization and is not an alternative deployment mode.

The engine reserves external operations in SQLite before I/O. On restart,
ambiguous preparation, inspection or charging states become `purchase_review`.
The worker only calls read-only reconciliation for a review with a persisted
store reference. It never repeats an uncertain charge. A review without a
reference remains unresolved. Store authentication challenges are retained
as `action_required`, and an invoice returning to unpaid after an attempted
charge is not charged again. A Telegram outage retries delivery of the same
verified local file; it never purchases another license. If Telegram accepted
a file but its response was lost, redelivery may occur, since `sendDocument`
has no client idempotency key.

Individual failed or unchanged jobs have bounded exponential backoff (5 to
300 seconds). On shutdown, current jobs receive a bounded grace period;
remaining jobs are canceled while the process lock is still held. The next
startup reconciles any ambiguous state. Adapter network clients belong to
the caller and should be closed after the worker stops.

## Operational data

Keep ledger, device IDs, license files, lock and `runtime-health.json` inside
an owner-only data directory. Provision Windows ACLs or POSIX service-owner
permissions before startup. Runtime uses a private POSIX umask. Health output
contains readiness flags, safe error types and job counts, without tokens,
user IDs, file paths, addresses or transaction details.

The present shared-address provider has a lifetime limit of 198 distinct
invoice amounts across the two plans. Supplier-slot selection examines all
relevant rows so an older unresolved reservation cannot be hidden behind a
batch limit. A future provider with more addresses/invoices needs an
independently reviewed paging/fairness policy before raising that capacity.

## Verification

```sh
python -m pytest tests/test_crypto_runtime.py -q
```

Tests use explicit mock transports and labeled simulation adapters. They
exercise a real cross-process lock, concurrent verification bounds, serialized
card operations under an enforced one-unpaid-order supplier quota, independent verification/delivery while another purchase
waits, uncertain-charge reconciliation, delivery-only retry, and private
Telegram destination and token-log protection. They do not prove that real
DriveMods unattended checkout or real client payments have been connected.

Telegram transport contract: official [Bot API, sendDocument](https://core.telegram.org/bots/api#senddocument),
checked 2026-11-24.

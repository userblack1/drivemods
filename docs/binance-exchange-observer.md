# Binance deposit observer

This isolated component reads Binance's authenticated deposit history. It does
not import the order ledger, Telegram bot, DriveMods shop, or card automation.
It cannot approve a client's order or purchase a license. `available_deposits`
is an exchange observation count, not a count of verified client orders.

## Configuration and launch

Provide these variables privately through a root-owned service environment
file, never through command arguments or Git:

- `BINANCE_READ_API_KEY`
- `BINANCE_READ_API_SECRET`
- `CRYPTO_RECEIVING_ADDRESS` (the owner's USDT TRC20 deposit address)
- `CRYPTO_EXCHANGE_DATA_DIR` (a dedicated, private writable directory)

Use a Binance key restricted to the server's public IP. Reading must be enabled;
trading, futures, transfers and withdrawals must be disabled. The reader checks
the key's permissions before each complete history-window request and refuses
to read deposits if that check fails or a required permission flag is unknown.

Run a connectivity check using the deployment's Python environment:

```sh
python -m crypto_checkout.exchange_observer --once
```

Run continuously with the same command without `--once`. Default polling is
30 seconds, with bounded backoff to five minutes after failure. No bot should
be restarted to run this observer. The observer does not load `.env` itself.
No deployment is performed by adding this module.

## Data and failure behavior

The private data directory contains `exchange-observations.sqlite3` and
`exchange-health.json`. A private umask is set at runtime. Provision the
directory and environment file with service-owner-only permissions before use.
SQLite stores only normalized matching USDT/TRX deposits to the configured
address: deposit ID, transaction ID, amount in millionths of USDT, insertion
time, status, compliance status and last observation time. Health contains
counts and safe status fields, without transaction IDs, address, amounts or
credentials. Exceptions are logged by type, never response body or signed URL.

The initial persisted timestamp is the first time this data directory starts.
Older deposits are intentionally not read. Every sweep scans from that timestamp
to a frozen sweep end, using windows shorter than 90 days. Its cursor advances
atomically only after all pages of a window have been read and validated. The
next sweep revisits old pending deposits, so a late successful credit is not
dropped by a moving lookback cutoff. Long sweeps continue from the persisted
cursor after a restart. Pagination uses the documented `offset` and `limit`;
repeated pages, ambiguous IDs and page-limit exhaustion fail the window rather
than declare an incomplete scan successful.

Only integer status `1` plus integer `travelRuleStatus=0` is counted as available.
Status `6` (credited but restricted), pending/rejected/wrong-deposit statuses,
missing compliance status and malformed data never count as available. A row
may update its status, but its transaction, amount and insertion time cannot
silently change. Deposit IDs and chain transaction IDs must both be unique.
No raw responses or unrelated-network deposits are persisted.

The final license-purchase pipeline still needs a separate durable association
between each invoice and its independently verified chain event and exchange
credit. Enabling this observer alone does not enable client checkout.

API contract checked against the official [Binance Capital API documentation](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/capital)
on 2026-11-24. Real connectivity and the actual account's permission response
must be verified separately; automated tests use explicit mock transports.

## Verification

```sh
python -m pytest tests/test_crypto_exchange_observer.py -q
```

Checks cover read-only preflight, complete pagination, repeated/truncated-page
rejection, exact amounts and statuses, durable window checkpoints, pending
credit updates, immutable identities, transaction uniqueness, atomic rollback
and health-output privacy.

## Verified deployment — 24 November 2026

The owner's server runs `drivemods-binance-observer.service` from
`/opt/drivemods-binance-observer`. Its environment file is
`/etc/drivemods-binance-observer.env` (root:root, mode 600); its dedicated
unprivileged account and private data directory are `drivemods-binance` and
`/var/lib/drivemods-binance-observer`. The service unit is supplied in
`deploy/drivemods-binance-observer.service`. Secrets are absent from this repo.

Live authenticated calls from the server verified `ipRestrict=true`, reading
enabled and all trading/transfer/withdrawal permissions disabled. The initial
complete history scan succeeded; no matching deposits had arrived since the
observer's first start. After the owner's IP-restriction confirmation, a fresh
continuous-service health report at 16:21:54 UTC was healthy with no error.
One temporary failed read during the restriction change recovered automatically.
The seven existing bot, bank-notice and chain-observer services retained their
PIDs and restart counts throughout this deployment.

This confirms live connectivity and continuous observation only. No real
client crypto invoice, client-payment association, store purchase or license
delivery was exercised. Both license bots remain in operator mode. The new
read-only DriveMods client and its mocked checks are described separately in
[drivemods-parse-observations.md](drivemods-parse-observations.md).

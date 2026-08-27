# TotalPay Payment Provider for Odoo 19

Integrates [TotalPay](https://totalpay.global)'s hosted Checkout (redirect) integration as a
native payment provider in Odoo 19: session creation, server-to-server webhook confirmation,
manual capture (DMS mode), refunds, voids, and a reconciliation cron for transactions that
never receive a callback.

## Requirements

- Odoo 19.0
- A TotalPay merchant account (test and/or live), including:
  - Merchant Key (test and/or live)
  - Hashing Password
  - API Base URL
- A publicly reachable HTTPS domain for your Odoo instance, so TotalPay's server-to-server
  webhook can reach you (see [Webhook setup](#webhook-setup) below)

## Installation

1. Copy this module into your Odoo `addons_path`
2. Restart the Odoo service
3. Apps → search "TotalPay" → Install

## Configuration

Go to **Settings → Payment Providers → TotalPay** and fill in the **Credentials** tab:

| Field | What to enter |
|---|---|
| TotalPay Merchant Key - Live | Your production merchant key (only required once you switch to Enabled/live mode) |
| TotalPay Merchant Key - Test | Your sandbox merchant key (only required while in Test Mode) |
| TotalPay Hashing Password | Secret password TotalPay assigned for computing hash signatures |
| Hash Mode | MD5 (default) or SHA256 — must match your TotalPay account's configuration |
| TotalPay API Base URL | The API host TotalPay gave you, e.g. `https://checkout.totalpay.global` |
| TotalPay Webhook URL | **Read-only, auto-computed.** Copy this value and give it to TotalPay as your `notification_url` — see below |

Only the merchant key matching your current **State** (Test Mode vs Enabled) is required —
you don't need both filled in at once.

## Webhook setup

1. Set `web.base.url` (Settings → Technical → Parameters → System Parameters) to your real,
   public HTTPS domain.
2. Open **Settings → Payment Providers → TotalPay** and copy the value shown in the
   **TotalPay Webhook URL** field.
3. Give that URL to TotalPay (via their merchant admin panel or your account manager) as the
   `notification_url` for the corresponding merchant key (test or live).

## Features

- Hosted checkout redirect (session creation)
- Server-to-server webhook confirmation (authoritative), with:
  - Signature verification
  - Idempotency / duplicate-event protection
  - A callback matrix covering `type` + `status` + `order_status`, not just a naive status check
- Browser-return fallback confirmation (for when the webhook is delayed or the customer's
  browser is the first thing to respond)
- A 5-minute reconciliation cron as a safety net for any transaction that never receives a
  callback at all
- Capture, refund, and void, wired to TotalPay's actual API endpoints

## Known limitations

- **Refund attribution**: TotalPay's `type=refund` webhook echoes the *original* transaction's
  identifiers, not a refund-specific one. If you issue more than one overlapping partial refund
  on the same transaction before the first one's callback arrives, the module can't always tell
  them apart and resolves the oldest pending one. Sequential refunds (wait for one to confirm
  before issuing the next) are unaffected.
- **Currency restriction**: this module does not restrict which currencies show TotalPay as an
  option at checkout — TotalPay's own API enforces currency validity against your merchant
  account, so an unsupported currency fails at checkout rather than being hidden in advance.

## Documentation

See [`TotalPay_Module_Documentation.md`](./TotalPay_Module_Documentation.md) for a full
plain-language walkthrough of the payment flow, file-by-file reference, and a troubleshooting
checklist.

## License

LGPL-3
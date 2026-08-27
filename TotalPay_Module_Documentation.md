# TotalPay Module — How It Works

## 1. Overview

When a customer pays through TotalPay, three things can independently confirm the payment
back to Odoo:

1. **The webhook** — TotalPay's server calls your server directly. Fast, and the authoritative
   source of truth. Requires your server to be publicly reachable over HTTPS.
2. **The browser return** — the customer's browser gets redirected back to your site after
   paying. Always fires if the customer stays on the page, but does not carry a fully trusted
   payload on its own — it triggers a status check instead of being applied directly.
3. **The reconciliation cron** — a background job that checks every 5 minutes for any payment
   that's still stuck, and asks TotalPay directly "what's the real status of this?" This is
   the safety net for when #1 and #2 both fail to happen (e.g. the webhook never arrives and
   the customer closes the tab before being redirected back).

All three paths funnel into the same underlying logic, so no matter which one succeeds first,
the result is the same: the transaction gets marked `done` and the invoice/order gets marked
paid.

---

## 2. Webhook setup

1. Set `web.base.url` (Settings → Technical → Parameters → System Parameters) to your real
   public HTTPS domain.
2. Open Settings → Payment Providers → TotalPay and copy the value shown in the **TotalPay
   Webhook URL** field — it's read-only and computed automatically from `web.base.url`.
3. Give that exact URL to TotalPay (their merchant admin panel, or your account manager) as
   the `notification_url` for the relevant merchant key (test or live — these are configured
   separately on TotalPay's side).
4. Do one real test payment and check your Odoo server log for:
   ```
   TotalPay: webhook received for order_number=..., type=..., status=...
   ```
   If you see it, the webhook is genuinely working. TotalPay requires a valid HTTPS
   certificate — it will not deliver to plain HTTP or a self-signed certificate.

---

## 3. Step-by-step: what happens when a customer pays

### Step 1 — Customer clicks "Pay"
Odoo creates a `payment.transaction` record in the `draft` state.

### Step 2 — Odoo asks TotalPay to open a checkout session
Odoo sends TotalPay the order number, amount, currency, and a description, all signed with a
hash. TotalPay responds with a `redirect_url`.

*(The order number sent to TotalPay is a cleaned-up version of Odoo's reference — slashes and
special characters replaced with dashes, since TotalPay's status-lookup endpoint rejects them
even though session creation itself accepts them. This mapping is stored on the transaction so
Odoo can match things back up correctly later.)*

### Step 3 — Customer is redirected to TotalPay's hosted payment page
Odoo has no visibility into what happens here (card entry, 3D Secure, etc.) until TotalPay
reports back.

### Step 4 — Customer finishes paying (or cancels)
TotalPay redirects the customer's browser back to one of four URLs:
- `/payment/totalpay/return` — payment attempt finished (success or otherwise)
- `/payment/totalpay/cancel` — customer canceled
- `/payment/totalpay/expired` — session timed out
- `/payment/totalpay/error` — a technical error occurred on TotalPay's side

Landing on any of these does **not** by itself mean the payment succeeded or failed — the
actual confirmation comes from Step 5.

### Step 5 — Confirmation (via whichever path succeeds first)

**Path A — Webhook (authoritative):**
TotalPay sends a direct, signed POST to `/payment/totalpay/webhook`. Note: despite TotalPay's
own documentation describing this as form-encoded, in practice it is sent as
**`Content-Type: application/json`**. The controller parses the raw request body as JSON as a
fallback when Odoo's automatic form-decoding finds nothing, so this is handled transparently —
worth knowing if you're debugging or extending this controller. Odoo verifies the signature,
checks it isn't a duplicate of something already processed, then applies the result per the
callback matrix below.

**Path B — Browser return fallback:**
Odoo doesn't trust the browser-return payload alone — it makes its own outbound call to
TotalPay's status endpoint and applies whatever TotalPay reports.

**Path C — Reconciliation cron (safety net):**
Every 5 minutes, checks any TotalPay transaction still sitting in `draft`, `pending`, or
`authorized` for more than 10 minutes, and does the same outbound status check as Path B. This
only checks *top-level* transactions — refund/capture child transactions are excluded, since
they don't have an independent order at TotalPay to poll (see Section 6).

### Step 6 — Transaction marked `done`, order/invoice marked paid
Once any path confirms success, Odoo marks the transaction `done`, immediately triggers Odoo's
built-in payment post-processing job, which creates the reconciled `account.payment` record and
marks the invoice/order paid.

---

## 4. The callback matrix — `type`, `status`, and `order_status`

A webhook payload always carries three relevant fields, and all three matter:

- **`type`** — what kind of event this is: `sale`, `capture`, `refund`, `void`, `3ds`,
  `chargeback`, `reversal`, etc.
- **`status`** — whether *this specific event* succeeded: `success`, `fail`, `waiting`.
- **`order_status`** — the *order's overall lifecycle state* at this moment, not specific to
  this one event.

### Important discovery from live testing: `order_status` is contextual, not fixed per event

The documentation implies each event type maps to one fixed `order_status` value. In practice,
`order_status` reflects the order's overall remaining balance, which varies by scenario:

| Event | `order_status` observed | Meaning |
|---|---|---|
| `sale` succeeding fully | `settled` | Order fully paid |
| `capture` succeeding | `settled` | Captured funds now settled |
| **Partial** `refund` | `settled` | Order still has funds captured, just less than before |
| **Full** `refund` | `refund` | Order has nothing left captured on it |
| `void` | `void` | Payment canceled before settlement — the full authorized amount is released, nothing is captured |

This module handles both possible values for a refund event
(`order_status in ('settled', 'refund')`), based on this observed behavior. Void is always a
full cancellation of an authorization before it settles — there is no partial void — so it only
ever needs to check for `order_status == 'void'`.

---

## 5. Refunds, Captures, and Voids

- **Refund**: clicking "Refund" in Odoo (or calling `tx._refund(amount_to_refund=...)`) creates
  a linked child transaction representing the refund, and sends the request to TotalPay
  immediately. The child starts in `pending` and is finalized to `done` by the asynchronous
  `type=refund` webhook.
  - **Known limitation**: TotalPay's refund callback echoes the *original* transaction's `id`/
    `order_number`, not a refund-specific identifier. If more than one partial refund is issued
    on the same transaction before the first one's callback arrives, the module resolves the
    oldest still-pending refund child — this can misattribute the callback if refunds
    genuinely overlapped. Sequential refunds (confirm one before issuing the next) are
    unaffected.
- **Capture** (relevant only when "Capture Manually" / DMS mode is enabled): acts directly on
  the original transaction, confirming the authorization and taking the funds.
- **Void**: acts directly on the original transaction, canceling an authorization that was
  never captured.

## 6. Reconciliation cron details

The 5-minute cron only checks top-level transactions — it explicitly excludes any transaction
that has a `source_transaction_id` set (i.e. refund/capture child transactions), because those
don't correspond to an independent order at TotalPay and polling their "order status" returns
`"Order does not exist"`. Refund/capture outcomes are only ever resolved by their own
asynchronous webhook, not by this cron.

---

## 7. Duplicate protection

Every incoming webhook event is checked against the last one already applied to that specific
transaction — an exact duplicate `(id, type, status)` combination is silently ignored. Every
state change also first checks the transaction's *current* state before applying, so an old,
duplicate, or out-of-order event can never overwrite a more recent, more final result.

---

## 8. Key files, in plain terms

| File | What it does |
|---|---|
| `models/api.py` | All actual network calls to TotalPay, and the hash/signature math |
| `models/payment_provider.py` | Stores TotalPay credentials/settings (merchant keys, API URL, webhook URL display, etc.) |
| `models/payment_transaction.py` | Core logic — checkout sessions, webhook processing, refunds/captures/voids, reconciliation |
| `controllers/main.py` | The web addresses (`/payment/totalpay/...`) that TotalPay and the customer's browser talk to |
| `data/ir_cron_data.xml` | Registers the 5-minute reconciliation safety-net job |
| `data/payment_provider_data.xml` | Default TotalPay provider/payment-method records, checkout redirect form, logos |
| `views/payment_provider_views.xml` | The settings form for entering credentials |

---

## 9. Quick troubleshooting checklist

| Symptom | Likely cause | Where to look |
|---|---|---|
| Payment stuck on "not processed yet" | Webhook can't reach the server (not publicly reachable, or `notification_url` not yet configured on TotalPay's side) | Run the reconciliation cron manually; confirm the webhook URL was actually given to TotalPay |
| Webhook logs show `order_number=None, type=None, status=None` | Content-Type mismatch — TotalPay sends `application/json`, not form-encoded | Confirm the controller's JSON-body fallback parsing is present |
| "order_id: This value is not correct" | Odoo reference contains `/` | Confirm the order-number sanitization logic is present and being used consistently |
| Refund/void confirmed by TotalPay but Odoo transaction stays `pending` | `order_status` value not in the accepted set for that event | Check the actual raw webhook payload's `order_status` value against the callback matrix in Section 4 |
| New field/method missing after editing a `.py` file | The Odoo *service* process wasn't actually restarted, or a stale compiled `.pyc` is being loaded | Restart via `services.msc` (not just closing a terminal); if the issue persists, delete the matching file in `models/__pycache__/` and restart again |
| A fresh `odoo-bin shell` session shows old code even after confirming the file on disk is correct | You're reusing an existing shell session — Python caches imported modules for the life of that process | Fully `exit()` and start a brand-new shell invocation before re-checking |
| "Define a payment method line" error | `setup_provider()` never ran for this database (only runs automatically on a fresh install, not an upgrade) | Run it manually once via Odoo shell: `from odoo.addons.payment import setup_provider; setup_provider(env, 'totalpay')` |
| Logo not showing | Wrong filename/path, or browser cache | Confirm `static/description/icon.png` exists exactly there; hard-refresh the browser |
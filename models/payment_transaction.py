# -*- coding: utf-8 -*-
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError, UserError

from .api import format_amount, SETTLING_TYPES

_logger = logging.getLogger(__name__)

# How long a totalpay transaction can sit in a non-terminal state before the
# reconciliation cron polls the status API for it. Give the webhook a
# reasonable head start since it's the authoritative, faster path.
RECONCILE_AFTER_MINUTES = 10


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # Dedup key for the last webhook event actually applied to this
    # transaction, per https://docs.totalpay.global/docs/guides/checkout/callbacks
    # ("Deduplicate on (id, type, status), never apply business effects twice").
    totalpay_last_event_key = fields.Char(string="TotalPay Last Webhook Event", copy=False)

    # The `order.number` / `order_id` actually sent to TotalPay. Odoo's own
    # `reference` (e.g. "INV/2026/00001-12") is accepted by session creation
    # but REJECTED by the get-status-by-order_id endpoint ("order_id: This
    # value is not correct") -- TotalPay applies a stricter validator there
    # than on sale/session creation. We send/store a sanitized value instead
    # and map it back to the real transaction in _extract_reference().
    totalpay_order_number = fields.Char(string="TotalPay Order Number", copy=False, index=True)

    def _totalpay_compute_order_number(self):
        """ TotalPay's status-lookup endpoint only reliably accepts
        alphanumerics and dashes in order_id, unlike session creation which
        tolerates '/'. Odoo's default payment references (e.g.
        "INV/2026/00001-12") contain '/', so we sanitize before sending. """
        self.ensure_one()
        return re.sub(r'[^A-Za-z0-9\-]', '-', self.reference)

    # -------------------------------------------------------------------
    # SESSION CREATION (redirect checkout)
    # https://docs.totalpay.global/docs/requests/checkout/sale
    # -------------------------------------------------------------------
    def _get_specific_rendering_values(self, processing_values):
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'totalpay':
            return res

        provider = self.provider_id
        api = provider._totalpay_get_api()
        urls = provider._totalpay_get_urls()

        order_number = self._totalpay_compute_order_number()
        self.totalpay_order_number = order_number

        order_amount = format_amount(self.amount, self.currency_id.name)
        # order_description = (_("Order %s") % self.reference)[:1024]
        store_url = f"{self.get_base_url()}/"
        order_description = (_("Payment Order # %s in the store %s") % (self.reference, store_url))[:1024]

        response = api.create_session(
            order_number=order_number,
            order_amount=order_amount,
            order_currency=self.currency_id.name,
            order_description=order_description,
            success_url=urls['success_url'],
            cancel_url=urls['cancel_url'],
            expiry_url=urls['expiry_url'],
            error_url=urls['error_url'],
            operation='purchase',
            # auth=provider.totalpay_capture_manually,
            custom_data={'odoo_reference': self.reference},
        )

        if response.get('status') == 'error' or not response.get('redirect_url'):
            _logger.error("TotalPay: session creation failed for %s: %s", self.reference, response)
            raise ValidationError(_(
                "TotalPay did not return a valid checkout session. Please try again "
                "or use another payment method."
            ))

        return {
            'api_url': response['redirect_url'],
            'redirect_params': response.get('redirect_params') or {},
            'redirect_method': (response.get('redirect_method') or 'GET').upper(),
        }

    # -------------------------------------------------------------------
    # EXTRACT REFERENCE
    # Webhook payload uses `order_number`; the browser-return query string
    # uses `order_id` (per redirects_and_returns). We accept either.
    # -------------------------------------------------------------------
    @api.model
    def _extract_reference(self, provider_code, payment_data):
        if provider_code != 'totalpay':
            return super()._extract_reference(provider_code, payment_data)

        order_number = payment_data.get('order_number') or payment_data.get('order_id')
        if not order_number:
            return None

        # order_number as sent to/echoed by TotalPay is the sanitized
        # totalpay_order_number, not Odoo's raw `reference` -- translate
        # back to the real reference so Odoo's base lookup-by-reference
        # finds the right transaction. See totalpay_order_number field.
        tx = self.sudo().search([
            ('provider_code', '=', 'totalpay'),
            ('totalpay_order_number', '=', order_number),
        ], limit=1)
        return tx.reference if tx else order_number

    # -------------------------------------------------------------------
    # EXTRACT AMOUNT DATA
    # -------------------------------------------------------------------
    def _extract_amount_data(self, payment_data):
        if self.provider_code != 'totalpay':
            return super()._extract_amount_data(payment_data)

        if 'order_amount' not in payment_data or 'order_currency' not in payment_data:
            # Bare browser return: no amount data, nothing to validate here.
            # _apply_updates will fall back to the status API instead.
            return None

        return {
            'amount': float(payment_data['order_amount']),
            'currency_code': payment_data['order_currency'],
        }

    # -------------------------------------------------------------------
    # APPLY UPDATES
    # Routes to the webhook handler (authoritative) or the browser-return
    # fallback (confirm via status API), per
    # https://docs.totalpay.global/docs/guides/checkout/redirects_and_returns
    # -------------------------------------------------------------------
    def _apply_updates(self, payment_data):
        if self.provider_code != 'totalpay':
            return super()._apply_updates(payment_data)

        is_webhook_payload = 'type' in payment_data and 'status' in payment_data and 'hash' in payment_data
        if is_webhook_payload:
            self._totalpay_process_callback(payment_data)
        else:
            self._totalpay_confirm_via_status_api()

    # -------------------------------------------------------------------
    # WEBHOOK / CALLBACK PROCESSING
    # https://docs.totalpay.global/docs/guides/checkout/callbacks
    # -------------------------------------------------------------------
    def _totalpay_process_callback(self, payment_data):
        self.ensure_one()
        provider = self.provider_id
        api = provider._totalpay_get_api()

        # 1. Verify signature (constant-time).
        if not api.verify_callback_hash(payment_data):
            _logger.warning("TotalPay: callback hash mismatch for tx %s.", self.reference)
            return

        # 2. Idempotency: dedupe on (id, type, status).
        event_key = "{}:{}:{}".format(
            payment_data.get('id', ''), payment_data.get('type', ''), payment_data.get('status', ''),
        )
        if self.totalpay_last_event_key == event_key:
            _logger.info("TotalPay: duplicate callback ignored for tx %s (%s).", self.reference, event_key)
            return
        self.totalpay_last_event_key = event_key

        event_type = (payment_data.get('type') or '').lower()
        status = (payment_data.get('status') or '').lower()
        order_status = (payment_data.get('order_status') or '').lower()
        reason = payment_data.get('reason')
        provider_reference = payment_data.get('id')

        if provider_reference and not self.provider_reference:
            self.provider_reference = provider_reference

        # 3. status=undefined: uncertain, never fulfill. Log + let the
        #    reconciliation cron sort it out via the status API.
        if status == 'undefined':
            _logger.error(
                "TotalPay: received status=undefined for tx %s (type=%s). Needs manual reconciliation.",
                self.reference, event_type,
            )
            return

        # 4. status=fail: the event failed outright.
        if status == 'fail':
            if event_type in ('refund', 'void', 'capture'):
                self._totalpay_resolve_post_payment_failure(event_type, reason)
            elif self.state in ('draft', 'pending', 'authorized'):
                self._set_error(reason or _("The payment was declined by TotalPay."))
            return

        # From here, status in ('success', 'waiting').

        # 5. Refund outcome: routes to the matching child transaction, since
        #    TotalPay's refund callback echoes the *original* order_number/id
        #    -- it never identifies which child refund it belongs to. See
        #    _totalpay_find_pending_child() docstring for the limitation
        if event_type == 'refund':
            if status == 'success' and order_status in ('settled', 'refund'):
                self._totalpay_resolve_pending_child('refund', done=True)
            elif status == 'waiting':
                _logger.info("TotalPay: refund for %s still processing upstream.", self.reference)
            return

        # 6. Void outcome: void acts on THIS transaction directly (it targets
        #    an authorized-but-not-captured payment).
        if event_type == 'void':
            if status == 'success' and order_status == 'settled':
                if self.state in ('authorized', 'pending', 'draft'):
                    self._set_canceled(_("Authorization voided via TotalPay."))
            return

        # 7. Capture outcome: capture acts on THIS transaction directly.
        # if event_type == 'capture':
        #     if status == 'success' and order_status == 'settled':
        #         if self.state in ('authorized', 'pending', 'draft'):
        #             self._set_done()
        #             self.env.ref('payment.cron_post_process_payment_tx')._trigger()
        #     elif status == 'waiting':
        #         _logger.info("TotalPay: capture for %s still processing upstream.", self.reference)
        #     return

        # 8. Chargeback / reversal: system-generated, always "final" per the
        #    docs, but Odoo has no dedicated state for them. Flag loudly so a
        #    human follows up; don't silently flip the transaction to done/error.
        if event_type in ('chargeback', 'reversal'):
            _logger.warning(
                "TotalPay: %s received for tx %s (order_status=%s). Manual review required.",
                event_type, self.reference, order_status,
            )
            return

        # 9. Sale / recurring / debit / transfer / credit: the "primary
        #    payment" events. `order_status` (not `status`) tells us whether
        #    this is final. See "Final vs intermediary callbacks" in the docs.
        if event_type in SETTLING_TYPES:
            if status == 'success' and order_status == 'settled':
                if self.state in ('draft', 'pending', 'authorized'):
                    self._set_done()
                    self.env.ref('payment.cron_post_process_payment_tx')._trigger()
            elif status == 'success' and event_type == 'sale' and order_status == 'pending':
                # DMS auth-only leg: funds held, not captured yet.
                if self.state in ('draft', 'pending'):
                    self._set_authorized()
            elif order_status == 'decline':
                if self.state in ('draft', 'pending', 'authorized'):
                    self._set_canceled(reason or _("Payment declined by TotalPay."))
            elif status == 'waiting' or order_status == 'prepare':
                if self.state == 'draft':
                    self._set_pending()
            return

        # 10. Intermediary steps (3ds, redirect, init): never final, nothing
        #     to apply. Wait for the next callback.
        if event_type in ('3ds', 'redirect', 'init'):
            _logger.info("TotalPay: intermediary event '%s' for tx %s, awaiting final callback.",
                         event_type, self.reference)
            return

        _logger.info("TotalPay: unhandled callback type '%s' for tx %s.", event_type, self.reference)

    def _totalpay_resolve_pending_child(self, operation, done=True, reason=None):
        """ Find the oldest still-in-progress child transaction of `self` for
        the given operation (e.g. 'refund') and finalize it.

        LIMITATION: TotalPay's refund callback carries the *parent* payment's
        id/order_number, not an identifier for the specific refund. If you
        issue more than one partial refund before the first one's callback
        arrives, this can apply the callback to the wrong child. Avoid
        overlapping partial refunds on the same transaction until each one
        resolves (check its state) if this matters for your business. """
        self.ensure_one()
        pending_children = self.child_transaction_ids.filtered(
            lambda c: c.operation == operation and c.state in ('draft', 'pending')
        ).sorted('create_date')

        if not pending_children:
            _logger.warning(
                "TotalPay: %s callback for %s but no pending %s child transaction found.",
                operation, self.reference, operation,
            )
            return

        if len(pending_children) > 1:
            _logger.warning(
                "TotalPay: %d pending %s children found for %s; resolving the oldest one. "
                "This can misattribute the callback if refunds overlapped.",
                len(pending_children), operation, self.reference,
            )

        child = pending_children[0]
        if done:
            child._set_done()
        else:
            child._set_error(reason or _("TotalPay reported failure for this operation."))

    def _totalpay_resolve_post_payment_failure(self, operation, reason):
        self.ensure_one()
        if operation == 'refund':
            self._totalpay_resolve_pending_child('refund', done=False, reason=reason)
        elif operation in ('void', 'capture') and self.state in ('draft', 'pending', 'authorized'):
            _logger.warning("TotalPay: %s failed for tx %s: %s", operation, self.reference, reason)

    # -------------------------------------------------------------------
    # BROWSER-RETURN FALLBACK
    # The callback is the source of truth (docs are explicit about this),
    # so the return URL never fulfills the order on its own -- it only
    # polls the status API as a stopgap for when the callback is delayed.
    # https://docs.totalpay.global/docs/guides/checkout/redirects_and_returns
    # -------------------------------------------------------------------
    def _totalpay_confirm_via_status_api(self):
        self.ensure_one()
        if self.state not in ('draft', 'pending', 'authorized'):
            return  # Already resolved by a callback; nothing to do.

        provider = self.provider_id
        api = provider._totalpay_get_api()

        if self.provider_reference:
            # payment_id (UUID) has no character-validation quirks; prefer
            # it once we have one (e.g. from an earlier partial callback).
            response = api.get_status_by_payment_id(self.provider_reference)
        else:
            order_number = self.totalpay_order_number or self._totalpay_compute_order_number()
            response = api.get_status_by_order_id(order_number)

        if response.get('status') == 'error':
            _logger.info("TotalPay: status check failed for %s; will retry via cron.", self.reference)
            return

        if response.get('payment_id') and not self.provider_reference:
            self.provider_reference = response['payment_id']

        self._totalpay_apply_status_value(response.get('status'))

    def _totalpay_apply_status_value(self, order_status):
        """ Map a value from GET-status (`/api/v1/payment/status`) onto the
        Odoo transaction state. Uses the same vocabulary as `order_status` in
        callbacks: prepare, settled, pending, 3ds, redirect, decline, credit,
        refund, reversal, void, chargeback. """
        self.ensure_one()
        order_status = (order_status or '').lower()

        if order_status == 'settled':
            if self.state in ('draft', 'pending', 'authorized'):
                self._set_done()
                self.env.ref('payment.cron_post_process_payment_tx')._trigger()
        elif order_status == 'pending':
            if self.state == 'draft':
                self._set_authorized()
        elif order_status in ('prepare', '3ds', 'redirect'):
            if self.state == 'draft':
                self._set_pending()
        elif order_status == 'decline':
            if self.state in ('draft', 'pending', 'authorized'):
                self._set_canceled(_("Payment declined by TotalPay."))
        elif order_status in ('chargeback', 'reversal'):
            _logger.warning("TotalPay: %s status via poll for tx %s. Manual review required.",
                             order_status, self.reference)
        # 'void' / 'refund' / 'credit' here would mean the *original* sale
        # itself was voided/refunded before ever settling; leave as-is and
        # let a human investigate rather than guessing.

    # -------------------------------------------------------------------
    # CAPTURE / VOID (act directly on this transaction)
    # https://docs.totalpay.global/docs/requests/checkout/payment-checkout-api
    # -------------------------------------------------------------------
    def _send_capture_request(self):
        child = super()._send_capture_request()
        if self.provider_code != 'totalpay':
            return child

        if not self.provider_reference:
            raise UserError(_("TotalPay: cannot capture a transaction without a payment_id."))

        api = self.provider_id._totalpay_get_api()
        amount = format_amount(self.amount, self.currency_id.name)
        response = api.capture(self.provider_reference, amount)

        if response.get('status') == 'error':
            _logger.error("TotalPay: capture failed for %s: %s", self.reference, response)
            raise UserError(_("TotalPay capture failed: %s") % response.get('message', _('unknown error')))

        # The capture response can already report the final state; the
        # `type=capture` callback will also arrive and is handled
        # idempotently by _totalpay_process_callback.
        if response.get('status') == 'settled' and self.state in ('authorized', 'pending'):
            self._set_done()
            self.env.ref('payment.cron_post_process_payment_tx')._trigger()

        return child

    def _send_void_request(self):
        child = super()._send_void_request()
        if self.provider_code != 'totalpay':
            return child

        if not self.provider_reference:
            raise UserError(_("TotalPay: cannot void a transaction without a payment_id."))

        api = self.provider_id._totalpay_get_api()
        response = api.void(self.provider_reference)

        if response.get('status') == 'error':
            _logger.error("TotalPay: void failed for %s: %s", self.reference, response)
            raise UserError(_("TotalPay void failed: %s") % response.get('message', _('unknown error')))

        if self.state in ('authorized', 'pending', 'draft'):
            self._set_canceled(_("Authorization voided via TotalPay."))

        return child

    # -------------------------------------------------------------------
    # REFUND (creates and returns a child transaction; TotalPay's async
    # `type=refund` callback finalizes it -- see _totalpay_resolve_pending_child)
    # -------------------------------------------------------------------
    def _send_refund_request(self):
        # In Odoo 19, this method runs ON THE CHILD refund transaction (self),
        # not the original. self.amount is already negative (the refund
        # amount), and self.source_transaction_id points back to the original
        # transaction, which holds the real payment_id.
        super()._send_refund_request()
        if self.provider_code != 'totalpay':
            return

        source_tx = self.source_transaction_id
        if not source_tx or not source_tx.provider_reference:
            raise ValidationError(_(
                "TotalPay: cannot refund -- the original transaction has no payment_id."
            ))

        api = self.provider_id._totalpay_get_api()
        amount = format_amount(abs(self.amount), self.currency_id.name)
        response = api.refund(source_tx.provider_reference, amount)

        if response.get('status') == 'error':
            _logger.error("TotalPay: refund failed for %s: %s", self.reference, response)
            # Raising ValidationError matches the base _refund()'s own contract:
            # it catches this and calls refund_tx._set_error() for us.
            raise ValidationError(_("TotalPay refund failed: %s") % response.get('message', _('unknown error')))

        # Accepted for processing; the async `type=refund` callback confirms
        # the final outcome.
        self._set_pending()

    # -------------------------------------------------------------------
    # RECONCILIATION CRON
    # https://docs.totalpay.global/docs/guides/checkout/callbacks#delivery--retries
    # "Each callback is delivered exactly once. There are no automatic
    # retries. ... Reconcile via /api/v1/payment/status as a safety net."
    # -------------------------------------------------------------------
    @api.model
    def _cron_totalpay_reconcile_pending(self):
        cutoff = fields.Datetime.now() - timedelta(minutes=RECONCILE_AFTER_MINUTES)
        stuck_txs = self.search([
            ('provider_code', '=', 'totalpay'),
            ('state', 'in', ('draft', 'pending', 'authorized')),
            ('create_date', '<=', cutoff),
            ('source_transaction_id', '=', False),  # skip refund/capture children -- they have no order of their own at TotalPay
        ])
        _logger.info("TotalPay: reconciliation cron checking %d stuck transaction(s).", len(stuck_txs))
        for tx in stuck_txs:
            try:
                tx._totalpay_confirm_via_status_api()
            except Exception:
                _logger.exception("TotalPay: reconciliation failed for tx %s.", tx.reference)

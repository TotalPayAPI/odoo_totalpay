# -*- coding: utf-8 -*-
import logging
import json

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class TotalPayController(http.Controller):

    # WEBHOOK (server-to-server callback)
    # https://docs.totalpay.global/docs/guides/checkout/callbacks

    @http.route('/payment/totalpay/webhook', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def totalpay_webhook(self, **post):
        if not post:
            try:
                raw_body = request.httprequest.get_data()
                if raw_body:
                    post = json.loads(raw_body)
            except (ValueError, TypeError):
                _logger.warning("TotalPay: webhook body could not be parsed as JSON either.")
                post = {}

        _logger.info("TotalPay: webhook received for order_number=%s, type=%s, status=%s",
                    post.get('order_number'), post.get('type'), post.get('status'))

        if not post.get('id') or not post.get('order_number'):
            _logger.warning("TotalPay: webhook missing id/order_number: %s", post)
            return request.make_response('missing required fields', status=400)

        try:
            request.env['payment.transaction'].sudo()._process('totalpay', post)
        except Exception:
            _logger.exception("TotalPay: error while processing webhook for order_number=%s",
                            post.get('order_number'))

        return request.make_response('OK', status=200)


    # -------------------------------------------------------------------
    # CUSTOMER RETURN
    # show a pending/confirmation page, but the webhook (not this
    # redirect) is the source of truth. _apply_updates() falls back to
    # polling the status API when this payload doesn't carry the
    # authoritative type/status/hash fields the webhook carries.
    # -------------------------------------------------------------------
    @http.route('/payment/totalpay/return', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False)
    def totalpay_return(self, **data):
        _logger.info("TotalPay: customer returned to success_url: %s", data)
        if data.get('order_id') or data.get('payment_id'):
            request.env['payment.transaction'].sudo()._process('totalpay', data)
        return request.redirect('/payment/status')

    @http.route('/payment/totalpay/cancel', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False)
    def totalpay_cancel(self, **data):
        _logger.info("TotalPay: customer returned to cancel_url: %s", data)
        # landing on cancel_url does not guarantee the payment
        # actually failed (network issues, back-button). Poll rather than
        # assume, same as the success path.
        if data.get('order_id') or data.get('payment_id'):
            request.env['payment.transaction'].sudo()._process('totalpay', data)
        return request.redirect('/payment/status')

    @http.route('/payment/totalpay/expired', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False)
    def totalpay_expired(self, **data):
        _logger.info("TotalPay: session expired (expiry_url): %s", data)
        return request.redirect('/payment/status')

    @http.route('/payment/totalpay/error', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False)
    def totalpay_error(self, **data):
        _logger.warning("TotalPay: technical error redirect (error_url): %s", data)
        return request.redirect('/payment/status')

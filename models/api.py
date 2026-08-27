# -*- coding: utf-8 -*-
import hashlib
import hmac
import logging

import requests

_logger = logging.getLogger(__name__)

TIMEOUT = 20  # seconds, requests to TotalPay

# Currencies whose smallest unit is NOT 2 decimal places.
CURRENCY_DECIMALS_OVERRIDE = {
    'BHD': 3,
    'KWD': 3,
    'OMR': 3,
    'JOD': 3,
}

# https://docs.totalpay.global/docs/guides/checkout/callbacks#event-types
# type -> whether it represents a *settling* event (order_status == 'settled' means final)
SETTLING_TYPES = {'sale', 'capture', 'recurring', 'debit', 'transfer', 'credit'}

TERMINAL_ORDER_STATUSES = {'settled', 'refund', 'void', 'chargeback', 'reversal', 'decline'}


def format_amount(amount, currency_name):
    """ Format an amount the way TotalPay expects: a decimal string with the
    correct number of decimals for the currency (most currencies use 2,
    a handful of Gulf-region currencies use 3). """
    decimals = CURRENCY_DECIMALS_OVERRIDE.get(currency_name, 2)
    return f"{amount:.{decimals}f}"


class TotalPayAPI:
    """ Thin client around the TotalPay Checkout API (protocol: CHECKOUT).

    Docs:
      - https://docs.totalpay.global/docs/requests/checkout/payment-checkout-api
      - https://docs.totalpay.global/docs/requests/hash_signature
    """

    def __init__(self, provider):
        self.provider = provider
        self.env = provider.env
        if provider.state == 'enabled':
            self.merchant_key = provider.totalpay_merchant_key_live
        else:
            self.merchant_key = provider.totalpay_merchant_key_test
        self.password = provider.totalpay_hash_password
        self.hash_mode = provider.totalpay_hash_mode or 'md5'
        self.base_url = (provider.totalpay_api_url or '').rstrip('/')

    # -------------------------------------------------------------------
    # HASH SIGNATURE
    # https://docs.totalpay.global/docs/requests/hash_signature
    #   hash = sha1( md5( uppercase( concat_of_fields_in_order ) ) )
    #   or sha1( sha256( ... ) ) if SHA256 mode is enabled for the merchant
    # -------------------------------------------------------------------
    def _hash(self, fields):
        base_string = (''.join(str(f) for f in fields) + self.password).upper()
        if self.hash_mode == 'sha256':
            inner = hashlib.sha256(base_string.encode('utf-8')).hexdigest()
        else:
            inner = hashlib.md5(base_string.encode('utf-8')).hexdigest()
        return hashlib.sha1(inner.encode('utf-8')).hexdigest()

    def verify_callback_hash(self, data):
        """ Verify the hash on an inbound callback (webhook) or customer-return
        redirect. Both use the same field order per the docs:
        id, order_number, order_amount, order_currency, order_description, password
        """
        expected = self._hash([
            data.get('id', ''),
            data.get('order_number', ''),
            data.get('order_amount', ''),
            data.get('order_currency', ''),
            data.get('order_description', ''),
        ])
        received = str(data.get('hash') or '')
        if not received:
            return False
        return hmac.compare_digest(received, expected)

    # -------------------------------------------------------------------
    # HTTP
    # -------------------------------------------------------------------
    def _post(self, endpoint, payload):
        if not self.merchant_key or not self.password or not self.base_url:
            _logger.error("TotalPay: provider %s is missing merchant_key/password/api_url.", self.provider.name)
            return {'status': 'error', 'message': 'TotalPay provider is not fully configured.'}

        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            _logger.exception("TotalPay: request to %s failed.", endpoint)
            return {'status': 'error', 'message': str(e)}

        try:
            data = response.json()
        except ValueError:
            _logger.warning("TotalPay: non-JSON response from %s: %s", endpoint, response.text[:500])
            data = {'status': 'error', 'message': 'Invalid JSON response from TotalPay.'}

        if response.status_code >= 400:
            _logger.warning(
                "TotalPay: %s returned HTTP %s: %s", endpoint, response.status_code, data
            )

        return data

    # -------------------------------------------------------------------
    # SALE / SESSION CREATION
    # POST /api/v1/session
    # https://docs.totalpay.global/docs/requests/checkout/sale
    # -------------------------------------------------------------------
    def create_session(self, *, order_number, order_amount, order_currency, order_description,
                        success_url, cancel_url, expiry_url=None, error_url=None,
                        operation='purchase', auth=False, custom_data=None, req_token=False):
        hash_signature = self._hash([order_number, order_amount, order_currency, order_description])

        payload = {
            'merchant_key': self.merchant_key,
            'operation': operation,
            'order': {
                'number': order_number,
                'amount': order_amount,
                'currency': order_currency,
                'description': order_description,
            },
            'success_url': success_url,
            'cancel_url': cancel_url,
            'hash': hash_signature,
        }
        if expiry_url:
            payload['expiry_url'] = expiry_url
        if error_url:
            payload['error_url'] = error_url
        if auth:
            # DMS / two-step (auth-only, capture later). Default is 'N' (immediate capture).
            payload['auth'] = 'Y'
        if custom_data:
            payload['custom_data'] = custom_data
        if req_token:
            payload['req_token'] = True

        return self._post('/api/v1/session', payload)

    # -------------------------------------------------------------------
    # CAPTURE
    # POST /api/v1/payment/capture
    # -------------------------------------------------------------------
    def capture(self, payment_id, amount):
        hash_signature = self._hash([payment_id, amount])
        payload = {
            'merchant_key': self.merchant_key,
            'payment_id': payment_id,
            'amount': amount,
            'hash': hash_signature,
        }
        return self._post('/api/v1/payment/capture', payload)

    # -------------------------------------------------------------------
    # REFUND
    # POST /api/v1/payment/refund
    # -------------------------------------------------------------------
    def refund(self, payment_id, amount):
        hash_signature = self._hash([payment_id, amount])
        payload = {
            'merchant_key': self.merchant_key,
            'payment_id': payment_id,
            'amount': amount,
            'hash': hash_signature,
        }
        return self._post('/api/v1/payment/refund', payload)

    # -------------------------------------------------------------------
    # VOID
    # POST /api/v1/payment/void
    # -------------------------------------------------------------------
    def void(self, payment_id):
        hash_signature = self._hash([payment_id])
        payload = {
            'merchant_key': self.merchant_key,
            'payment_id': payment_id,
            'hash': hash_signature,
        }
        return self._post('/api/v1/payment/void', payload)

    # -------------------------------------------------------------------
    # STATUS
    # POST /api/v1/payment/status
    # -------------------------------------------------------------------
    def get_status_by_payment_id(self, payment_id):
        hash_signature = self._hash([payment_id])
        payload = {
            'merchant_key': self.merchant_key,
            'payment_id': payment_id,
            'hash': hash_signature,
        }
        return self._post('/api/v1/payment/status', payload)

    def get_status_by_order_id(self, order_id):
        hash_signature = self._hash([order_id])
        payload = {
            'merchant_key': self.merchant_key,
            'order_id': order_id,
            'hash': hash_signature,
        }
        return self._post('/api/v1/payment/status', payload)

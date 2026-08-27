# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .api import TotalPayAPI

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('totalpay', "TotalPay")],
        ondelete={'totalpay': 'set default'},
    )

    # --- TotalPay credentials & config -------------------------------------
    totalpay_merchant_key_live = fields.Char(
        string="TotalPay Merchant Key - Live",
        help="Merchant key provided by TotalPay for your live account ",
        required_if_provider='totalpay',
    )
    totalpay_merchant_key_test = fields.Char(
        string="TotalPay Merchant Key - Test",
        help="Merchant key provided by TotalPay for your test account ",
        required_if_provider='totalpay',
    )
    totalpay_hash_password = fields.Char(
        string="TotalPay Hashing Password",
        help="Secret password used only to compute the hash signature. "
             "Never sent to the browser or in any request body.",
        required_if_provider='totalpay',
        groups="base.group_system",
    )
    totalpay_hash_mode = fields.Selection(
        selection=[('md5', 'MD5 (default)'), ('sha256', 'SHA256')],
        string="Hash Mode",
        default='md5',
        required_if_provider='totalpay',
        help="Must match the 'Use SHA256 encryption algorithm for hash' setting "
             "on your Protocol Mapping in the TotalPay admin panel. Confirm with "
             "your account manager if unsure -- getting this wrong breaks every "
             "signature check (requests, callbacks, and return redirects).",
    )
    totalpay_api_url = fields.Char(
        string="TotalPay API Base URL",
        help="Base URL for API calls, e.g. your sandbox or production Checkout host. "
             "Confirm the exact host with your TotalPay account manager.",
        required_if_provider='totalpay',
    )

    totalpay_callback_endpoint = fields.Char(
        string="TotalPay Webhook URL",
        compute='_compute_totalpay_callback_endpoint',
        help="This is the exact URL your server will respond on. Give this URL "
             "to TotalPay as the notification_url for this merchant."
             " It's computed automatically from web.base.url"
             "to your real domain first (Settings > Technical > Parameters > "
             "System Parameters).",
    )

    def _compute_totalpay_callback_endpoint(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        for rec in self:
            rec.totalpay_callback_endpoint = f"{base_url}/payment/totalpay/webhook"

    # totalpay_callback_endpoint = fields.Char(
    #     string="TotalPay API Callback Endpoint",
    #     help="Callback endpoint to be shared with TotalPay, e.g. your sandbox or production Checkout host. "
    #          "Confirm the exact host with your TotalPay account manager.",
    #     required_if_provider='totalpay',
    # )

    # totalpay_capture_manually = fields.Boolean(
    #     string="TotalPay: Capture Manually (DMS mode)",
    #     help="If enabled, payments are only authorized (funds held) at checkout time. "
    #          "You (or a cron/API call) must explicitly capture the payment afterwards. "
    #          "Your TotalPay account must have DMS mode enabled for this to work -- "
    #          "contact your account manager first.",
    # )

    # -------------------------------------------------------------------
    # FEATURE SUPPORT
    #
    # NOTE: depending on the Odoo point release, capture/refund/tokenization
    # support flags live on payment.provider, on payment.method, or both.
    # We set them in both places defensively (field-existence checked) so
    # this module doesn't break across minor version differences.
    # -------------------------------------------------------------------
    @api.model
    def _setup_totalpay_payment_methods(self):
        """ Safely configure the 'totalpay' payment.method capabilities
        without crashing on Odoo versions where some fields don't exist. """
        method = self.env['payment.method'].search([('code', '=', 'totalpay')], limit=1)
        if not method:
            return

        method_fields = self.env['payment.method']._fields
        vals = {}
        if 'support_manual_capture' in method_fields:
            vals['support_manual_capture'] = 'partial'
        if 'support_refund' in method_fields:
            vals['support_refund'] = 'partial'
        if 'support_tokenization' in method_fields:
            vals['support_tokenization'] = False
        if 'support_express_checkout' in method_fields:
            vals['support_express_checkout'] = False

        if vals:
            _logger.info("TotalPay: configuring payment.method fields: %s", list(vals.keys()))
            method.sudo().write(vals)

    def _compute_feature_support_fields(self):
        """ Override of `payment` to declare TotalPay's capabilities.
        https://docs.totalpay.global/docs/guides/checkout/checkout_overview:
        'Two-step auth + capture: DMS mode for deferred and partial captures'
        and 'Refund, void, and credit: full or partial, server-to-server'. """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'totalpay').update({
            'support_manual_capture': 'partial',
            'support_refund': 'partial',
            'support_tokenization': False,
        })

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        self.ensure_one()
        if self.code != 'totalpay':
            return super()._get_default_payment_method_codes()
        return ['totalpay']

    # -------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------
    def _totalpay_get_api(self):
        self.ensure_one()
        return TotalPayAPI(self)

    def _totalpay_get_urls(self):
        """ Build the success/cancel/expiry/error URLs TotalPay redirects the
        customer to. https://docs.totalpay.global/docs/guides/checkout/redirects_and_returns """
        self.ensure_one()
        base_url = self.get_base_url()
        return {
            'success_url': f"{base_url}/payment/totalpay/return",
            'cancel_url': f"{base_url}/payment/totalpay/cancel",
            'expiry_url': f"{base_url}/payment/totalpay/expired",
            'error_url': f"{base_url}/payment/totalpay/error",
        }

    # -------------------------------------------------------------------
    # VALIDATION
    # -------------------------------------------------------------------

    @api.constrains('totalpay_merchant_key_live', 'totalpay_merchant_key_test',
                     'totalpay_hash_password', 'totalpay_api_url', 'state')
    def _totalpay_check_config_on_save(self):
        for rec in self:
            if rec.code != 'totalpay' or rec.state == 'disabled':
                continue

            needs_test_key = rec.state == 'test'
            needs_live_key = rec.state == 'enabled'

            if needs_test_key and not rec.totalpay_merchant_key_test:
                raise ValidationError(_(
                    "TotalPay: a Test Merchant Key is required while this provider is in Test Mode."
                ))
            if needs_live_key and not rec.totalpay_merchant_key_live:
                raise ValidationError(_(
                    "TotalPay: a Live Merchant Key is required to enable this provider in production."
                ))
            if not rec.totalpay_hash_password:
                raise ValidationError(_(
                    "TotalPay: the Hashing Password is required."
                ))
            if not rec.totalpay_api_url or not rec.totalpay_api_url.startswith('https://'):
                raise ValidationError(_(
                    "TotalPay: the API Base URL must be a valid HTTPS URL provided by TotalPay"
                ))
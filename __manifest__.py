{
    'name': "TotalPay Payments",
    'version': '19.0.3.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Accept payments on your Odoo site in a seamless and secure checkout environment.",
    'description': """
TotalPay Payment Gateway
==========================
Accept payments on your Odoo site in a seamless and secure checkout environment.

TotalPay integrates directly into Odoo's payment flow, giving your customers a fast,
secure hosted checkout experience - backed by real-time payment confirmation, refunds,
and automatic reconciliation.

Features
--------
* Secure hosted checkout redirect
* Real-time payment confirmation via webhook
* Support for full and partial refunds
* Manual capture (DMS / two-step authorization) support
* Automatic reconciliation for delayed or missed confirmations

Docs: https://docs.totalpay.global/
    """,
    'author': "TotalPay",
    'website': "https://totalpay.global",
    'depends': ['payment'],
    'data': [
        'views/payment_provider_views.xml',

        'data/ir_cron_data.xml',
        'data/payment_provider_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'LGPL-3',
    'installable': True,
}

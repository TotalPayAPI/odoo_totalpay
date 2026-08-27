{
    'name': "Payment Provider: TotalPay",
    'version': '19.0.3.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "TotalPay hosted checkout payment provider",
    'description': """
TotalPay Payment Provider
==========================
Integrates TotalPay's hosted Checkout (redirect) integration as a payment
provider in Odoo: session creation, server-to-server webhook confirmation,
manual capture (DMS mode), refunds, voids, and a reconciliation cron for
transactions that never receive a callback.

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

Eventyay BitPay
===============

Eventyay BitPay is a payment plugin for `Eventyay <https://github.com/fossasia/eventyay>`_. It allows event organisers to accept cryptocurrency payments through BitPay and BitPay compatible payment providers.

The plugin adds a ``BitPay`` payment provider to Eventyay. It supports connecting an event to BitPay, using BitPay test mode, connecting to third party providers with a BitPay compatible API, creating payment invoices, receiving payment status webhooks, handling payment returns, and processing refunds.

Project status
--------------

Eventyay BitPay is maintained as part of the Eventyay plugin ecosystem.

The plugin package is named ``eventyay-bitpay`` and the Python module is named ``eventyay_bitpay``.

Main features
-------------

* Cryptocurrency payment support for Eventyay events
* BitPay account connection from the event payment settings
* BitPay test mode support
* Support for third party BitPay compatible API providers
* Custom public payment method name
* Invoice creation during checkout
* Payment return handling after the buyer completes payment
* Webhook handling for payment status updates
* Payment confirmation for paid, confirmed, and completed invoices
* Pending payment view for unpaid invoices
* Control view for payment details in the organiser interface
* Refund and partial refund support
* Payment data shredding for sensitive payment information
* Internationalisation support through Django translation files

Requirements
------------

* Python 3.9 or newer
* Eventyay 4.16.0 or newer
* ``btcpay-python``
* A working Eventyay development or production setup
* A BitPay account or a BitPay compatible provider

Installation for development
----------------------------

Use this setup when developing the plugin together with a local Eventyay installation.

1. Set up Eventyay

   First make sure you have a working Eventyay development setup.

   See the Eventyay repository:

   `Eventyay <https://github.com/fossasia/eventyay>`_

2. Clone this repository

   Clone the plugin repository next to your Eventyay checkout or into the local plugin directory used by your Eventyay development setup.

   .. code-block:: bash

      git clone https://github.com/fossasia/eventyay-bitpay.git
      cd eventyay-bitpay

3. Activate the Eventyay virtual environment

   Activate the virtual environment used by your Eventyay installation.

   Example:

   .. code-block:: bash

      cd ../eventyay/app
      . .venv/bin/activate

4. Install the plugin in editable mode

   From the ``eventyay-bitpay`` directory, install the plugin in editable mode.

   .. code-block:: bash

      pip install -e .

   If your Eventyay development setup uses ``uv``, you can also use:

   .. code-block:: bash

      uv pip install -e .

5. Apply migrations

   Run migrations from the Eventyay app directory.

   .. code-block:: bash

      python manage.py migrate

6. Compile translations

   Compile translation files from the plugin directory.

   .. code-block:: bash

      make

7. Restart Eventyay

   Restart the Eventyay development server and worker processes if they are running.

   .. code-block:: bash

      python manage.py runserver

   If you use Docker for Eventyay development, restart the relevant containers after installing or changing the plugin.

Using the plugin
----------------

1. Open the Eventyay organiser interface.
2. Open the event where cryptocurrency payments should be enabled.
3. Go to the payment provider settings.
4. Enable the BitPay payment provider.
5. Connect the event to BitPay or to a BitPay compatible provider.
6. Configure the public payment method name if needed.
7. Save the payment settings.
8. Test the checkout flow with a test order.

BitPay connection
-----------------

If no BitPay token is configured, the plugin shows connection options in the payment provider settings.

The organiser can:

* Connect with BitPay
* Connect with ``test.bitpay.com`` for test mode
* Enter a custom BitPay compatible API URL and start pairing

After the BitPay token is authorised, return to Eventyay and refresh the payment provider settings page.

Test mode
---------

The plugin supports BitPay test mode through ``test.bitpay.com``. In test mode, no real money is transferred.

Use test mode before enabling cryptocurrency payments for a live event.

Third party BitPay compatible providers
---------------------------------------

The plugin can connect to third party providers that expose a BitPay compatible API.

To use such a provider, enter the provider API URL in the BitPay payment settings and start pairing.

Checkout flow
-------------

During checkout, the plugin creates a BitPay invoice for the order payment amount and event currency. The buyer is redirected to the invoice URL returned by the payment provider.

The plugin stores the invoice data with the Eventyay order payment and records the external BitPay invoice reference.

Webhook flow
------------

The plugin exposes a webhook endpoint for payment provider notifications.

When a webhook is received, the plugin looks up the referenced BitPay object, validates that it belongs to the current event, logs the BitPay event, and processes the invoice status.

Invoice states handled by the plugin include:

* ``new``
* ``paid``
* ``confirmed``
* ``complete``
* ``expired``
* ``invalid``

Paid, confirmed, and complete invoices can confirm the Eventyay payment.

Refunds
-------

The plugin supports refunds and partial refunds through the BitPay compatible API.

If the provider reports an error during refund processing, the plugin raises an Eventyay payment exception so the organiser can see that the refund could not be completed automatically.

URLs
----

The plugin registers event specific URLs for BitPay payment handling.

Event URLs include:

.. code-block:: text

   /<organizer>/<event>/bitpay/webhook/
   /<organizer>/<event>/bitpay/redirect/
   /<organizer>/<event>/bitpay/return/<order>/<hash>/<payment>/

Control URLs include:

.. code-block:: text

   /control/event/<organizer>/<event>/bitpay/connect/
   /control/event/<organizer>/<event>/bitpay/disconnect/

Data model
----------

The plugin stores references to BitPay objects in ``ReferencedBitPayObject``.

The model links the external BitPay reference to:

* The Eventyay order
* The Eventyay payment, if available

This allows webhook notifications to be mapped back to the corresponding Eventyay order and payment.

Development commands
--------------------

Run these commands from the plugin repository unless otherwise noted.

Install the plugin in editable mode:

.. code-block:: bash

   pip install -e .

Compile translations:

.. code-block:: bash

   make

Regenerate translation files:

.. code-block:: bash

   make localegen

Run tests, if tests are available in the checkout:

.. code-block:: bash

   pytest

Package structure
-----------------

Important files and directories:

.. code-block:: text

   .
   ├── eventyay_bitpay/
   │   ├── __init__.py        Plugin version
   │   ├── apps.py            Eventyay plugin metadata
   │   ├── models.py          BitPay reference model
   │   ├── payment.py         Payment provider implementation
   │   ├── signals.py         Payment provider registration and display hooks
   │   ├── urls.py            Event and control URLs
   │   ├── views.py           Webhook, return, redirect, connect and disconnect views
   │   ├── templates/         Django templates
   │   ├── static/            Static assets
   │   └── locale/            Translation files
   ├── pyproject.toml         Python package metadata
   ├── setup.py               Setuptools entry point
   ├── setup.cfg              Tool configuration
   ├── MANIFEST.in            Package data inclusion
   ├── Makefile               Translation helper commands
   ├── LICENSE                Apache License 2.0
   └── README.rst             Project documentation

Packaging
---------

The package metadata is defined in ``pyproject.toml``.

The package includes:

* Python module ``eventyay_bitpay``
* Static files
* Templates
* Locale files
* License file

The version is read from ``eventyay_bitpay.__version__``.

Security and privacy notes
--------------------------

The plugin stores payment provider responses as payment information in Eventyay.

The plugin includes a ``shred_payment_info`` implementation that masks sensitive data while keeping selected operational fields such as invoice ID, status, price, currency, invoice time, payment totals, transaction currency, and amount paid.

Production notes
----------------

Before using the plugin for a live event:

* Test the provider connection with a test event.
* Verify that the payment method appears correctly in checkout.
* Confirm that the webhook endpoint is reachable from the payment provider.
* Place a test order and verify that payment confirmation updates the order.
* Test refund handling before relying on automatic refunds.
* Confirm that the event currency is supported by the selected BitPay compatible provider.
* Confirm that the public payment method name matches the cryptocurrency payment options actually accepted.

License
-------

Eventyay BitPay is released under the terms of the Apache License 2.0.

See `LICENSE <LICENSE>`_ for the full license text.

Credits
-------

Copyright 2018 Raphael Michel.

Maintained by the Eventyay team and FOSSASIA.

Links
-----

* `Eventyay <https://github.com/fossasia/eventyay>`_
* `Eventyay BitPay <https://github.com/fossasia/eventyay-bitpay>`_
* `BitPay <https://bitpay.com>`_
* `BTCPay Python package <https://pypi.org/project/btcpay-python/>`_
* `FOSSASIA <https://fossasia.org>`_

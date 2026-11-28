import json
from unittest.mock import Mock

import pytest
from eventyay.base.models import LogEntry, Order, OrderPayment, OrderRefund

from eventyay_bitpay.payment import BitPay


@pytest.fixture
def order():
    order = Order()
    order.all_logentries = Mock()
    return order


@pytest.mark.parametrize("record_type", [OrderPayment, OrderRefund])
def test_shred_payment_and_refund_info(record_type, order):
    kept = {"id": "invoice-123", "status": "confirmed", "amount": 10, "currency": "EUR"}
    record = record_type()
    record.info_data = {
        **kept,
        "token": "token-123",
        "buyer": {"name": "Test Buyer", "email": "buyer@example.test"},
    }
    record.order = order
    record.save = Mock()
    order.all_logentries.return_value.filter.return_value.exclude.return_value = []

    BitPay(event=Mock()).shred_payment_info(record)

    assert record.info_data == {**kept, "token": "█", "buyer": "█"}
    record.save.assert_called_once_with(update_fields=["info"])


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "id": "invoice-123",
                "status": "confirmed",
                "buyerFields": {"buyerEmail": "buyer@example.test"},
            },
            {"id": "invoice-123", "status": "confirmed", "buyerFields": "█"},
        ),
        (
            {
                "event": {
                    "code": 1005,
                    "name": "invoice_confirmed",
                    "email": "buyer@example.test",
                },
                "data": {
                    "id": "invoice-123",
                    "price": 10,
                    "currency": "EUR",
                    "token": "token-123",
                },
                "email": "buyer@example.test",
            },
            {
                "event": {"code": 1005, "name": "invoice_confirmed", "email": "█"},
                "data": {
                    "id": "invoice-123",
                    "price": 10,
                    "currency": "EUR",
                    "token": "█",
                },
                "email": "█",
            },
        ),
        ({"data": "buyer@example.test"}, {"data": {}}),
        (["buyer@example.test"], {}),
    ],
    ids=["standard", "extended", "malformed-envelope", "malformed-payload"],
)
def test_shred_webhook_log(payload, expected, order):
    entry = LogEntry(data=json.dumps(payload))
    entry.save = Mock()
    order.all_logentries.return_value.filter.return_value.exclude.return_value = [entry]
    payment = OrderPayment()
    payment.order = order
    payment.save = Mock()

    BitPay(event=Mock()).shred_payment_info(payment)

    assert json.loads(entry.data) == expected
    assert entry.shredded
    entry.save.assert_called_once_with(update_fields=["data", "shredded"])
    order.all_logentries.return_value.filter.assert_called_once_with(
        action_type="eventyay_bitpay.event"
    )

    del entry.parsed_data
    BitPay(event=Mock()).shred_payment_info(payment)
    assert json.loads(entry.data) == expected


@pytest.mark.parametrize(
    "info", ['["buyer@example.test"]', "invalid buyer@example.test"]
)
def test_shred_malformed_payment_info(info, order):
    payment = OrderPayment(order=order, info=info)
    payment.save = Mock()
    order.all_logentries.return_value.filter.return_value.exclude.return_value = []

    BitPay(event=Mock()).shred_payment_info(payment)

    assert payment.info_data == {}

"""Privacy and fail-open behavior of plugin operational logs."""

from __future__ import annotations

import builtins
import contextlib
import importlib
import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest
import requests

from eventyay_bitpay import operational_log
from eventyay_bitpay.operational_log import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    _resolve_host_emitter,
    log_operation,
    logged_request,
)


@pytest.fixture
def captured(monkeypatch):
    records = []

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)
    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    return records


def _load_plugin_modules(monkeypatch):
    """Import payment and views with minimal host stubs when Django isn't initialized."""
    from django.apps import apps
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret",
            INSTALLED_APPS=[],
            USE_I18N=False,
            INSTANCE_NAME="Eventyay Test",
        )

    if not apps.ready:
        eventyay_mod = sys.modules.get("eventyay") or types.ModuleType("eventyay")
        base_mod = sys.modules.get("eventyay.base") or types.ModuleType("eventyay.base")
        models_mod = types.ModuleType("eventyay.base.models")
        payment_mod = types.ModuleType("eventyay.base.payment")
        services_mod = types.ModuleType("eventyay.base.services")
        locking_mod = types.ModuleType("eventyay.base.services.locking")
        control_mod = types.ModuleType("eventyay.control")
        permissions_mod = types.ModuleType("eventyay.control.permissions")
        multidomain_mod = types.ModuleType("eventyay.multidomain")
        urlreverse_mod = types.ModuleType("eventyay.multidomain.urlreverse")

        class Order:
            STATUS_PAID = "p"

        class OrderPayment:
            PAYMENT_STATE_CREATED = "created"
            PAYMENT_STATE_PENDING = "pending"
            PAYMENT_STATE_CONFIRMED = "confirmed"
            PAYMENT_STATE_FAILED = "failed"

        class OrderRefund:
            pass

        class Quota:
            class QuotaExceededException(Exception):
                pass

        class BasePaymentProvider:
            def __init__(self, event):
                self.event = event

            @property
            def settings(self):
                return self.event.settings

            @property
            def settings_form_fields(self):
                return {}

        class PaymentException(Exception):
            pass

        class LockTimeoutException(Exception):
            pass

        models_mod.Order = Order
        models_mod.OrderPayment = OrderPayment
        models_mod.OrderRefund = OrderRefund
        models_mod.Quota = Quota
        payment_mod.BasePaymentProvider = BasePaymentProvider
        payment_mod.PaymentException = PaymentException
        locking_mod.LockTimeoutException = LockTimeoutException
        permissions_mod.event_permission_required = lambda perm: lambda fn: fn
        urlreverse_mod.build_absolute_uri = lambda event, name, kwargs=None: (
            f"https://example.invalid/{name}"
        )
        urlreverse_mod.eventreverse = lambda event, name, kwargs=None: f"/{name}/"

        for name, mod in {
            "eventyay": eventyay_mod,
            "eventyay.base": base_mod,
            "eventyay.base.models": models_mod,
            "eventyay.base.payment": payment_mod,
            "eventyay.base.services": services_mod,
            "eventyay.base.services.locking": locking_mod,
            "eventyay.control": control_mod,
            "eventyay.control.permissions": permissions_mod,
            "eventyay.multidomain": multidomain_mod,
            "eventyay.multidomain.urlreverse": urlreverse_mod,
        }.items():
            monkeypatch.setitem(sys.modules, name, mod)

    if "eventyay_bitpay.models" not in sys.modules:
        plugin_models = types.ModuleType("eventyay_bitpay.models")

        class ReferencedBitPayObject:
            class DoesNotExist(Exception):
                pass

            objects = SimpleNamespace()

        plugin_models.ReferencedBitPayObject = ReferencedBitPayObject
        monkeypatch.setitem(sys.modules, "eventyay_bitpay.models", plugin_models)

    payment_module = importlib.import_module("eventyay_bitpay.payment")
    views_module = importlib.import_module("eventyay_bitpay.views")
    return payment_module, views_module


def test_drops_email_token_and_unknown_fields(captured):
    log_operation(
        "plugin.loaded",
        OUTCOME_SUCCESS,
        backend="bitpay",
        email="person@example.com",
        token="secret-token",
        body="{raw}",
        event_id=12,
    )
    assert len(captured) == 1
    extra = captured[0][2]
    assert extra["backend"] == "bitpay"
    assert extra["event_id"] == 12
    assert extra["outcome"] == "success"
    assert "email" not in extra
    assert "token" not in extra
    assert "body" not in extra
    blob = str(extra)
    assert "person@example.com" not in blob
    assert "secret-token" not in blob


def test_rejects_unsafe_action_and_backend(captured):
    log_operation("has space", OUTCOME_SUCCESS, backend="bitpay")
    log_operation(
        "connection.request", OUTCOME_FAILURE, backend="bit pay", error_code="not safe"
    )
    assert len(captured) == 1
    assert captured[0][1] == "connection.request"
    assert "backend" not in captured[0][2]
    assert "error_code" not in captured[0][2]
    assert "bit pay" not in str(captured)
    assert "not safe" not in str(captured)


def test_host_emitter_falls_back_to_logger_on_exception(monkeypatch):
    records = []

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    def broken_emitter(*args, **kwargs):
        raise RuntimeError("host emitter failed")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", broken_emitter)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)

    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="bitpay")
    assert len(records) == 1
    assert records[0][1] == "plugin.loaded"
    assert records[0][2]["backend"] == "bitpay"


def test_resolve_host_emitter_handles_non_import_error(monkeypatch):
    real_import = builtins.__import__

    def exploding_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "eventyay.base.operational_logging" or (
            name == "eventyay.base" and fromlist and "operational_logging" in fromlist
        ):
            raise RuntimeError("broken host module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", exploding_import)
    assert _resolve_host_emitter() is None


def test_logging_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", boom)
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="bitpay")


def test_logged_request_records_status_timeout_and_no_url(captured, monkeypatch):
    seen_kwargs = []

    class Response:
        status_code = 201

    def fake_post(url, **kwargs):
        seen_kwargs.append(kwargs)
        assert url == "https://example.invalid/secret"
        assert kwargs["json"]["token"] == "raw"
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    logged_request(
        "bitpay", "POST", "https://example.invalid/secret", json={"token": "raw"}
    )
    assert seen_kwargs[0]["timeout"] == 30

    logged_request(
        "bitpay",
        "POST",
        "https://example.invalid/secret",
        json={"token": "raw"},
        timeout=12,
    )
    assert seen_kwargs[1]["timeout"] == 12

    assert captured[-1][1] == "connection.request"
    extra = captured[-1][2]
    assert extra["status"] == 201
    assert extra["outcome"] == "success"
    assert "example.invalid" not in str(extra)
    assert "raw" not in str(extra)


def test_refund_redacts_error_payload_and_uses_timeout(captured, monkeypatch, caplog):
    payment_module, _ = _load_plugin_modules(monkeypatch)
    seen = {}

    class FakeResponse:
        ok = False
        status_code = 422

        def json(self):
            return {"error": "secret-refund-token-leaked"}

    def fake_post(url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    provider = payment_module.BitPay(
        SimpleNamespace(
            pk=7,
            settings=SimpleNamespace(
                url="https://bitpay.example", pem="pem", token="t"
            ),
        )
    )
    provider.__dict__["client"] = SimpleNamespace(
        host="https://bitpay.example",
        pem="fake-pem",
        verify=True,
    )

    btcpay_crypto = types.ModuleType("btcpay.crypto")
    btcpay_crypto.get_compressed_public_key_from_pem = lambda pem: "id"
    btcpay_crypto.sign = lambda msg, pem: "sig"
    btcpay_mod = types.ModuleType("btcpay")
    btcpay_mod.crypto = btcpay_crypto
    monkeypatch.setitem(sys.modules, "btcpay", btcpay_mod)
    monkeypatch.setitem(sys.modules, "btcpay.crypto", btcpay_crypto)

    refund = SimpleNamespace(
        order_id=99,
        order=SimpleNamespace(email="buyer@example.com"),
        payment=SimpleNamespace(
            info_data={"token": "tok", "price": 10, "currency": "USD", "id": "inv-1"}
        ),
    )

    with (
        caplog.at_level(logging.ERROR, logger=payment_module.logger.name),
        pytest.raises(payment_module.PaymentException) as exc_info,
    ):
        provider._refund(refund)

    assert "secret-refund-token-leaked" in str(exc_info.value)
    assert "secret-refund-token-leaked" not in caplog.text
    assert "status=422" in caplog.text
    assert seen["timeout"] == 30
    assert captured[-1][1] == "connection.request"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["status"] == 422


def test_execute_payment_logs_success_only_after_persistence(captured, monkeypatch):
    payment_module, _ = _load_plugin_modules(monkeypatch)

    provider = payment_module.BitPay(
        SimpleNamespace(
            pk=11,
            currency="USD",
            settings=SimpleNamespace(token="tok"),
        )
    )
    provider.__dict__["client"] = SimpleNamespace(
        create_invoice=lambda payload: {"id": "inv-1"}  # missing "url"
    )
    monkeypatch.setattr(
        payment_module.ReferencedBitPayObject,
        "objects",
        SimpleNamespace(get_or_create=lambda **kwargs: (None, True)),
        raising=False,
    )

    payment = SimpleNamespace(
        pk=5,
        amount=10,
        order_id=42,
        info="",
        order=SimpleNamespace(full_code="ABC-1", code="ABC", secret="sec"),
        save=lambda **kwargs: None,
    )
    request = SimpleNamespace(session={})

    with pytest.raises(KeyError):
        provider.execute_payment(request, payment)

    assert all(item[2]["outcome"] != OUTCOME_SUCCESS for item in captured)
    assert captured[-1][1] == "payment.charge"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "response_error"


def test_webhook_logs_invalid_payload_quota_rejection_and_success(
    captured, monkeypatch
):
    _, views_module = _load_plugin_modules(monkeypatch)
    quota_exc = views_module.Quota.QuotaExceededException

    event = SimpleNamespace(pk=3)
    bad_req = SimpleNamespace(method="POST", body=b"{not-json", event=event)
    with pytest.raises(json.JSONDecodeError):
        views_module.webhook(bad_req)
    assert captured[-1][1] == "webhook.inbound"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "invalid_payload"

    missing_id_req = SimpleNamespace(method="POST", body=b'{"data": {}}', event=event)
    with pytest.raises(KeyError):
        views_module.webhook(missing_id_req)
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "invalid_payload"

    fake_order = SimpleNamespace(
        event=event,
        event_id=3,
        log_action=lambda *args, **kwargs: None,
        refresh_from_db=lambda: None,
    )
    fake_payment = SimpleNamespace(
        state=views_module.OrderPayment.PAYMENT_STATE_CREATED,
        refresh_from_db=lambda: None,
    )
    fake_rso = SimpleNamespace(order=fake_order, order_id=88, payment=fake_payment)
    monkeypatch.setattr(
        views_module.ReferencedBitPayObject,
        "objects",
        SimpleNamespace(
            select_related=lambda *a: SimpleNamespace(get=lambda **kw: fake_rso)
        ),
        raising=False,
    )

    class FakeBitPay:
        def __init__(self, ev):
            self.client = SimpleNamespace(
                get_invoice=lambda invoice_id: {"status": "paid", "amount": "10.00"}
            )

    monkeypatch.setattr(views_module, "BitPay", FakeBitPay)
    monkeypatch.setattr(
        views_module,
        "transaction",
        SimpleNamespace(atomic=contextlib.nullcontext),
    )

    # Event mismatch branch must log failure without touching process_invoice.
    other_event = SimpleNamespace(pk=999)
    mismatch_req = SimpleNamespace(
        method="POST", body=b'{"id": "inv-1"}', event=other_event
    )
    mismatch_resp = views_module.webhook(mismatch_req)
    assert mismatch_resp.status_code == 200
    assert mismatch_resp.content == b"Unable to detect event"
    assert captured[-1][1] == "webhook.inbound"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "event_mismatch"

    def confirm_quota():
        raise quota_exc()

    fake_payment.confirm = confirm_quota
    quota_req = SimpleNamespace(method="POST", body=b'{"id": "inv-1"}', event=event)
    resp = views_module.webhook(quota_req)
    assert resp.status_code == 200
    assert resp.content == b"Quota exceeded."
    assert captured[-1][1] == "webhook.inbound"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "quota_exceeded"
    assert captured[-1][2]["status"] == 200

    fake_payment.confirm = lambda: None
    ok_resp = views_module.webhook(quota_req)
    assert ok_resp.status_code == 200
    assert captured[-1][1] == "webhook.inbound"
    assert captured[-1][2]["outcome"] == OUTCOME_SUCCESS
    assert captured[-1][2]["status"] == 200


def test_auth_start_validates_status_and_pairing_payload(captured, monkeypatch):
    _, views_module = _load_plugin_modules(monkeypatch)

    btcpay_crypto = types.ModuleType("btcpay.crypto")
    btcpay_crypto.generate_privkey = lambda: "priv"
    btcpay_crypto.get_sin_from_pem = lambda pem: "sin-123"
    btcpay_mod = types.ModuleType("btcpay")
    btcpay_mod.crypto = btcpay_crypto
    monkeypatch.setitem(sys.modules, "btcpay", btcpay_mod)
    monkeypatch.setitem(sys.modules, "btcpay.crypto", btcpay_crypto)

    monkeypatch.setattr(views_module, "reverse", lambda *a, **kw: "/control/bitpay/")
    monkeypatch.setattr(
        views_module,
        "redirect",
        lambda to: SimpleNamespace(status_code=302, url=to),
    )
    monkeypatch.setattr(views_module.messages, "error", lambda *a, **kw: None)

    def make_request():
        return SimpleNamespace(
            GET={},
            session={},
            event=SimpleNamespace(
                pk=5,
                slug="ev",
                organizer=SimpleNamespace(slug="org"),
                settings=SimpleNamespace(payment_bitpay_token=None),
            ),
        )

    # Non-200 (e.g. 201) must log failure, not success.
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=201, json=dict),
    )
    views_module.auth_start(make_request())
    assert captured[-1][1] == "connection.request"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "http_error"
    assert captured[-1][2]["status"] == 201

    # 200 with malformed payload must log failure and raise.
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=200, json=lambda: {"data": [{"token": "only-token"}]}
        ),
    )
    with pytest.raises(KeyError):
        views_module.auth_start(make_request())
    assert captured[-1][1] == "connection.request"
    assert captured[-1][2]["outcome"] == OUTCOME_FAILURE
    assert captured[-1][2]["error_code"] == "response_error"

    # 200 with valid token + pairingCode logs success without leaking secrets.
    req = make_request()
    seen_timeouts = []

    def ok_post(*a, **kw):
        seen_timeouts.append(kw.get("timeout"))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"token": "secret-tok", "pairingCode": "pair-xyz"}]},
        )

    monkeypatch.setattr(requests, "post", ok_post)
    redirect_resp = views_module.auth_start(req)
    assert redirect_resp.status_code == 302
    assert "pair-xyz" in redirect_resp.url
    assert req.event.settings.payment_bitpay_token == "secret-tok"
    assert seen_timeouts == [30]
    assert captured[-1][1] == "connection.request"
    assert captured[-1][2]["outcome"] == OUTCOME_SUCCESS
    assert captured[-1][2]["status"] == 200
    assert "secret-tok" not in str(captured[-1])
    assert "pair-xyz" not in str(captured[-1])

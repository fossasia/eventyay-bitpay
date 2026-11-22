import functools
import hashlib
import json
import logging
import time
import urllib.parse
from decimal import Decimal

import requests
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from eventyay.base.models import Order, OrderPayment, Quota
from eventyay.base.services.locking import LockTimeoutException
from eventyay.control.permissions import event_permission_required
from eventyay.multidomain.urlreverse import build_absolute_uri, eventreverse

from eventyay_bitpay.operational_log import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    log_operation,
)

from .models import ReferencedBitPayObject
from .payment import BitPay

logger = logging.getLogger(__name__)


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    try:
        data = signing.loads(request.GET.get("data", ""), salt="safe-redirect")
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid parameter")

    if "go" in request.GET:
        if "session" in data:
            for k, v in data["session"].items():
                request.session[k] = v
        return redirect(data["url"])
    else:
        params = request.GET.copy()
        params["go"] = "1"
        r = render(
            request,
            "eventyay_bitpay/redirect.html",
            {
                "url": build_absolute_uri(
                    request.event, "plugins:eventyay_bitpay:redirect"
                )
                + "?"
                + urllib.parse.urlencode(params),
            },
        )
        r._csp_ignore = True
        return r


def _validate_bitpay_url(raw_url):
    candidate = str(raw_url).strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in ("https", "http"):
        return None
    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        return None
    if (
        not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _log_permission_denied(route_name):
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except PermissionDenied:
                user = getattr(request, "user", None)
                if user is not None and getattr(user, "is_authenticated", False):
                    event_id = getattr(getattr(request, "event", None), "pk", None)
                    log_operation(
                        "permission.denied",
                        OUTCOME_FAILURE,
                        backend="bitpay",
                        payment_provider="bitpay",
                        error_code="forbidden",
                        model="bitpay_settings",
                        route=route_name,
                        event_id=event_id,
                        object_id=event_id,
                        user_id=getattr(user, "pk", None),
                    )
                raise

        return wrapper

    return decorator


def _parse_pairing_response(response, base_url):
    payload = response.json()
    data = payload["data"][0]
    token = data["token"]
    pairing_code = data["pairingCode"]
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(pairing_code, str)
        or not pairing_code
    ):
        raise ValueError("Invalid BitPay pairing response")
    encoded_code = urllib.parse.quote(pairing_code, safe="")
    return token, base_url + "/api-access-request?pairingCode=" + encoded_code


@csrf_exempt
@require_POST
def webhook(request, *args, **kwargs):
    try:
        event_json = json.loads(request.body.decode("utf-8"))
        if "data" not in event_json and "id" in event_json:
            objid = event_json["id"]
        else:
            objid = event_json["data"]["id"]
    except Exception:
        log_operation(
            "webhook.inbound",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            error_code="invalid_payload",
            event_id=getattr(getattr(request, "event", None), "pk", None),
        )
        raise
    try:
        rso = ReferencedBitPayObject.objects.select_related(
            "order", "order__event"
        ).get(reference=objid)
        if rso.order.event != request.event:
            log_operation(
                "webhook.inbound",
                OUTCOME_FAILURE,
                backend="bitpay",
                payment_provider="bitpay",
                error_code="event_mismatch",
                status=200,
                event_id=getattr(request.event, "pk", None),
                order_id=rso.order_id,
            )
            return HttpResponse("Unable to detect event", status=200)
        rso.order.log_action("eventyay_bitpay.event", data=event_json)
        try:
            response = process_invoice(rso.order, rso.payment, objid)
        except Exception:
            log_operation(
                "webhook.inbound",
                OUTCOME_FAILURE,
                backend="bitpay",
                payment_provider="bitpay",
                error_code="processing_error",
                event_id=rso.order.event_id,
                order_id=rso.order_id,
            )
            raise
        status = getattr(response, "status_code", None)
        explicit_outcome = getattr(response, "_operational_outcome", None)
        explicit_error = getattr(response, "_operational_error_code", None)
        failed = explicit_outcome == OUTCOME_FAILURE or (
            isinstance(status, int) and status >= 400
        )
        log_operation(
            "webhook.inbound",
            OUTCOME_FAILURE if failed else OUTCOME_SUCCESS,
            backend="bitpay",
            payment_provider="bitpay",
            error_code=(explicit_error or "processing_error") if failed else None,
            status=status if isinstance(status, int) else None,
            event_id=rso.order.event_id,
            order_id=rso.order_id,
        )
        return response
    except ReferencedBitPayObject.DoesNotExist:
        log_operation(
            "webhook.inbound",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            error_code="unknown_reference",
            status=200,
            event_id=getattr(getattr(request, "event", None), "pk", None),
        )
        return HttpResponse("Unable to detect event", status=200)


def process_invoice(order, payment, invoice_id):
    prov = BitPay(order.event)
    event_id = getattr(order, "event_id", None)
    order_id = getattr(order, "pk", None)
    started = time.monotonic()
    try:
        src = prov.client.get_invoice(invoice_id)
    except requests.RequestException as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        status_ok = isinstance(status, int) and not isinstance(status, bool)
        error_code = (
            "timeout"
            if isinstance(exc, requests.Timeout)
            else ("http_error" if status_ok else "request_error")
        )
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            error_code=error_code,
            status=status if status_ok else None,
            duration_ms=duration_ms,
            event_id=event_id,
            order_id=order_id,
        )
        raise
    log_operation(
        "connection.request",
        OUTCOME_SUCCESS,
        backend="bitpay",
        payment_provider="bitpay",
        duration_ms=int((time.monotonic() - started) * 1000),
        event_id=event_id,
        order_id=order_id,
    )

    if not payment:
        payment = order.payments.filter(
            info__icontains=invoice_id,
            provider="bitpay",
        ).last()
    if not payment:
        payment = order.payments.create(
            state=OrderPayment.PAYMENT_STATE_CREATED,
            provider="bitpay",
            amount=Decimal(src["amount"]),
            info=json.dumps(src),
        )

    with transaction.atomic():
        order.refresh_from_db()
        payment.refresh_from_db()

        if src["status"] == "new":
            pass
        elif src["status"] in ("paid", "confirmed", "complete"):
            if payment.state in (
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
            ):
                try:
                    payment.confirm()
                except LockTimeoutException:
                    response = HttpResponse(
                        "Lock timeout, please try again.", status=503
                    )
                    response._operational_outcome = OUTCOME_FAILURE
                    response._operational_error_code = "lock_timeout"
                    return response
                except Quota.QuotaExceededException:
                    response = HttpResponse("Quota exceeded.", status=200)
                    response._operational_outcome = OUTCOME_FAILURE
                    response._operational_error_code = "quota_exceeded"
                    return response
        elif src["status"] == ("expired", "invalid"):
            if payment.state in (
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
            ):
                payment.state = OrderPayment.PAYMENT_STATE_FAILED
                payment.info = json.dumps(src)
                payment.save()
                payment.order.log_action(
                    "eventyay.event.order.payment.failed",
                    {
                        "local_id": payment.local_id,
                        "provider": payment.provider,
                        "info": json.dumps(src),
                    },
                )
            elif order.state in OrderPayment.PAYMENT_STATE_CONFIRMED:
                payment.state = OrderPayment.PAYMENT_STATE_FAILED
                payment.info = json.dumps(src)
                payment.save()
                payment.create_external_refund()
                payment.order.log_action(
                    "eventyay.event.order.payment.failed",
                    {
                        "local_id": payment.local_id,
                        "provider": payment.provider,
                        "info": json.dumps(src),
                    },
                )

    return HttpResponse(status=200)


@_log_permission_denied("bitpay.auth_disconnect")
@event_permission_required("can_change_event_settings")
@require_POST
def auth_disconnect(request, **kwargs):
    del request.event.settings.payment_bitpay_token
    request.event.settings.payment_bitpay__enabled = False
    log_operation(
        "connection.disconnect",
        OUTCOME_SUCCESS,
        backend="bitpay",
        payment_provider="bitpay",
        event_id=request.event.pk,
    )
    messages.success(request, _("Your BitPay account has been disconnected."))

    return redirect(
        reverse(
            "control:event.settings.payment.provider",
            kwargs={
                "organizer": request.event.organizer.slug,
                "event": request.event.slug,
                "provider": "bitpay",
            },
        )
    )


class BitPayOrderView:
    def dispatch(self, request, *args, **kwargs):
        try:
            self.order = request.event.orders.get(code=kwargs["order"])
            if (
                hashlib.sha1(self.order.secret.lower().encode()).hexdigest()
                != kwargs["hash"].lower()
            ):
                raise Http404("")
        except Order.DoesNotExist:
            # Do a hash comparison as well to harden timing attacks
            if (
                "abcdefghijklmnopq".lower()
                == hashlib.sha1(b"abcdefghijklmnopq").hexdigest()
            ):
                raise Http404("")
            else:
                raise Http404("")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def pprov(self):
        return self.payment.payment_provider

    @property
    def payment(self):
        return get_object_or_404(
            self.order.payments,
            pk=self.kwargs["payment"],
            provider__istartswith="bitpay",
        )


@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(BitPayOrderView, View):
    def get(self, request, *args, **kwargs):
        with transaction.atomic():
            self.order.refresh_from_db()
            if self.order.status == Order.STATUS_PAID:
                return self._redirect_to_order()

            payment_data = self.payment.info_data
            process_invoice(self.order, self.payment, payment_data["id"])
            return self._redirect_to_order()

    def _redirect_to_order(self):
        if self.request.session.get("payment_bitpay_order_secret") != self.order.secret:
            messages.error(
                self.request,
                _(
                    "Sorry, there was an error in the payment process. Please check the link "
                    "in your emails to continue."
                ),
            )
            return redirect(eventreverse(self.request.event, "presale:event.index"))

        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )


@_log_permission_denied("bitpay.auth_start")
@event_permission_required("can_change_event_settings")
def auth_start(request, **kwargs):
    from btcpay import crypto  # improve performance at import time

    if request.event.settings.payment_bitpay_token:
        messages.error(request, _("You are already connected to BitPay."))
        return redirect(
            reverse(
                "control:event.settings.payment.provider",
                kwargs={
                    "organizer": request.event.organizer.slug,
                    "event": request.event.slug,
                    "provider": "bitpay",
                },
            )
        )
    if request.GET.get("url"):
        url = _validate_bitpay_url(request.GET.get("url"))
        if url is None:
            log_operation(
                "validation.error",
                OUTCOME_FAILURE,
                backend="bitpay",
                payment_provider="bitpay",
                error_code="invalid_url",
                model="bitpay_settings",
                route="bitpay.auth_start",
                event_id=request.event.pk,
                object_id=request.event.pk,
            )
            messages.error(request, _("Communication with BitPay was not successful."))
            return redirect(
                reverse(
                    "control:event.settings.payment.provider",
                    kwargs={
                        "organizer": request.event.organizer.slug,
                        "event": request.event.slug,
                        "provider": "bitpay",
                    },
                )
            )
    else:
        url = (
            "https://test.bitpay.com" if "test" in request.GET else "https://bitpay.com"
        )
    request.session["payment_bitpay_auth_event"] = request.event.pk

    request.event.settings.payment_bitpay_pem = crypto.generate_privkey()

    pem = request.event.settings.payment_bitpay_pem
    sin = crypto.get_sin_from_pem(pem)
    started = time.monotonic()
    try:
        r = requests.post(
            url + "/tokens",
            json={
                "label": settings.INSTANCE_NAME,
                "facade": "merchant",
                "id": sin,
            },
            timeout=30,
        )
    except requests.Timeout:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            error_code="timeout",
            duration_ms=int((time.monotonic() - started) * 1000),
            event_id=request.event.pk,
        )
        messages.error(request, _("Communication with BitPay was not successful."))
    except requests.ConnectionError:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            error_code="request_error",
            duration_ms=int((time.monotonic() - started) * 1000),
            event_id=request.event.pk,
        )
        messages.error(request, _("Communication with BitPay was not successful."))
    else:
        if r.status_code == 200:
            try:
                token, pairing_url = _parse_pairing_response(r, url)
            except Exception:
                log_operation(
                    "connection.request",
                    OUTCOME_FAILURE,
                    backend="bitpay",
                    payment_provider="bitpay",
                    status=r.status_code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_code="response_error",
                    event_id=request.event.pk,
                )
                raise
            log_operation(
                "connection.request",
                OUTCOME_SUCCESS,
                backend="bitpay",
                payment_provider="bitpay",
                status=r.status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                event_id=request.event.pk,
            )
            request.event.settings.payment_bitpay_token = token
            request.event.settings.payment_bitpay_url = url
            return redirect(pairing_url)
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend="bitpay",
            payment_provider="bitpay",
            status=r.status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code="http_error",
            event_id=request.event.pk,
        )
        messages.error(request, _("Communication with BitPay was not successful."))

    return redirect(
        reverse(
            "control:event.settings.payment.provider",
            kwargs={
                "organizer": request.event.organizer.slug,
                "event": request.event.slug,
                "provider": "bitpay",
            },
        )
    )

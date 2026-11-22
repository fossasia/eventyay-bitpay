"""Privacy-safe operational logs for this plugin.

Delegates to the host logger when it is importable. Otherwise uses
``eventyay.plugins``. Logging never raises into product code.

Emails, names, tokens, secrets, and request or response bodies are dropped.
Only opaque IDs, status codes, durations, and safe error codes are kept.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar

import requests
import requests.api as requests_api

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOGGER = logging.getLogger("eventyay.plugins")
_request_id_var: ContextVar[str | None] = ContextVar(
    "eventyay_bitpay_request_id", default=None
)
_job_id_var: ContextVar[str | None] = ContextVar("eventyay_bitpay_job_id", default=None)

_INT_FIELDS = frozenset(
    {
        "event_id",
        "order_id",
        "user_id",
        "status",
        "duration_ms",
    }
)
_IDENTIFIER_FIELDS = frozenset(
    {
        "error_code",
        "backend",
        "payment_provider",
        "request_id",
        "job_id",
        "model",
        "route",
    }
)
_REQUEST_LOG_CONTEXT_FIELDS = frozenset(
    {
        "event_id",
        "order_id",
        "payment_provider",
        "request_id",
        "job_id",
    }
)
_REQUEST_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _resolve_host_logging():
    try:
        from eventyay.base.operational_logging import (
            get_job_id,
            get_request_id,
            log_event,
        )
    except Exception:  # noqa: BLE001
        return None, None, None
    return log_event, get_request_id, get_job_id


def _resolve_host_emitter():
    emitter, _, _ = _resolve_host_logging()
    return emitter


_HOST_LOG_EVENT, _HOST_GET_REQUEST_ID, _HOST_GET_JOB_ID = _resolve_host_logging()


def _safe_identifier(value):
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def bind_request_id(request_id: str | None) -> None:
    _request_id_var.set(_safe_identifier(request_id))


def reset_request_id() -> None:
    _request_id_var.set(None)


def bind_job_id(job_id: str | None) -> None:
    _job_id_var.set(_safe_identifier(job_id))


def reset_job_id() -> None:
    _job_id_var.set(None)


def _read_correlation_id(
    host_getter, fallback_var: ContextVar[str | None]
) -> str | None:
    if host_getter is not None:
        try:
            host_id = _safe_identifier(host_getter())
        except Exception:  # noqa: BLE001
            host_id = None
        if host_id is not None:
            return host_id
    return _safe_identifier(fallback_var.get())


def get_request_id() -> str | None:
    return _read_correlation_id(_HOST_GET_REQUEST_ID, _request_id_var)


def get_job_id() -> str | None:
    return _read_correlation_id(_HOST_GET_JOB_ID, _job_id_var)


def _clean(fields):
    extra = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            extra[key] = value
            continue
        if key in _IDENTIFIER_FIELDS:
            cleaned = _safe_identifier(value if isinstance(value, str) else None)
            if cleaned is not None:
                extra[key] = cleaned
            continue
        if key == "object_id":
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                extra[key] = value
            else:
                cleaned = _safe_identifier(str(value))
                if cleaned is not None:
                    extra[key] = cleaned
    return extra


def log_operation(action, outcome, **fields):
    """Emit one allowlisted operational line. Failures are swallowed."""
    try:
        if not isinstance(action, str) or _safe_identifier(action) is None:
            return
        if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE):
            outcome = OUTCOME_FAILURE
        level = logging.WARNING if outcome == OUTCOME_FAILURE else logging.INFO
        payload = _clean(fields)
        if "request_id" not in payload:
            req_id = get_request_id()
            if req_id is not None:
                payload["request_id"] = req_id
        if "job_id" not in payload:
            job_id = get_job_id()
            if job_id is not None:
                payload["job_id"] = job_id
        emitter = _HOST_LOG_EVENT
        if emitter is not None:
            try:
                emitter("plugins", action, outcome, level=level, **payload)
            except Exception:  # noqa: BLE001
                emitter = None
            else:
                return
        extra = {
            "component": "plugins",
            "outcome": outcome,
            "action": action,
            **payload,
        }
        _LOGGER.log(level, action, extra=extra)
    except Exception:  # noqa: BLE001
        return


def log_plugin_loaded(backend):
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend=backend)


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def _send_request(method, url, **kwargs):
    """Call requests without changing mocks or redirect defaults.

    A patched ``requests.get`` or ``requests.post`` wins. Otherwise a patched
    ``requests.request`` wins. Unpatched calls use the verb helper.
    """
    verb_name = str(method).lower()
    if verb_name in _REQUEST_VERBS:
        patched = getattr(requests, verb_name, None)
        real = getattr(requests_api, verb_name, None)
        if patched is not None and patched is not real:
            return patched(url, **kwargs)  # codeql[py/full-ssrf]
    if requests.request is not requests_api.request:
        return requests.request(method, url, **kwargs)  # codeql[py/full-ssrf]
    if verb_name in _REQUEST_VERBS:
        real = getattr(requests_api, verb_name)
        return real(url, **kwargs)  # codeql[py/full-ssrf]
    return requests_api.request(method, url, **kwargs)  # codeql[py/full-ssrf]


def logged_request(backend, method, url, **kwargs):
    """Log status and duration for one HTTP call.

    ``ok_statuses`` and safe logging context kwargs (``event_id``, ``order_id``,
    ``payment_provider``, ``request_id``, ``job_id``) are not sent to the server.
    The same exception is re-raised. The URL and body are not logged.
    """
    ok_statuses = kwargs.pop("ok_statuses", ())
    log_context = {
        key: kwargs.pop(key) for key in _REQUEST_LOG_CONTEXT_FIELDS if key in kwargs
    }
    kwargs.setdefault("timeout", 30)
    started = time.monotonic()
    try:
        response = _send_request(method, url, **kwargs)
    except requests.Timeout:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend=backend,
            error_code="timeout",
            duration_ms=_elapsed_ms(started),
            **log_context,
        )
        raise
    except requests.RequestException:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend=backend,
            error_code="request_error",
            duration_ms=_elapsed_ms(started),
            **log_context,
        )
        raise
    status = getattr(response, "status_code", None)
    status_ok = isinstance(status, int) and not isinstance(status, bool)
    tolerated = status_ok and status in set(ok_statuses)
    failed = status_ok and status >= 400 and not tolerated
    log_operation(
        "connection.request",
        OUTCOME_FAILURE if failed else OUTCOME_SUCCESS,
        backend=backend,
        status=status if status_ok else None,
        duration_ms=_elapsed_ms(started),
        error_code="http_error" if failed else None,
        **log_context,
    )
    return response

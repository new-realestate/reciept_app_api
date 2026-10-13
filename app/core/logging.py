"""Application logging: one setup, request correlation and secret redaction.

- `setup_logging()` configures the root logger once (plain text or JSON lines).
- `RequestLogMiddleware` assigns each request an id (honouring a sane incoming
  `X-Request-ID`), returns it in the response, logs one line per request, and puts the
  id on every log record emitted while handling it.
- `RedactingFilter` strips secrets from every record, including uvicorn's access log:
  public document tokens in `/i/{token}` and `/q/{token}` paths, bearer tokens, and
  values of password/secret/token-like key=value pairs.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REDACTED = "[redacted]"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(/[iq]/)[A-Za-z0-9_\-]{8,}"), r"\1" + REDACTED),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b((?:password|passwd|secret|token|api[_-]?key|refresh_token|authorization)\s*[=:]\s*)[^\s,;&\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&](?:token|key|password)=)[^&\s]+"), r"\1" + REDACTED),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_value(v) for k, v in record.args.items()}
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"
_configured = False


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotent: safe to call from the app module and from tests."""
    global _configured
    root = logging.getLogger()
    root.setLevel(level.upper())
    if _configured:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter(_TEXT_FORMAT))
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]

    # uvicorn installs its own handlers (and logs full paths); route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Our middleware writes the access line (with request id and timing), so silence uvicorn's duplicate.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs full request URLs at INFO; keep provider calls quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
access_logger = logging.getLogger("app.access")


class RequestLogMiddleware:
    """Pure ASGI middleware (no response buffering, unlike BaseHTTPMiddleware)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            access_logger.exception("%s %s failed", scope["method"], scope["path"])
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            client = scope.get("client")
            level = logging.WARNING if status_holder["status"] >= 500 else logging.INFO
            access_logger.log(
                level,
                "%s %s -> %d in %.0fms (client=%s)",
                scope["method"], scope["path"], status_holder["status"], elapsed_ms, client[0] if client else "-",
            )
            request_id_var.reset(token)

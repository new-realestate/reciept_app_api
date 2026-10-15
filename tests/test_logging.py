import json
import logging

import pytest

from app.core.logging import JsonFormatter, RedactingFilter, redact


def test_redacts_public_tokens_bearer_and_secrets():
    token = "AbCdEf123456_-xyzXYZ789"
    assert token not in redact(f"GET /i/{token}/pdf")
    assert token not in redact(f"POST /q/{token}/accept")
    assert "eyJhbGciOi.payload.sig" not in redact("Authorization: Bearer eyJhbGciOi.payload.sig")
    assert "hunter2" not in redact("login password=hunter2 ok")
    assert "abc123" not in redact("GET /x?token=abc123&a=1")
    assert redact("GET /invoices/42") == "GET /invoices/42"  # ordinary paths are untouched


def test_filter_redacts_args_of_access_style_records():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s"', ("1.2.3.4", "GET", "/i/SECRETTOKEN12345"), None)
    RedactingFilter().filter(record)
    assert "SECRETTOKEN12345" not in record.getMessage()


def test_json_formatter_outputs_one_object_with_request_id():
    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    record.request_id = "req-1"
    data = json.loads(JsonFormatter().format(record))
    assert data["message"] == "hello world" and data["level"] == "WARNING" and data["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_request_id_header_is_returned_and_honoured(client):
    first = await client.get("/health")
    assert first.status_code == 200 and len(first.headers["x-request-id"]) >= 8
    second = await client.get("/health", headers={"X-Request-ID": "trace-abc-12345"})
    assert second.headers["x-request-id"] == "trace-abc-12345"
    unsafe = await client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert unsafe.headers["x-request-id"] != "bad id\twith spaces"


@pytest.mark.asyncio
async def test_request_log_line_never_contains_the_public_token(client, caplog):
    caplog.set_level(logging.INFO, logger="app.access")
    caplog.handler.addFilter(RedactingFilter())
    resp = await client.get("/i/NOTAREALTOKEN1234567890")
    assert resp.status_code == 404
    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert lines and all("NOTAREALTOKEN1234567890" not in line for line in lines)
    assert any("404" in line for line in lines)

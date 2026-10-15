from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio

TODAY = date.today()


def _body(**overrides):
    body = {
        "customer_name": "Grace Tenant",
        "customer_email": "grace@example.com",
        "customer_phone": "+256701234567",
        "title": "Rent for November",
        "currency": "UGX",
        "due_date": str(TODAY + timedelta(days=14)),
        "line_items": [
            {"description": "Monthly rent", "quantity": "1", "unit_price_cents": 50000000},
        ],
    }
    body.update(overrides)
    return body


async def _issued(client, headers, **overrides):
    resp = await client.post("/invoices", json=_body(issue_now=True, **overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ creation / drafts / numbering


async def test_create_draft_has_no_number_until_issued(client, signup):
    headers = await signup()
    resp = await client.post("/invoices", json=_body(), headers=headers)
    assert resp.status_code == 201
    inv = resp.json()
    assert inv["status"] == "draft" and inv["number"] is None
    assert inv["total_cents"] == 50000000
    assert inv["outstanding_cents"] == 50000000

    issued = await client.post(f"/invoices/{inv['id']}/issue", headers=headers)
    assert issued.status_code == 200
    assert issued.json()["number"].endswith("-000001")
    assert issued.json()["display_status"] == "issued"
    assert issued.json()["public_url"]


async def test_invoice_numbers_are_sequential_per_business(client, signup):
    headers_a = await signup("a@example.com", "Biz A")
    headers_b = await signup("b@example.com", "Biz B")
    a1 = await _issued(client, headers_a)
    a2 = await _issued(client, headers_a)
    b1 = await _issued(client, headers_b)
    assert a1["number"].endswith("000001") and a2["number"].endswith("000002")
    assert b1["number"].endswith("000001")


async def test_server_computes_totals_with_discount_and_tax(client, signup):
    headers = await signup()
    resp = await client.post(
        "/invoices",
        json=_body(
            line_items=[
                {"description": "Service", "quantity": "2", "unit_price_cents": 10000, "tax_rate": "18"},
                {"description": "Parts", "quantity": "1", "unit_price_cents": 5000},
            ],
            discount_percent="10",
        ),
        headers=headers,
    )
    assert resp.status_code == 201
    inv = resp.json()
    assert inv["subtotal_cents"] == 25000
    assert inv["discount_cents"] == 2500
    assert inv["tax_cents"] == 3240  # 3600 * 0.9
    assert inv["total_cents"] == 25000 - 2500 + 3240
    assert [li["line_total_cents"] for li in inv["line_items"]] == [20000, 5000]


async def test_validation_errors(client, signup):
    headers = await signup()
    bad_qty = _body(line_items=[{"description": "x", "quantity": "0", "unit_price_cents": 100}])
    assert (await client.post("/invoices", json=bad_qty, headers=headers)).status_code == 422
    bad_due = _body(issue_date=str(TODAY), due_date=str(TODAY - timedelta(days=1)))
    assert (await client.post("/invoices", json=bad_due, headers=headers)).status_code == 422
    too_much = _body(discount_cents=999999999)
    assert (await client.post("/invoices", json=too_much, headers=headers)).status_code == 422
    no_contact = _body(customer_email=None, customer_phone=None)
    assert (await client.post("/invoices", json=no_contact, headers=headers)).status_code == 422


async def test_draft_can_be_edited_but_issued_cannot(client, signup):
    headers = await signup()
    draft = (await client.post("/invoices", json=_body(), headers=headers)).json()
    edit = await client.patch(
        f"/invoices/{draft['id']}",
        json={"line_items": [{"description": "New", "quantity": "2", "unit_price_cents": 1000}], "notes": "Thanks"},
        headers=headers,
    )
    assert edit.status_code == 200
    assert edit.json()["total_cents"] == 2000 and edit.json()["notes"] == "Thanks"

    await client.post(f"/invoices/{draft['id']}/issue", headers=headers)
    blocked = await client.patch(f"/invoices/{draft['id']}", json={"notes": "changed"}, headers=headers)
    assert blocked.status_code == 409


# ------------------------------------------------------------------ payments


async def test_partial_then_full_payment_generates_receipts(client, signup, outbox):
    headers = await signup()
    inv = await _issued(client, headers, line_items=[{"description": "Rent", "quantity": "1", "unit_price_cents": 50000000}])

    pay1 = await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 20000000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers={**headers, "Idempotency-Key": "k1"},
    )
    assert pay1.status_code == 201, pay1.text
    body = pay1.json()
    assert body["invoice"]["amount_paid_cents"] == 20000000
    assert body["invoice"]["outstanding_cents"] == 30000000
    assert body["invoice"]["display_status"] == "partially_paid"
    assert body["receipt_url"].endswith(body["receipt_url"].split("/")[-1])

    receipt = (await client.get(f"/receipts/{body['receipt_id']}", headers=headers)).json()
    assert receipt["amount_cents"] == 20000000
    assert receipt["balance_cents"] == 30000000
    assert inv["number"] in receipt["reason"]

    pay2 = await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 30000000, "paid_at": str(TODAY), "payment_method": "mobile_money", "payment_reference": "MM123"},
        headers={**headers, "Idempotency-Key": "k2"},
    )
    assert pay2.status_code == 201
    assert pay2.json()["invoice"]["display_status"] == "paid"
    assert pay2.json()["invoice"]["outstanding_cents"] == 0
    assert len(pay2.json()["invoice"]["payments"]) == 2

    receipts = (await client.get("/receipts", headers=headers)).json()
    assert len(receipts) == 2
    # customers got a confirmation each time
    assert any("Payment received" in kw["subject"] for kind, kw in outbox if kind == "email")


async def test_overpayment_is_rejected(client, signup):
    headers = await signup()
    inv = await _issued(client, headers)
    resp = await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": inv["total_cents"] + 1, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert (await client.get("/receipts", headers=headers)).json() == []


async def test_duplicate_payment_with_same_idempotency_key_is_replayed(client, signup):
    headers = await signup()
    inv = await _issued(client, headers)
    payload = {"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"}
    h = {**headers, "Idempotency-Key": "same-key"}
    first = await client.post(f"/invoices/{inv['id']}/payments", json=payload, headers=h)
    second = await client.post(f"/invoices/{inv['id']}/payments", json=payload, headers=h)
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["payment"]["id"] == first.json()["payment"]["id"]
    detail = (await client.get(f"/invoices/{inv['id']}", headers=headers)).json()
    assert detail["amount_paid_cents"] == 1000
    assert len((await client.get("/receipts", headers=headers)).json()) == 1


async def test_payment_rules(client, signup):
    headers = await signup()
    draft = (await client.post("/invoices", json=_body(), headers=headers)).json()
    pay = {"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"}
    assert (await client.post(f"/invoices/{draft['id']}/payments", json=pay, headers=headers)).status_code == 409

    inv = await _issued(client, headers)
    future = {**pay, "paid_at": str(TODAY + timedelta(days=3))}
    assert (await client.post(f"/invoices/{inv['id']}/payments", json=future, headers=headers)).status_code == 422
    bad_method = {**pay, "payment_method": "bitcoin"}
    assert (await client.post(f"/invoices/{inv['id']}/payments", json=bad_method, headers=headers)).status_code == 422
    zero = {**pay, "amount_cents": 0}
    assert (await client.post(f"/invoices/{inv['id']}/payments", json=zero, headers=headers)).status_code == 422

    full = {**pay, "amount_cents": inv["total_cents"]}
    assert (await client.post(f"/invoices/{inv['id']}/payments", json=full, headers=headers)).status_code == 201
    again = await client.post(f"/invoices/{inv['id']}/payments", json=pay, headers=headers)
    assert again.status_code == 409  # already fully paid


async def test_overdue_is_derived_from_balance_and_due_date(client, signup, db_session):


    headers = await signup()
    unpaid_future = await _issued(client, headers)
    assert unpaid_future["display_status"] == "issued"  # unpaid but not yet due: not overdue

    past = await _issued(client, headers, issue_date=str(TODAY - timedelta(days=30)), due_date=str(TODAY - timedelta(days=5)))
    assert past["display_status"] == "overdue" and past["days_overdue"] == 5

    paid_past = await _issued(client, headers, issue_date=str(TODAY - timedelta(days=30)), due_date=str(TODAY - timedelta(days=5)))
    await client.post(
        f"/invoices/{paid_past['id']}/payments",
        json={"amount_cents": paid_past["total_cents"], "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    detail = (await client.get(f"/invoices/{paid_past['id']}", headers=headers)).json()
    assert detail["display_status"] == "paid" and detail["days_overdue"] == 0

    overdue_list = (await client.get("/invoices?status=overdue", headers=headers)).json()
    assert [i["id"] for i in overdue_list["items"]] == [past["id"]]
    assert overdue_list["total"] == 1


async def test_cancel_rules(client, signup):
    headers = await signup()
    inv = await _issued(client, headers)
    ok = await client.post(f"/invoices/{inv['id']}/cancel", json={"reason": "wrong amount"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["display_status"] == "cancelled"
    pay = {"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"}
    assert (await client.post(f"/invoices/{inv['id']}/payments", json=pay, headers=headers)).status_code == 409

    paid = await _issued(client, headers)
    await client.post(f"/invoices/{paid['id']}/payments", json=pay, headers=headers)
    assert (await client.post(f"/invoices/{paid['id']}/cancel", headers=headers)).status_code == 409


# ------------------------------------------------------------------ isolation + public links


async def test_other_business_cannot_see_or_touch_invoice(client, signup):
    headers_a = await signup("a@example.com", "Biz A")
    headers_b = await signup("b@example.com", "Biz B")
    inv = await _issued(client, headers_a)
    iid = inv["id"]
    pay = {"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"}

    assert (await client.get(f"/invoices/{iid}", headers=headers_b)).status_code == 404
    assert (await client.patch(f"/invoices/{iid}", json={"notes": "x"}, headers=headers_b)).status_code == 404
    assert (await client.post(f"/invoices/{iid}/payments", json=pay, headers=headers_b)).status_code == 404
    assert (await client.get(f"/invoices/{iid}/pdf", headers=headers_b)).status_code == 404
    assert (await client.post(f"/invoices/{iid}/send", json={"channels": ["email"]}, headers=headers_b)).status_code == 404
    assert (await client.get("/invoices", headers=headers_b)).json()["total"] == 0

    # customers are tenant-scoped too
    customer_id = inv["customer_id"]
    foreign = _body(customer_id=customer_id)
    foreign.pop("customer_name")
    assert (await client.post("/invoices", json=foreign, headers=headers_b)).status_code == 404


async def test_requires_authentication(client):
    assert (await client.get("/invoices")).status_code == 401
    assert (await client.post("/invoices", json={})).status_code == 401


async def test_public_invoice_link_and_pdf(client, signup):
    headers = await signup()
    inv = await _issued(client, headers, notes="Pay promptly", payment_instructions="Send to account 123")
    token = inv["public_url"].rsplit("/", 1)[-1]
    assert len(token) >= 40  # 256 bits, not a database id

    page = await client.get(f"/i/{token}")
    assert page.status_code == 200
    assert "INVOICE" in page.text and "Grace Tenant" in page.text and "Send to account 123" in page.text
    assert "no-store" in page.headers["cache-control"]
    assert inv["id"] not in page.text

    pdf = await client.get(f"/i/{token}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    assert (await client.get("/i/not-a-real-token")).status_code == 404

    # public access never grants management ability
    assert (await client.post(f"/i/{token}/payments", json={})).status_code in (404, 405)


async def test_revoked_and_rotated_links(client, signup):
    headers = await signup()
    inv = await _issued(client, headers)
    old_token = inv["public_url"].rsplit("/", 1)[-1]

    rotated = (await client.post(f"/invoices/{inv['id']}/rotate-link", headers=headers)).json()
    new_token = rotated["public_url"].rsplit("/", 1)[-1]
    assert new_token != old_token
    assert (await client.get(f"/i/{old_token}")).status_code == 404
    assert (await client.get(f"/i/{new_token}")).status_code == 200

    revoked = (await client.post(f"/invoices/{inv['id']}/revoke-link", headers=headers)).json()
    assert revoked["public_url"] is None
    assert (await client.get(f"/i/{new_token}")).status_code == 404


async def test_draft_has_no_public_page_and_authenticated_pdf_works(client, signup):
    headers = await signup()
    draft = (await client.post("/invoices", json=_body(), headers=headers)).json()
    assert draft["public_url"] is None
    pdf = await client.get(f"/invoices/{draft['id']}/pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_issued_invoice_keeps_historical_contents(client, signup):

    headers = await signup()
    inv = await _issued(client, headers)
    token = inv["public_url"].rsplit("/", 1)[-1]
    await client.patch("/tenant/me", json={"business_name": "Renamed Co"}, headers=headers)
    page = await client.get(f"/i/{token}")
    assert "Acme Rentals" in page.text and "Renamed Co" not in page.text


# ------------------------------------------------------------------ sending


async def test_send_invoice_by_email_and_sms_is_logged(client, signup, outbox):
    headers = await signup()
    inv = await _issued(client, headers)
    resp = await client.post(f"/invoices/{inv['id']}/send", json={"channels": ["email", "sms"]}, headers=headers)
    assert resp.status_code == 200
    events = resp.json()
    assert {e["channel"]: e["status"] for e in events} == {"email": "sent", "sms": "sent"}
    assert all(e["attempt_log"] for e in events)

    email = next(kw for kind, kw in outbox if kind == "email")
    assert inv["number"] in email["subject"]
    assert email["pdf_bytes"].startswith(b"%PDF")
    sms = next(kw for kind, kw in outbox if kind == "sms")
    assert inv["public_url"] in sms["body"]

    history = (await client.get(f"/invoices/{inv['id']}/notifications", headers=headers)).json()
    assert len([e for e in history if e["kind"] == "invoice_issued"]) == 2


async def test_send_skips_suppressed_recipient(client, signup, outbox, db_session):
    from app.models.enums import DeliveryChannel
    from app.models.suppression import Suppression

    headers = await signup()
    inv = await _issued(client, headers)
    db_session.add(Suppression(tenant_id=None, channel=DeliveryChannel.email, contact="grace@example.com", reason="hard_bounce"))
    await db_session.commit()

    resp = await client.post(f"/invoices/{inv['id']}/send", json={"channels": ["email"]}, headers=headers)
    event = resp.json()[0]
    assert event["status"] == "skipped" and "suppression" in event["skip_reason"]
    assert outbox == []


async def test_failed_send_is_recorded_and_retryable(client, signup, monkeypatch):
    import app.services.notifications as n

    headers = await signup()
    inv = await _issued(client, headers)
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        raise RuntimeError("gmail down")

    monkeypatch.setattr(n, "send_document_email", flaky)
    resp = await client.post(f"/invoices/{inv['id']}/send", json={"channels": ["email"]}, headers=headers)
    event = resp.json()[0]
    assert event["status"] == "failed" and calls["n"] == n.EMAIL_RETRY_ATTEMPTS

    monkeypatch.setattr(n, "send_document_email", lambda **kw: {"MessageID": "ok-1"})
    retried = await client.post(f"/notifications/{event['id']}/retry", headers=headers)
    assert retried.status_code == 200 and retried.json()["status"] == "sent"
    assert retried.json()["attempts"] == 2

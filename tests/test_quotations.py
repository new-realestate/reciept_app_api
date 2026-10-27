from datetime import date, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

TODAY = date.today()


def _body(**overrides):
    body = {
        "customer_name": "Prospect Ltd",
        "customer_email": "prospect@example.com",
        "customer_phone": "+256701234567",
        "title": "Roof repair estimate",
        "currency": "UGX",
        "valid_until": str(TODAY + timedelta(days=30)),
        "line_items": [
            {"description": "Labour", "quantity": "10", "unit_price_cents": 100000, "tax_rate": "18"},
            {"description": "Materials", "quantity": "1", "unit_price_cents": 500000},
        ],
        "terms": "50% deposit",
        "notes": "Thanks for asking",
        "internal_notes": "SECRET margin is 40%",
    }
    body.update(overrides)
    return body


async def _sent(client, headers, **overrides):
    q = (await client.post("/quotations", json=_body(**overrides), headers=headers)).json()
    resp = await client.post(f"/quotations/{q['id']}/send", json={"channels": ["email"]}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token(q):
    return q["public_url"].rsplit("/", 1)[-1]


async def test_create_edit_draft_and_totals(client, signup):
    headers = await signup()
    resp = await client.post("/quotations", json=_body(), headers=headers)
    assert resp.status_code == 201
    q = resp.json()
    assert q["status"] == "draft" and q["number"] is None and q["public_url"] is None
    assert q["subtotal_cents"] == 1500000 and q["tax_cents"] == 180000 and q["total_cents"] == 1680000

    edit = await client.patch(
        f"/quotations/{q['id']}",
        json={"line_items": [{"description": "Only item", "quantity": "2", "unit_price_cents": 1000}], "discount_cents": 500},
        headers=headers,
    )
    assert edit.status_code == 200 and edit.json()["total_cents"] == 1500


async def test_send_assigns_sequential_number_and_locks_editing(client, signup, outbox):
    headers = await signup()
    q1 = await _sent(client, headers)
    q2 = await _sent(client, headers)
    assert q1["number"].startswith("QUO-") and q1["number"].endswith("000001") and q2["number"].endswith("000002")
    assert q1["status"] == "sent" and q1["sent_at"]
    assert (await client.patch(f"/quotations/{q1['id']}", json={"notes": "x"}, headers=headers)).status_code == 409
    email = next(kw for kind, kw in outbox if kind == "email")
    assert "Quotation" in email["subject"] and email["pdf_bytes"].startswith(b"%PDF")


async def test_public_page_hides_internal_notes_and_is_labelled_quotation(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    page = await client.get(f"/q/{_token(q)}")
    assert page.status_code == 200
    assert "QUOTATION" in page.text and "not an invoice" in page.text.lower()
    assert "SECRET" not in page.text and q["id"] not in page.text
    assert "50% deposit" in page.text
    pdf = await client.get(f"/q/{_token(q)}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # authenticated PDF works for drafts too and has the right type
    draft = (await client.post("/quotations", json=_body(), headers=headers)).json()
    assert (await client.get(f"/quotations/{draft['id']}/pdf", headers=headers)).content.startswith(b"%PDF")


async def test_viewing_marks_viewed_once(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    await client.get(f"/q/{_token(q)}")
    first = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert first["status"] == "viewed" and first["viewed_at"]
    await client.get(f"/q/{_token(q)}")
    again = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert again["viewed_at"] == first["viewed_at"]


async def test_accept_records_name_and_is_idempotent(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    tok = _token(q)

    missing = await client.post(f"/q/{tok}/accept", data={"name": "A", "confirm": "yes"})
    assert missing.status_code == 422  # name too short
    unconfirmed = await client.post(f"/q/{tok}/accept", data={"name": "Pat Prospect", "confirm": "no"})
    assert unconfirmed.status_code == 422

    ok = await client.post(f"/q/{tok}/accept", data={"name": "Pat Prospect", "confirm": "yes"}, follow_redirects=False)
    assert ok.status_code == 303
    detail = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert detail["status"] == "accepted" and detail["accepted_name"] == "Pat Prospect" and detail["responded_at"]

    again = await client.post(f"/q/{tok}/accept", data={"name": "Someone Else", "confirm": "yes"}, follow_redirects=False)
    assert again.status_code == 303
    assert (await client.get(f"/quotations/{q['id']}", headers=headers)).json()["accepted_name"] == "Pat Prospect"

    flip = await client.post(f"/q/{tok}/reject", data={"comment": "changed my mind"}, follow_redirects=False)
    assert flip.headers["location"].endswith("r=unavailable")
    assert (await client.get(f"/quotations/{q['id']}", headers=headers)).json()["status"] == "accepted"

    page = await client.get(f"/q/{tok}?r=accepted")
    assert "acceptance has been recorded" in page.text
    assert "Accept quotation" not in page.text  # no response form once answered


async def test_reject_with_comment(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    resp = await client.post(f"/q/{_token(q)}/reject", data={"comment": "Too expensive"}, follow_redirects=False)
    assert resp.status_code == 303
    detail = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert detail["status"] == "rejected" and detail["response_comment"] == "Too expensive"
    assert (await client.post(f"/quotations/{q['id']}/convert", json={"due_date": str(TODAY)}, headers=headers)).status_code == 409


async def test_expired_quotation_cannot_be_answered_and_status_is_derived(client, signup, db_session):
    headers = await signup()
    q = await _sent(client, headers)
    await db_session.execute(text("UPDATE quotations SET valid_until = :d"), {"d": str(TODAY - timedelta(days=1))})
    await db_session.commit()

    detail = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert detail["status"] == "sent" and detail["display_status"] == "expired"
    resp = await client.post(f"/q/{_token(q)}/accept", data={"name": "Pat Prospect", "confirm": "yes"}, follow_redirects=False)
    assert resp.headers["location"].endswith("r=unavailable")
    page = await client.get(f"/q/{_token(q)}")
    assert "Accept quotation" not in page.text and "EXPIRED" in page.text
    assert (await client.get("/quotations?status=expired", headers=headers)).json()["total"] == 1


async def test_scheduler_materialises_expiry(client, signup, db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import AsyncSessionLocal  # noqa: F401 - ensure module import works
    from app.services import scheduler

    headers = await signup()
    q = await _sent(client, headers)
    await db_session.execute(text("UPDATE quotations SET valid_until = :d"), {"d": str(TODAY - timedelta(days=2))})
    await db_session.commit()

    stats = await scheduler.run_cycle(async_sessionmaker(db_session.bind, expire_on_commit=False))
    assert stats["quotations_expired"] == 1
    assert (await client.get(f"/quotations/{q['id']}", headers=headers)).json()["status"] == "expired"


async def test_convert_accepted_quotation_to_invoice(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    tok = _token(q)

    not_yet = await client.post(f"/quotations/{q['id']}/convert", json={"due_date": str(TODAY + timedelta(days=10))}, headers=headers)
    assert not_yet.status_code == 409

    await client.post(f"/q/{tok}/accept", data={"name": "Pat Prospect", "confirm": "yes"})
    due = str(TODAY + timedelta(days=10))
    conv = await client.post(
        f"/quotations/{q['id']}/convert", json={"due_date": due, "payment_instructions": "Pay to 123"}, headers=headers
    )
    assert conv.status_code == 201, conv.text
    inv = conv.json()
    assert inv["status"] == "draft" and inv["quotation_id"] == q["id"]
    assert inv["total_cents"] == q["total_cents"] and inv["currency"] == "UGX"
    assert [li["description"] for li in inv["line_items"]] == ["Labour", "Materials"]
    assert inv["due_date"] == due and inv["payment_instructions"] == "Pay to 123"
    assert "SECRET" not in str(inv)

    # no payment happened, so no receipt exists
    assert (await client.get("/receipts", headers=headers)).json() == []
    # duplicate conversion is refused and the original quotation is untouched
    dup = await client.post(f"/quotations/{q['id']}/convert", json={"payment_received_confirmed": True, "due_date": due}, headers=headers)
    assert dup.status_code == 409
    after = (await client.get(f"/quotations/{q['id']}", headers=headers)).json()
    assert after["status"] == "accepted" and after["converted_invoice_id"] == inv["id"]
    assert (await client.get("/invoices", headers=headers)).json()["total"] == 1

    # the resulting invoice follows the normal payment flow
    issued = (await client.post(f"/invoices/{inv['id']}/issue", headers=headers)).json()
    pay = await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    assert pay.status_code == 201 and issued["number"].startswith("INV-")
    assert len((await client.get("/receipts", headers=headers)).json()) == 1


async def test_convert_with_issue_now_and_accepted_snapshot_preserved(client, signup, db_session):
    headers = await signup()
    q = await _sent(client, headers)
    await client.post(f"/q/{_token(q)}/accept", data={"name": "Pat Prospect", "confirm": "yes"})
    conv = await client.post(
        f"/quotations/{q['id']}/convert", json={"due_date": str(TODAY + timedelta(days=5)), "issue_now": True}, headers=headers
    )
    assert conv.status_code == 201 and conv.json()["status"] == "issued"

    from sqlalchemy import select

    from app.models.quotation import Quotation

    row = (await db_session.execute(select(Quotation))).scalar_one()
    assert row.snapshot["response"]["result"] == "accepted"
    assert row.snapshot["response"]["accepted_name"] == "Pat Prospect"
    assert "internal_notes" not in row.snapshot and row.snapshot["total_cents"] == 1680000


async def test_cancel_rules(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    cancelled = await client.post(f"/quotations/{q['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await client.get(f"/q/{_token(q)}")).status_code == 200  # still viewable, but...
    resp = await client.post(f"/q/{_token(q)}/accept", data={"name": "Pat Prospect", "confirm": "yes"}, follow_redirects=False)
    assert resp.headers["location"].endswith("r=unavailable")

    other = await _sent(client, headers)
    await client.post(f"/q/{_token(other)}/accept", data={"name": "Pat Prospect", "confirm": "yes"})
    assert (await client.post(f"/quotations/{other['id']}/cancel", headers=headers)).status_code == 409


async def test_cross_business_access_denied(client, signup):
    headers_a = await signup("a@example.com", "Biz A")
    headers_b = await signup("b@example.com", "Biz B")
    q = await _sent(client, headers_a)
    qid = q["id"]
    assert (await client.get(f"/quotations/{qid}", headers=headers_b)).status_code == 404
    assert (await client.patch(f"/quotations/{qid}", json={"notes": "x"}, headers=headers_b)).status_code == 404
    assert (await client.post(f"/quotations/{qid}/send", json={"channels": ["email"]}, headers=headers_b)).status_code == 404
    assert (await client.post(f"/quotations/{qid}/convert", json={"due_date": str(TODAY)}, headers=headers_b)).status_code == 404
    assert (await client.get(f"/quotations/{qid}/pdf", headers=headers_b)).status_code == 404
    assert (await client.get("/quotations", headers=headers_b)).json()["total"] == 0


async def test_unknown_and_draft_tokens_404_and_public_rate_limit(client, signup):
    await signup()
    assert (await client.get("/q/nope")).status_code == 404
    assert (await client.post("/q/nope/accept", data={"name": "Pat Prospect", "confirm": "yes"})).status_code == 404

    from app.core.rate_limit import public_doc_rate_limiter

    public_doc_rate_limiter._hits.clear()
    statuses = [(await client.get(f"/q/guess-{i}")).status_code for i in range(public_doc_rate_limiter.max_requests + 5)]
    assert statuses[0] == 404 and statuses[-1] == 429  # guessing many tokens trips the per-IP limit


async def test_accept_response_is_rate_limited(client, signup):
    headers = await signup()
    q = await _sent(client, headers)
    codes = []
    for _ in range(15):
        r = await client.post(f"/q/{_token(q)}/accept", data={"name": "Pat Prospect", "confirm": "yes"}, follow_redirects=False)
        codes.append(r.status_code)
    assert 429 in codes

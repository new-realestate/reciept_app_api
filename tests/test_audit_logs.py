from datetime import date, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

TODAY = date.today()


async def _make_activity(client, headers, make_invoice):
    inv = await make_invoice(headers)
    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    return inv


async def test_lists_newest_first_with_actor_and_metadata(client, signup, make_invoice, outbox):
    headers = await signup()
    inv = await _make_activity(client, headers, make_invoice)

    resp = await client.get("/audit-logs", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    actions = [i["action"] for i in body["items"]]
    assert {"user.signup", "invoice.created", "invoice.issued", "invoice.payment_recorded", "receipt.created"} <= set(actions)
    assert body["total"] == len(body["items"])
    stamps = [i["created_at"] for i in body["items"]]
    assert stamps == sorted(stamps, reverse=True)

    pay = next(i for i in body["items"] if i["action"] == "invoice.payment_recorded")
    assert pay["entity_id"] == inv["id"] and pay["metadata"]["amount_cents"] == 1000
    assert pay["actor_email"] == "owner@example.com" and pay["actor_name"] == "Jane Landlord"


async def test_filters_and_pagination(client, signup, make_invoice, outbox):
    headers = await signup()
    inv = await _make_activity(client, headers, make_invoice)

    only = (await client.get("/audit-logs?action=invoice.issued", headers=headers)).json()
    assert [i["action"] for i in only["items"]] == ["invoice.issued"] and only["total"] == 1

    family = (await client.get("/audit-logs?action_prefix=invoice", headers=headers)).json()
    assert family["total"] >= 3 and all(i["action"].startswith("invoice.") for i in family["items"])

    by_entity = (await client.get(f"/audit-logs?entity_id={inv['id']}", headers=headers)).json()
    assert by_entity["total"] >= 3 and all(i["entity_id"] == inv["id"] for i in by_entity["items"])

    by_type = (await client.get("/audit-logs?entity_type=receipt", headers=headers)).json()
    assert by_type["total"] == 1

    today = (await client.get(f"/audit-logs?date_from={TODAY}&date_to={TODAY}", headers=headers)).json()
    assert today["total"] > 0
    past = (await client.get(f"/audit-logs?date_to={TODAY - timedelta(days=2)}", headers=headers)).json()
    assert past["total"] == 0
    future = (await client.get(f"/audit-logs?date_from={TODAY + timedelta(days=2)}", headers=headers)).json()
    assert future["total"] == 0

    page = (await client.get("/audit-logs?limit=2&offset=1", headers=headers)).json()
    assert len(page["items"]) == 2 and page["total"] > 2 and page["limit"] == 2 and page["offset"] == 1
    assert (await client.get("/audit-logs?limit=0", headers=headers)).status_code == 422


async def test_distinct_actions(client, signup, make_invoice, outbox):
    headers = await signup()
    await _make_activity(client, headers, make_invoice)
    actions = (await client.get("/audit-logs/actions", headers=headers)).json()
    assert actions == sorted(set(actions)) and "invoice.issued" in actions


async def test_only_owner_or_admin_and_only_own_business(client, signup, make_invoice, db_session, outbox):
    headers_a = await signup("a@example.com", "Biz A")
    headers_b = await signup("b@example.com", "Biz B")
    await _make_activity(client, headers_a, make_invoice)

    # Business B sees none of A's trail
    other = (await client.get("/audit-logs?action_prefix=invoice", headers=headers_b)).json()
    assert other["total"] == 0
    assert (await client.get("/audit-logs/actions", headers=headers_b)).json() != (await client.get("/audit-logs/actions", headers=headers_a)).json()

    assert (await client.get("/audit-logs")).status_code == 401

    await db_session.execute(text("UPDATE users SET role = 'staff' WHERE email = 'a@example.com'"))
    await db_session.commit()
    assert (await client.get("/audit-logs", headers=headers_a)).status_code == 403
    assert (await client.get("/audit-logs/actions", headers=headers_a)).status_code == 403

    await db_session.execute(text("UPDATE users SET role = 'admin' WHERE email = 'a@example.com'"))
    await db_session.commit()
    assert (await client.get("/audit-logs", headers=headers_a)).status_code == 200


async def test_customer_actions_have_no_actor(client, signup, outbox):
    from datetime import timedelta as td

    headers = await signup()
    q = (await client.post("/quotations", json={
        "customer_name": "P", "customer_email": "p@example.com", "valid_until": str(TODAY + td(days=9)),
        "line_items": [{"description": "x", "quantity": "1", "unit_price_cents": 5000}]}, headers=headers)).json()
    sent = (await client.post(f"/quotations/{q['id']}/send", json={"channels": ["email"]}, headers=headers)).json()
    token = sent["public_url"].rsplit("/", 1)[-1]
    await client.post(f"/q/{token}/accept", data={"name": "Pat Prospect", "confirm": "yes"})

    accepted = (await client.get("/audit-logs?action=quotation.accepted", headers=headers)).json()["items"][0]
    assert accepted["actor_user_id"] is None and accepted["actor_name"] is None

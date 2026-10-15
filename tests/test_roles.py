from datetime import date, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

TODAY = date.today()


async def test_staff_can_work_but_not_perform_manager_actions(client, signup, make_invoice, db_session):
    headers = await signup()
    inv = await make_invoice(headers)
    q = (await client.post("/quotations", json={
        "customer_name": "P", "customer_email": "p@example.com", "valid_until": str(TODAY + timedelta(days=9)),
        "line_items": [{"description": "x", "quantity": "1", "unit_price_cents": 7000}]}, headers=headers)).json()

    await db_session.execute(text("UPDATE users SET role = 'staff'"))
    await db_session.commit()

    # day-to-day work is allowed
    assert (await make_invoice(headers, issue=False, customer_email="new@example.com")) is not None
    pay = await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 1000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    assert pay.status_code == 201

    # destructive / business-wide actions are not
    assert (await client.post(f"/invoices/{inv['id']}/cancel", headers=headers)).status_code == 403
    assert (await client.post(f"/invoices/{inv['id']}/rotate-link", headers=headers)).status_code == 403
    assert (await client.post(f"/invoices/{inv['id']}/revoke-link", headers=headers)).status_code == 403
    assert (await client.post(f"/quotations/{q['id']}/cancel", headers=headers)).status_code == 403

    await db_session.execute(text("UPDATE users SET role = 'admin'"))
    await db_session.commit()
    assert (await client.post(f"/invoices/{inv['id']}/rotate-link", headers=headers)).status_code == 200


async def test_existing_receipt_endpoints_unchanged_for_staff(client, signup, db_session):
    headers = await signup()
    await db_session.execute(text("UPDATE users SET role = 'staff'"))
    await db_session.commit()
    cust = (await client.post("/customers", json={"name": "J", "email": "j@example.com"}, headers=headers)).json()
    resp = await client.post(
        "/receipts",
        json={"customer_id": cust["id"], "amount_cents": 1000, "reason": "Rent", "payment_method": "cash", "paid_at": str(TODAY)},
        headers=headers,
    )
    assert resp.status_code == 201

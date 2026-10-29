import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

TODAY = date.today()


async def test_non_tenant_customer_receives_all_three_document_types(client, signup, outbox):
    owner = await signup()
    resp = await client.post("/customers", json={
        "name": "Acme Ltd", "email": "ap@acme.example", "customer_type": "business", "tax_identification_number": "100200300",
        "billing_address": "Plot 5, Kampala",
    }, headers=owner)
    assert resp.status_code == 201
    cust = resp.json()
    assert cust["customer_type"] == "business" and cust["renter_id"] is None and cust["status"] == "active"

    receipt = await client.post("/receipts", json={
        "payment_received_confirmed": True, "customer_id": cust["id"], "amount_cents": 10000, "reason": "Consulting",
        "payment_method": "bank_transfer", "paid_at": str(TODAY),
    }, headers=owner)
    assert receipt.status_code == 201 and receipt.json()["property_id"] is None

    invoice = await client.post("/invoices", json={
        "customer_id": cust["id"], "due_date": str(TODAY + timedelta(days=7)), "issue_now": True,
        "line_items": [{"description": "Design work", "quantity": "2", "unit_price_cents": 5000}],
    }, headers=owner)
    assert invoice.status_code == 201

    quotation = await client.post("/quotations", json={
        "customer_id": cust["id"], "valid_until": str(TODAY + timedelta(days=14)),
        "line_items": [{"description": "Maintenance", "quantity": "1", "unit_price_cents": 7000}],
    }, headers=owner)
    assert quotation.status_code == 201

    docs = (await client.get(f"/customers/{cust['id']}/documents", headers=owner)).json()
    assert len(docs["receipts"]) == 1 and len(docs["invoices"]) == 1 and len(docs["quotations"]) == 1
    assert docs["outstanding_by_currency"] == {"UGX": 10000}

    # Partial payment through the invoice leaves an outstanding balance and creates exactly one receipt.
    pay = await client.post(f"/invoices/{invoice.json()['id']}/payments",
                            json={"amount_cents": 4000, "paid_at": str(TODAY), "payment_method": "cash"},
                            headers={**owner, "Idempotency-Key": "pay-1"})
    assert pay.status_code == 201
    again = await client.post(f"/invoices/{invoice.json()['id']}/payments",
                              json={"amount_cents": 4000, "paid_at": str(TODAY), "payment_method": "cash"},
                              headers={**owner, "Idempotency-Key": "pay-1"})
    assert again.status_code == 200 and again.json()["replayed"] is True
    docs = (await client.get(f"/customers/{cust['id']}/documents", headers=owner)).json()
    assert docs["outstanding_by_currency"] == {"UGX": 6000}
    assert len(docs["receipts"]) == 2


async def test_duplicate_customers_are_detected_within_the_business_only(client, signup):
    owner = await signup()
    first = (await client.post("/customers", json={"name": "Grace", "phone": "+256701234567"}, headers=owner)).json()

    # Same number written locally is still the same customer.
    dup = await client.post("/customers", json={"name": "Grace N", "phone": "0701 234 567"}, headers=owner)
    assert dup.status_code == 409
    assert dup.json()["error"]["message"]["matches"][0]["id"] == first["id"]

    matches = (await client.get("/customers/matches", params={"phone": "0701234567", "name": "grace"}, headers=owner)).json()
    assert [m["id"] for m in matches["exact"]] == [first["id"]] and matches["possible"] == []
    name_only = (await client.get("/customers/matches", params={"name": "GRACE", "email": "x@example.com"}, headers=owner)).json()
    assert name_only["exact"] == [] and [m["id"] for m in name_only["possible"]] == [first["id"]]

    # The user can still explicitly create a separate record.
    forced = await client.post("/customers", json={"name": "Grace N", "phone": "0701234567", "allow_duplicate": True}, headers=owner)
    assert forced.status_code == 201

    # Another business never sees these customers.
    other = await signup("b@example.com", "Other")
    assert (await client.post("/customers", json={"name": "Grace", "phone": "+256701234567"}, headers=other)).status_code == 201
    assert (await client.get(f"/customers/{first['id']}", headers=other)).status_code == 404
    assert (await client.get("/customers/matches", params={"phone": "+256701234567"}, headers=other)).json()["exact"][0]["id"] != first["id"]


async def test_contact_changes_do_not_rewrite_issued_documents(client, signup, make_invoice, db_session):
    from app.models.invoice import Invoice

    owner = await signup()
    inv = await make_invoice(owner)
    cust_id = inv["customer_id"]
    resp = await client.patch(f"/customers/{cust_id}", json={"name": "Grace Renamed", "email": "new@example.com"}, headers=owner)
    assert resp.status_code == 200 and resp.json()["name"] == "Grace Renamed"

    snapshot = (await db_session.execute(select(Invoice.snapshot).where(Invoice.id == uuid.UUID(inv["id"])))).scalar_one()
    assert snapshot["customer"]["name"] == "Grace Tenant"
    assert snapshot["customer"]["email"] == "grace@example.com"


async def test_archived_customers_cannot_receive_new_documents(client, signup):
    owner = await signup()
    cust = (await client.post("/customers", json={"name": "Old", "email": "old@example.com"}, headers=owner)).json()
    assert (await client.post(f"/customers/{cust['id']}/archive", headers=owner)).json()["status"] == "archived"
    resp = await client.post("/receipts", json={
        "payment_received_confirmed": True, "customer_id": cust["id"], "amount_cents": 100, "reason": "x",
        "payment_method": "cash", "paid_at": str(TODAY),
    }, headers=owner)
    assert resp.status_code == 422
    assert (await client.post(f"/customers/{cust['id']}/restore", headers=owner)).json()["status"] == "active"


async def test_quotation_converts_to_invoice_preserving_link(client, signup):
    owner = await signup()
    q = (await client.post("/quotations", json={
        "customer_name": "Walk-in", "customer_email": "walkin@example.com", "valid_until": str(TODAY + timedelta(days=5)),
        "line_items": [{"description": "Repair", "quantity": "1", "unit_price_cents": 9000}],
    }, headers=owner)).json()
    # Not accepted yet: conversion refused.
    early = await client.post(f"/quotations/{q['id']}/convert", json={"due_date": str(TODAY + timedelta(days=10))}, headers=owner)
    assert early.status_code == 409

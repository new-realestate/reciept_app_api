import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _signup_and_login(client, monkeypatch, email="owner@example.com"):
    monkeypatch.setattr("app.api.auth.send_verification_email", lambda **kwargs: {"MessageID": "fake"})

    await client.post(
        "/auth/signup",
        json={
            "business_name": "Acme Rentals",
            "full_name": "Jane Landlord",
            "email": email,
            "password": "supersecret",
        },
    )
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret"})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create_customer(client, headers, email="tenant@example.com"):
    response = await client.post(
        "/customers", json={"name": "John Payer", "email": email, "phone": "+256701234567"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_create_receipt_generates_words_and_reference_number(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    response = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 150000000,
            "currency": "UGX",
            "reason": "Rent for July 2026",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["reference_number"].startswith("RCT-")
    assert "One Million Five Hundred Thousand Shillings" in body["amount_in_words"]
    assert body["balance_cents"] == 0
    assert body["pdf_url"] is not None
    assert body["hosted_view_token"]


async def test_receipt_requires_reason_and_payment_method(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    response = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 10000,
            "reason": "",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    assert response.status_code == 422


async def test_sequential_reference_numbers_per_tenant(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    refs = []
    for _ in range(3):
        response = await client.post(
            "/receipts",
            json={"payment_received_confirmed": True, 
                "customer_id": customer_id,
                "amount_cents": 5000,
                "reason": "Rent",
                "payment_method": "cash",
                "paid_at": "2026-07-01",
            },
            headers=headers,
        )
        refs.append(response.json()["reference_number"])

    assert len(set(refs)) == 3


async def test_list_and_get_receipt(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    created = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 20000,
            "reason": "Security deposit",
            "payment_method": "bank_transfer",
            "payment_reference": "SLIP-001",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    receipt_id = created.json()["id"]

    listing = await client.get("/receipts", headers=headers)
    assert listing.status_code == 200
    assert any(r["id"] == receipt_id for r in listing.json())

    detail = await client.get(f"/receipts/{receipt_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["delivery_attempts"] == []


async def test_hosted_view_shows_nil_balance(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    created = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 20000,
            "reason": "Rent",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    token = created.json()["hosted_view_token"]

    view = await client.get(f"/r/{token}")
    assert view.status_code == 200
    assert "Nil" in view.text


async def test_correction_references_original_receipt(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    original = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 20000,
            "reason": "Rent",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    original_id = original.json()["id"]

    correction = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 25000,
            "reason": "Rent (corrected amount)",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
            "corrects_receipt_id": original_id,
        },
        headers=headers,
    )
    assert correction.status_code == 201
    assert correction.json()["corrects_receipt_id"] == original_id


async def test_receipt_template_selection_changes_layout_and_rejects_unknown(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)

    assert (await client.get("/tenant/me", headers=headers)).json()["receipt_template"] == "classic"

    bad = await client.patch("/tenant/me", json={"receipt_template": "fancy"}, headers=headers)
    assert bad.status_code == 422

    ok = await client.patch("/tenant/me", json={"receipt_template": "modern"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["receipt_template"] == "modern"

    created = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 20000,
            "reason": "Rent",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    view = await client.get(f"/r/{created.json()['hosted_view_token']}")
    assert "Amount received" in view.text

    await client.patch("/tenant/me", json={"receipt_template": "classic"}, headers=headers)
    view = await client.get(f"/r/{created.json()['hosted_view_token']}")
    assert "Amount received" not in view.text and "Payment Receipt" in view.text


async def test_receipt_detail_includes_sharing_details(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    created = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, "customer_id": customer_id, "amount_cents": 150000, "currency": "UGX", "reason": "Rent",
              "payment_method": "cash", "paid_at": "2026-07-01"},
        headers=headers,
    )
    detail = (await client.get(f"/receipts/{created.json()['id']}", headers=headers)).json()
    assert detail["customer_name"] == "John Payer"
    assert detail["customer_phone"] == "+256701234567"
    assert detail["business_name"] == "Acme Rentals"


def _body(customer_id, **extra):
    return {"payment_received_confirmed": True, "customer_id": customer_id, "amount_cents": 5000, "reason": "Rent",
            "payment_method": "cash", "paid_at": "2026-07-01", **extra}


async def test_receipt_requires_confirmation_that_payment_was_received(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    body = _body(customer_id)
    del body["payment_received_confirmed"]
    assert (await client.post("/receipts", json=body, headers=headers)).status_code == 422
    assert (await client.post("/receipts", json={**body, "payment_received_confirmed": False}, headers=headers)).status_code == 422


async def test_receipt_payment_date_cannot_be_in_the_future(client, monkeypatch):
    from datetime import date, timedelta

    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    future = str(date.today() + timedelta(days=3))
    assert (await client.post("/receipts", json=_body(customer_id, paid_at=future), headers=headers)).status_code == 422


async def test_idempotency_key_prevents_duplicate_receipts(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    first = await client.post("/receipts", json=_body(customer_id), headers={**headers, "Idempotency-Key": "abc-1"})
    again = await client.post("/receipts", json=_body(customer_id), headers={**headers, "Idempotency-Key": "abc-1"})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert len((await client.get("/receipts", headers=headers)).json()) == 1


async def test_numbering_continues_after_existing_receipts(client, monkeypatch, db_session):
    """Receipts issued before the counter existed are respected: the next number follows the highest one."""
    from sqlalchemy import delete, update

    from app.models.invoice import DocumentCounter
    from app.models.receipt import Receipt

    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    first = (await client.post("/receipts", json=_body(customer_id), headers=headers)).json()
    # Simulate data from before the counter: a legacy number and no counter row.
    await db_session.execute(update(Receipt).where(Receipt.id == uuid.UUID(first["id"])).values(reference_number="RCT-2025-000041"))
    await db_session.execute(delete(DocumentCounter).where(DocumentCounter.kind == "receipt"))
    await db_session.commit()

    nxt = (await client.post("/receipts", json=_body(customer_id), headers=headers)).json()
    assert nxt["reference_number"].endswith("-000042")


@pytest.mark.skipif(
    not __import__("os").environ.get("TEST_DATABASE_URL"),
    reason="Needs a migrated PostgreSQL database in TEST_DATABASE_URL (row locks don't exist in SQLite)",
)
async def test_concurrent_receipt_numbers_are_unique_on_postgres():
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.tenant import Tenant
    from app.services.reference_numbers import next_reference_number

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        tenant = Tenant(business_name="Concurrency check")
        s.add(tenant)
        await s.commit()
    try:
        async def allocate():
            async with sessions() as s, s.begin():
                return await next_reference_number(s, tenant.id)

        refs = await asyncio.gather(*(allocate() for _ in range(25)))
        assert len(set(refs)) == 25
        assert sorted(int(r.rsplit("-", 1)[1]) for r in refs) == list(range(1, 26))
    finally:
        async with sessions() as s:
            await s.delete(await s.get(Tenant, tenant.id))
            await s.commit()
        await engine.dispose()

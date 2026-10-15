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
        json={
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
        json={
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
            json={
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
        json={
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
        json={
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
        json={
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
        json={
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
        json={
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

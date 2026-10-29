import pytest

pytestmark = pytest.mark.asyncio


async def _signup_and_login(client, monkeypatch, email="owner2@example.com"):
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


async def _create_customer(client, headers, email="payer2@example.com", phone="+256701234599"):
    response = await client.post("/customers", json={"name": "John Payer", "email": email, "phone": phone}, headers=headers)
    return response.json()["id"]


async def _create_receipt(client, headers, customer_id):
    response = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, 
            "customer_id": customer_id,
            "amount_cents": 30000,
            "reason": "Rent",
            "payment_method": "cash",
            "paid_at": "2026-07-01",
        },
        headers=headers,
    )
    return response.json()


async def test_send_receipt_by_email_success(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.receipts.send_receipt_email", lambda **kwargs: {"MessageID": "postmark-msg-1"}
    )

    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    receipt = await _create_receipt(client, headers, customer_id)

    response = await client.post(
        f"/receipts/{receipt['id']}/send", json={"channels": ["email"]}, headers=headers
    )

    assert response.status_code == 200
    attempts = response.json()
    assert attempts[0]["status"] == "sent"
    assert attempts[0]["channel"] == "email"


async def test_send_receipt_by_sms_success(client, monkeypatch):
    monkeypatch.setattr("app.api.receipts.send_receipt_sms", lambda **kwargs: "SM123")

    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    receipt = await _create_receipt(client, headers, customer_id)

    response = await client.post(f"/receipts/{receipt['id']}/send", json={"channels": ["sms"]}, headers=headers)

    assert response.status_code == 200
    attempts = response.json()
    assert attempts[0]["status"] == "sent"
    assert attempts[0]["provider"] == "twilio"


async def test_send_retries_on_transient_email_failure(client, monkeypatch):
    calls = {"count": 0}

    def flaky_send(**kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            raise RuntimeError("transient provider error")
        return {"MessageID": "postmark-msg-2"}

    monkeypatch.setattr("app.api.receipts.send_receipt_email", flaky_send)

    headers = await _signup_and_login(client, monkeypatch)
    customer_id = await _create_customer(client, headers)
    receipt = await _create_receipt(client, headers, customer_id)

    response = await client.post(f"/receipts/{receipt['id']}/send", json={"channels": ["email"]}, headers=headers)

    assert response.status_code == 200
    assert response.json()[0]["status"] == "sent"
    assert calls["count"] == 2


async def test_resend_skips_suppressed_contact(client, monkeypatch):
    monkeypatch.setattr("app.api.receipts.send_receipt_email", lambda **kwargs: {"MessageID": "x"})

    headers = await _signup_and_login(client, monkeypatch, email="owner3@example.com")
    customer_id = await _create_customer(client, headers, email="suppressed@example.com", phone="+256701234500")
    receipt = await _create_receipt(client, headers, customer_id)

    # Manually insert a suppression via the DB used by this test client.
    from app.db.session import get_db
    from app.main import app
    from app.models.enums import DeliveryChannel
    from app.models.suppression import Suppression

    override = app.dependency_overrides[get_db]
    async for session in override():
        session.add(Suppression(tenant_id=None, channel=DeliveryChannel.email, contact="suppressed@example.com", reason="hard_bounce"))
        await session.commit()
        break

    response = await client.post(f"/receipts/{receipt['id']}/send", json={"channels": ["email"]}, headers=headers)

    assert response.status_code == 200
    attempt = response.json()[0]
    assert attempt["status"] == "failed"


async def test_send_without_contact_info_returns_400(client, monkeypatch):
    headers = await _signup_and_login(client, monkeypatch, email="owner4@example.com")
    response = await client.post(
        "/customers", json={"name": "No Phone Payer", "email": "onlyemail@example.com"}, headers=headers
    )
    customer_id = response.json()["id"]
    receipt = await _create_receipt(client, headers, customer_id)

    response = await client.post(f"/receipts/{receipt['id']}/send", json={"channels": ["sms"]}, headers=headers)
    assert response.status_code == 400


async def test_postmark_webhook_updates_delivery_status_and_suppresses_hard_bounce(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.receipts.send_receipt_email", lambda **kwargs: {"MessageID": "postmark-msg-3"}
    )

    headers = await _signup_and_login(client, monkeypatch, email="owner5@example.com")
    customer_id = await _create_customer(client, headers, email="bounced@example.com", phone="+256701234511")
    receipt = await _create_receipt(client, headers, customer_id)

    await client.post(f"/receipts/{receipt['id']}/send", json={"channels": ["email"]}, headers=headers)

    webhook_response = await client.post(
        "/webhooks/postmark",
        json={"RecordType": "Bounce", "MessageID": "postmark-msg-3", "Type": "HardBounce"},
    )
    assert webhook_response.status_code == 200

    detail = await client.get(f"/receipts/{receipt['id']}", headers=headers)
    attempts = detail.json()["delivery_attempts"]
    assert attempts[0]["status"] == "bounced"

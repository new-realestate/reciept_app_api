import uuid

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _tenant_and_user(client, superadmin, email="owner@example.com"):
    users = (await client.get("/admin/users", params={"q": email}, headers=superadmin)).json()["items"]
    user = next(u for u in users if u["email"] == email)
    return user["tenant_id"], user["id"]


async def _add_user(db_session, tenant_id, email, role):
    db_session.add(User(tenant_id=uuid.UUID(tenant_id), full_name=email.split("@")[0], email=email,
                        password_hash="x", role=role))
    await db_session.commit()


async def _issue_receipt(client, headers):
    customer = await client.post("/customers", json={"name": "John Payer", "email": "payer@example.com"}, headers=headers)
    assert customer.status_code == 201, customer.text
    receipt = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, "customer_id": customer.json()["id"], "amount_cents": 150000, "currency": "UGX", "reason": "Rent",
              "payment_method": "cash", "paid_at": "2026-07-01"},
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text


async def test_admin_endpoints_require_superadmin(client, signup, superadmin):
    owner = await signup()
    assert (await client.get("/admin/stats", headers=owner)).status_code == 403
    assert (await client.get("/admin/stats")).status_code == 401
    assert (await client.get("/admin/stats", headers=superadmin)).status_code == 200


async def test_superadmin_cannot_use_business_endpoints_without_acting_as_one(client, superadmin):
    response = await client.get("/customers", headers=superadmin)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Choose a business to act as"


async def test_stats_and_lists(client, signup, superadmin):
    owner = await signup()
    await signup(email="second@example.com", business="Second Co")
    await _issue_receipt(client, owner)

    stats = (await client.get("/admin/stats", headers=superadmin)).json()
    assert stats["businesses_total"] == 2
    assert stats["users_total"] == 2  # the super admin isn't counted
    assert stats["receipts_total"] == 1
    assert stats["receipt_totals"] == [{"currency": "UGX", "amount_cents": 150000, "count": 1}]

    tenants = (await client.get("/admin/tenants", headers=superadmin)).json()
    assert tenants["total"] == 2
    acme = next(t for t in tenants["items"] if t["business_name"] == "Acme Rentals")
    assert acme["owner_email"] == "owner@example.com"
    assert acme["user_count"] == 1 and acme["receipt_count"] == 1

    found = (await client.get("/admin/tenants", params={"q": "second@"}, headers=superadmin)).json()
    assert [t["business_name"] for t in found["items"]] == ["Second Co"]

    detail = (await client.get(f"/admin/tenants/{acme['id']}", headers=superadmin)).json()
    assert detail["customer_count"] == 1
    assert [u["email"] for u in detail["users"]] == ["owner@example.com"]

    users = (await client.get("/admin/users", headers=superadmin)).json()
    assert users["total"] == 3  # two owners + the super admin


async def test_suspended_business_is_locked_out_until_reactivated(client, signup, superadmin):
    owner = await signup()
    tenant_id, _ = await _tenant_and_user(client, superadmin)

    resp = await client.post(f"/admin/tenants/{tenant_id}/suspend", json={"reason": "Unpaid"}, headers=superadmin)
    assert resp.status_code == 204
    assert (await client.get("/customers", headers=owner)).status_code == 401
    login = await client.post("/auth/login", json={"email": "owner@example.com", "password": "supersecret"})
    assert login.status_code == 403
    suspended = (await client.get("/admin/tenants", params={"status": "suspended"}, headers=superadmin)).json()
    assert suspended["items"][0]["suspended_reason"] == "Unpaid"

    await client.post(f"/admin/tenants/{tenant_id}/reactivate", headers=superadmin)
    assert (await client.get("/customers", headers=owner)).status_code == 200


async def test_suspended_user_is_locked_out_until_reactivated(client, signup, superadmin):
    owner = await signup()
    _, user_id = await _tenant_and_user(client, superadmin)

    assert (await client.post(f"/admin/users/{user_id}/suspend", json={}, headers=superadmin)).status_code == 204
    assert (await client.get("/customers", headers=owner)).status_code == 401
    login = await client.post("/auth/login", json={"email": "owner@example.com", "password": "supersecret"})
    assert login.status_code == 403

    await client.post(f"/admin/users/{user_id}/reactivate", headers=superadmin)
    assert (await client.get("/customers", headers=owner)).status_code == 200


async def test_role_change_keeps_a_business_owner(client, signup, superadmin, db_session):
    await signup()
    tenant_id, owner_id = await _tenant_and_user(client, superadmin)

    resp = await client.patch(f"/admin/users/{owner_id}", json={"role": "staff"}, headers=superadmin)
    assert resp.status_code == 409

    await _add_user(db_session, tenant_id, "owner2@example.com", "owner")
    resp = await client.patch(f"/admin/users/{owner_id}", json={"role": "staff", "is_verified": True},
                              headers=superadmin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "staff" and resp.json()["is_verified"] is True


async def test_cannot_manage_superadmins(client, superadmin):
    root = (await client.get("/admin/users", params={"q": "root@"}, headers=superadmin)).json()["items"][0]
    assert (await client.post(f"/admin/users/{root['id']}/suspend", json={}, headers=superadmin)).status_code == 403
    assert (await client.delete(f"/admin/users/{root['id']}", headers=superadmin)).status_code == 403


async def test_delete_user(client, signup, superadmin, db_session):
    owner = await signup()
    tenant_id, owner_id = await _tenant_and_user(client, superadmin)
    await _issue_receipt(client, owner)

    await _add_user(db_session, tenant_id, "staff@example.com", "staff")
    _, staff_id = await _tenant_and_user(client, superadmin, "staff@example.com")

    resp = await client.delete(f"/admin/users/{owner_id}", headers=superadmin)
    assert resp.status_code == 409
    assert "Suspend them instead" in resp.json()["error"]["message"]

    assert (await client.delete(f"/admin/users/{staff_id}", headers=superadmin)).status_code == 204
    users = (await client.get("/admin/users", params={"q": "staff@"}, headers=superadmin)).json()
    assert users["total"] == 0


async def test_password_reset_sends_email(client, signup, superadmin, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.auth.send_password_reset_email", lambda **kw: sent.append(kw))
    await signup()
    _, user_id = await _tenant_and_user(client, superadmin)

    assert (await client.post(f"/admin/users/{user_id}/password-reset", headers=superadmin)).status_code == 204
    assert sent and sent[0]["to_email"] == "owner@example.com"
    assert "/auth/password-reset/confirm?token=" in sent[0]["reset_url"]


async def test_act_as_business_is_audited_under_the_admin(client, signup, superadmin, db_session):
    owner = await signup()
    tenant_id, _ = await _tenant_and_user(client, superadmin)

    resp = await client.post(f"/admin/tenants/{tenant_id}/impersonate", headers=superadmin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["business_name"] == "Acme Rentals"
    act_as = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    # Owner-level access inside the business...
    created = await client.post("/customers", json={"name": "Via Admin", "email": "via@example.com"}, headers=act_as)
    assert created.status_code == 201
    assert (await client.get("/tenant/me", headers=act_as)).json()["business_name"] == "Acme Rentals"
    # ...but the act-as token can't reach the admin API.
    assert (await client.get("/admin/stats", headers=act_as)).status_code == 403

    admin_id = (await db_session.execute(select(User.id).where(User.is_superadmin.is_(True)))).scalar_one()
    actions = (await db_session.execute(
        select(AuditLog.action).where(AuditLog.actor_user_id == admin_id, AuditLog.tenant_id.is_not(None))
    )).scalars().all()
    assert "platform.impersonation_started" in actions

    # The business sees the admin's actions, flagged, in its own audit log.
    logs = (await client.get("/audit-logs", headers=owner)).json()["items"]
    started = next(log for log in logs if log["action"] == "platform.impersonation_started")
    assert started["actor_name"] == "Platform Admin" and started["platform_admin"] is True


async def test_platform_audit_log_lists_admin_actions(client, signup, superadmin):
    await signup()
    tenant_id, _ = await _tenant_and_user(client, superadmin)
    await client.post(f"/admin/tenants/{tenant_id}/suspend", json={}, headers=superadmin)

    logs = (await client.get("/admin/audit-logs", params={"platform_only": True}, headers=superadmin)).json()
    actions = [log["action"] for log in logs["items"]]
    assert "platform.tenant_suspended" in actions and "platform.login" in actions
    suspended = next(log for log in logs["items"] if log["action"] == "platform.tenant_suspended")
    assert suspended["business_name"] == "Acme Rentals"


async def test_seed_superadmin_is_idempotent(db_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.db.seed as seed
    from app.core.config import get_settings

    monkeypatch.setattr(seed, "AsyncSessionLocal", async_sessionmaker(db_engine, expire_on_commit=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "superadmin_email", "boss@platform.test")
    monkeypatch.setattr(settings, "superadmin_password", "first-password")

    await seed.seed_superadmin()
    monkeypatch.setattr(settings, "superadmin_password", "changed-in-env")
    await seed.seed_superadmin()

    async with async_sessionmaker(db_engine)() as session:
        admins = (await session.execute(select(User).where(User.email == "boss@platform.test"))).scalars().all()
    assert len(admins) == 1 and admins[0].is_superadmin and admins[0].tenant_id is None
    from app.core.security import verify_password

    assert verify_password("first-password", admins[0].password_hash)


async def test_seed_users_naming_the_same_business_share_it(db_engine, monkeypatch):
    import json

    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.db.seed as seed
    from app.core.config import get_settings

    monkeypatch.setattr(seed, "AsyncSessionLocal", async_sessionmaker(db_engine, expire_on_commit=False))
    monkeypatch.setattr(get_settings(), "seed_default_users", json.dumps([
        {"email": "boss@example.com", "password": "x", "business_name": "Demo Business", "role": "owner"},
        {"email": "helper@example.com", "password": "x", "business_name": "Demo Business", "role": "staff"},
        {"email": "other@example.com", "password": "x", "business_name": "Elsewhere", "role": "owner"},
    ]))

    await seed.seed_default_users()
    await seed.seed_default_users()  # idempotent

    async with async_sessionmaker(db_engine)() as session:
        users = {u.email: u for u in (await session.execute(select(User))).scalars()}
    assert len(users) == 3
    assert users["boss@example.com"].tenant_id == users["helper@example.com"].tenant_id
    assert users["other@example.com"].tenant_id != users["boss@example.com"].tenant_id

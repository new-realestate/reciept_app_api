from datetime import date

import pytest

pytestmark = pytest.mark.asyncio

TODAY = str(date.today())


async def test_owner_creates_staff_who_cannot_create_staff(client, signup, make_staff):
    owner = await signup()
    member, staff = await make_staff(owner)
    assert member["role"] == "staff" and member["is_active"] and member["uses_default_permissions"]

    listing = await client.get("/team/members", headers=owner)
    assert {m["email"] for m in listing.json()} == {"owner@example.com", "staff@example.com"}

    # Staff can't see or grow the team.
    assert (await client.get("/team/members", headers=staff)).status_code == 403
    resp = await client.post("/team/members", json={"full_name": "X", "email": "x@example.com"}, headers=staff)
    assert resp.status_code == 403


async def test_only_owner_can_create_or_promote_admins(client, signup, make_staff):
    owner = await signup()
    admin_member, admin = await make_staff(owner, email="admin@example.com", role="admin")
    assert admin_member["role"] == "admin"

    # An admin can add staff but not admins, and can't promote anyone to admin.
    staff_member, _ = await make_staff(admin, email="s2@example.com")
    assert (await client.post("/team/members", json={"full_name": "A", "email": "a2@example.com", "role": "admin"}, headers=admin)).status_code == 403
    assert (await client.patch(f"/team/members/{staff_member['id']}", json={"role": "admin"}, headers=admin)).status_code == 403

    # The owner can; "owner" is never a valid role through this API.
    assert (await client.patch(f"/team/members/{staff_member['id']}", json={"role": "admin"}, headers=owner)).json()["role"] == "admin"
    assert (await client.post("/team/members", json={"full_name": "O", "email": "o@example.com", "role": "owner"}, headers=owner)).status_code == 422


async def test_members_cannot_change_their_own_access(client, signup, make_staff):
    owner = await signup()
    admin_member, admin = await make_staff(owner, email="admin@example.com", role="admin")
    staff_member, staff = await make_staff(owner)

    assert (await client.patch(f"/team/members/{staff_member['id']}", json={"role": "admin"}, headers=staff)).status_code == 403
    assert (await client.patch(f"/team/members/{admin_member['id']}", json={"role": "staff"}, headers=admin)).status_code == 403
    me = (await client.get("/users/me", headers=owner)).json()
    assert (await client.post(f"/team/members/{me['id']}/deactivate", headers=owner)).status_code == 403
    # An admin can't touch the owner.
    assert (await client.post(f"/team/members/{me['id']}/deactivate", headers=admin)).status_code == 403


async def test_deactivation_takes_effect_immediately_and_is_reversible(client, signup, make_staff):
    owner = await signup()
    member, staff = await make_staff(owner)
    assert (await client.get("/receipts", headers=staff)).status_code == 200

    resp = await client.post(f"/team/members/{member['id']}/deactivate", json={"reason": "left"}, headers=owner)
    assert resp.json()["is_active"] is False
    assert (await client.get("/receipts", headers=staff)).status_code == 401
    login = await client.post("/auth/login", json={"email": "staff@example.com", "password": "staffsecret1"})
    assert login.status_code == 403

    await client.post(f"/team/members/{member['id']}/reactivate", headers=owner)
    login = await client.post("/auth/login", json={"email": "staff@example.com", "password": "staffsecret1"})
    assert login.status_code == 200

    actions = {e["action"] for e in (await client.get("/audit-logs", headers=owner)).json()["items"]}
    assert {"team.member_created", "team.member_deactivated", "team.member_reactivated"} <= actions


async def test_permissions_are_enforced_server_side(client, signup, make_staff):
    owner = await signup()
    cust = (await client.post("/customers", json={"name": "C", "email": "c@example.com"}, headers=owner)).json()
    member, staff = await make_staff(owner, permissions=["customers.view"])

    me = (await client.get("/users/me", headers=staff)).json()
    assert me["permissions"] == ["customers.view"] and me["is_manager"] is False
    assert (await client.get("/customers", headers=staff)).status_code == 200
    receipt = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, "customer_id": cust["id"], "amount_cents": 1000, "reason": "Rent",
              "payment_method": "cash", "paid_at": TODAY},
        headers=staff,
    )
    assert receipt.status_code == 403
    assert (await client.post("/customers", json={"name": "D", "email": "d@example.com"}, headers=staff)).status_code == 403

    # Granting the flag works at once; unknown flags are rejected.
    await client.patch(f"/team/members/{member['id']}", json={"permissions": ["customers.view", "receipts.issue"]}, headers=owner)
    receipt = await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, "customer_id": cust["id"], "amount_cents": 1000, "reason": "Rent",
              "payment_method": "cash", "paid_at": TODAY},
        headers=staff,
    )
    assert receipt.status_code == 201
    bad = await client.patch(f"/team/members/{member['id']}", json={"permissions": ["everything"]}, headers=owner)
    assert bad.status_code == 422


async def test_staff_cannot_change_business_settings(client, signup, make_staff):
    owner = await signup()
    _, staff = await make_staff(owner)
    assert (await client.patch("/tenant/me", json={"bank_name": "Evil Bank"}, headers=staff)).status_code == 403
    assert (await client.get("/tenant/me", headers=staff)).status_code == 200
    assert (await client.patch("/tenant/me", json={"bank_name": "Good Bank"}, headers=owner)).status_code == 200


async def test_business_branding_is_set_by_managers_and_shared_with_the_team(client, signup, make_staff):
    owner = await signup()
    _, staff = await make_staff(owner)

    ok = await client.patch("/tenant/me", json={"brand_color": "#1F7A5C"}, headers=owner)
    assert ok.status_code == 200 and ok.json()["brand_color"] == "#1f7a5c"
    for bad in ("red", "#12345", "1f7a5c", "#1f7a5cff"):
        assert (await client.patch("/tenant/me", json={"brand_color": bad}, headers=owner)).status_code == 422
    assert (await client.patch("/tenant/me", json={"brand_color": "#000000"}, headers=staff)).status_code == 403

    # Everyone in the business gets the same branding with their account details.
    for headers in (owner, staff):
        business = (await client.get("/users/me", headers=headers)).json()["business"]
        assert business["name"] == "Acme Rentals" and business["brand_color"] == "#1f7a5c"

    reset = await client.patch("/tenant/me", json={"brand_color": None}, headers=owner)
    assert reset.json()["brand_color"] is None
    assert (await client.get("/users/me", headers=staff)).json()["business"]["brand_color"] is None

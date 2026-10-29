import pytest

pytestmark = pytest.mark.asyncio


async def test_owner_creates_property_with_units_staff_cannot(client, signup, make_staff, make_property):
    owner = await signup()
    prop = await make_property(owner, units=["A1", "A2"])
    assert prop["unit_count"] == 2 and [u["label"] for u in prop["units"]] == ["A1", "A2"]

    dup = await client.post(f"/properties/{prop['id']}/units", json={"label": "A1"}, headers=owner)
    assert dup.status_code == 409

    _, staff = await make_staff(owner)
    assert (await client.post("/properties", json={"name": "Mine"}, headers=staff)).status_code == 403
    assert (await client.post(f"/properties/{prop['id']}/units", json={"label": "B"}, headers=staff)).status_code == 403


async def test_staff_see_only_assigned_properties(client, signup, make_staff, make_property, assign):
    owner = await signup()
    p1 = await make_property(owner, name="Assigned House")
    p2 = await make_property(owner, name="Other House")
    member, staff = await make_staff(owner)

    assert (await client.get("/properties", headers=staff)).json() == []
    assert (await client.get(f"/properties/{p1['id']}", headers=staff)).status_code == 404

    detail = await assign(owner, p1["id"], member["id"])
    assert [s["user_id"] for s in detail["assigned_staff"]] == [member["id"]]
    assert [p["id"] for p in (await client.get("/properties", headers=staff)).json()] == [p1["id"]]
    assert (await client.get(f"/properties/{p1['id']}", headers=staff)).status_code == 200
    assert (await client.get(f"/properties/{p2['id']}", headers=staff)).status_code == 404

    me = (await client.get("/users/me", headers=staff)).json()
    assert me["assigned_property_ids"] == [p1["id"]]


async def test_staff_cannot_assign_properties(client, signup, make_staff, make_property, assign):
    owner = await signup()
    prop = await make_property(owner)
    member, staff = await make_staff(owner)
    resp = await client.post(f"/properties/{prop['id']}/assignments", json={"user_id": member["id"]}, headers=staff)
    assert resp.status_code == 403
    # Even once assigned, staff can't assign others or revoke.
    await assign(owner, prop["id"], member["id"])
    other, _ = await make_staff(owner, email="other@example.com")
    resp = await client.post(f"/properties/{prop['id']}/assignments", json={"user_id": other["id"]}, headers=staff)
    assert resp.status_code == 403
    assert (await client.delete(f"/properties/{prop['id']}/assignments/{member['id']}", headers=staff)).status_code == 403


async def test_revoking_assignment_is_immediate_and_keeps_history(client, signup, make_staff, make_property, assign):
    owner = await signup()
    prop = await make_property(owner)
    member, staff = await make_staff(owner)
    await assign(owner, prop["id"], member["id"])
    assert (await client.get(f"/properties/{prop['id']}", headers=staff)).status_code == 200

    resp = await client.delete(f"/properties/{prop['id']}/assignments/{member['id']}", headers=owner)
    assert resp.status_code == 200 and resp.json()["assigned_staff"] == []
    assert (await client.get(f"/properties/{prop['id']}", headers=staff)).status_code == 404

    history = (await client.get(f"/properties/{prop['id']}/assignments", headers=owner)).json()
    assert len(history) == 1 and history[0]["active"] is False and history[0]["revoked_at"]

    # Re-assigning creates a new active row; the old one stays as history.
    await assign(owner, prop["id"], member["id"])
    history = (await client.get(f"/team/members/{member['id']}/assignments", headers=owner)).json()
    assert sorted(h["active"] for h in history) == [False, True]


async def test_cross_business_properties_and_members_are_invisible(client, signup, make_staff, make_property):
    owner_a = await signup("a@example.com", "Business A")
    owner_b = await signup("b@example.com", "Business B")
    prop_a = await make_property(owner_a)
    member_b, _ = await make_staff(owner_b, email="bstaff@example.com")
    prop_b = await make_property(owner_b)

    assert (await client.get(f"/properties/{prop_a['id']}", headers=owner_b)).status_code == 404
    assert (await client.patch(f"/properties/{prop_a['id']}", json={"name": "Mine now"}, headers=owner_b)).status_code == 404
    # A's owner can't assign B's staff, even to A's own property.
    resp = await client.post(f"/properties/{prop_a['id']}/assignments", json={"user_id": member_b["id"]}, headers=owner_a)
    assert resp.status_code == 404
    assert (await client.get(f"/team/members/{member_b['id']}", headers=owner_a)).status_code == 404
    assert [p["id"] for p in (await client.get("/properties", headers=owner_b)).json()] == [prop_b["id"]]


async def test_archived_property_keeps_history(client, signup, make_property):
    owner = await signup()
    prop = await make_property(owner)
    archived = await client.post(f"/properties/{prop['id']}/archive", headers=owner)
    assert archived.json()["status"] == "archived"
    assert (await client.get(f"/properties/{prop['id']}", headers=owner)).status_code == 200
    actions = {e["action"] for e in (await client.get("/audit-logs", headers=owner)).json()["items"]}
    assert {"property.created", "property.archived"} <= actions

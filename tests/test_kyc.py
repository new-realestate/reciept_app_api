import uuid

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ID_NUMBER = "CM90012345ABCD"


async def _upload(client, headers, profile_id, *, kind="id_front", data=PNG, name="id.png", ctype="image/png"):
    return await client.post(
        f"/kyc/profiles/{profile_id}/documents", data={"kind": kind}, files={"file": (name, data, ctype)}, headers=headers
    )


async def test_staff_kyc_submit_correct_resubmit_approve(client, signup, make_staff):
    owner = await signup()
    _, staff = await make_staff(owner)

    profile = (await client.get("/kyc/me", headers=staff)).json()
    assert profile["status"] == "not_submitted" and profile["subject_type"] == "staff"

    updated = (await client.put(f"/kyc/profiles/{profile['id']}", json={
        "legal_name": "Samuel Staff", "id_document_type": "national_id", "id_number": ID_NUMBER,
        "date_of_birth": "1990-01-01", "emergency_contact_name": "Mum", "emergency_contact_phone": "+256700000000",
    }, headers=staff)).json()
    assert updated["id_number_masked"] == "••••ABCD"
    assert ID_NUMBER not in str(updated)

    # Documents are required before submitting.
    assert (await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=staff)).status_code == 422
    assert (await _upload(client, staff, profile["id"])).status_code == 201
    submitted = (await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=staff)).json()
    assert submitted["status"] == "pending_review" and submitted["can_review"] is False

    # Nobody approves their own KYC.
    own = await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=staff)
    assert own.status_code == 403

    # A correction request needs a note.
    assert (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "requires_correction"}, headers=owner)).status_code == 422
    corrected = (await client.post(f"/kyc/profiles/{profile['id']}/review",
                                   json={"decision": "requires_correction", "note": "Photo is blurry"}, headers=owner)).json()
    assert corrected["status"] == "requires_correction" and corrected["review_note"] == "Photo is blurry"

    await _upload(client, staff, profile["id"], kind="id_back")
    assert (await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=staff)).json()["status"] == "pending_review"
    approved = (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=owner)).json()
    assert approved["status"] == "verified" and approved["verification_method"] == "manual"
    assert sorted(r["decision"] for r in approved["reviews"]) == ["approved", "requires_correction"]

    # Changing identity details needs a fresh review.
    changed = (await client.put(f"/kyc/profiles/{profile['id']}", json={"id_number": "NEW12345"}, headers=staff)).json()
    assert changed["status"] == "not_submitted"

    actions = {e["action"] for e in (await client.get("/audit-logs", headers=owner)).json()["items"]}
    assert {"kyc.submitted", "kyc.correction_requested", "kyc.approved", "kyc.document_uploaded"} <= actions


async def test_id_number_is_encrypted_at_rest_and_revealed_only_to_reviewers(client, signup, make_staff, db_session):
    from app.models.kyc import KycProfile

    owner = await signup()
    _, staff = await make_staff(owner)
    profile = (await client.get("/kyc/me", headers=staff)).json()
    await client.put(f"/kyc/profiles/{profile['id']}", json={"id_number": ID_NUMBER}, headers=staff)

    stored = (await db_session.execute(select(KycProfile.id_number_encrypted).where(KycProfile.user_id == uuid.UUID(profile["user_id"])))).scalar_one()
    assert stored and ID_NUMBER not in stored

    revealed = await client.post(f"/kyc/profiles/{profile['id']}/reveal-id-number", headers=owner)
    assert revealed.json()["id_number"] == ID_NUMBER
    logs = (await client.get("/audit-logs", params={"action": "kyc.id_number_revealed"}, headers=owner)).json()["items"]
    assert len(logs) == 1 and ID_NUMBER not in str(logs)


async def test_document_links_are_private_short_lived_and_audited(client, signup, make_staff):
    owner = await signup()
    _, staff = await make_staff(owner)
    profile = (await client.get("/kyc/me", headers=staff)).json()
    doc = (await _upload(client, staff, profile["id"])).json()

    link = (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=owner)).json()
    path = link["url"].replace("http://testserver", "")
    file_resp = await client.get(path)
    assert file_resp.status_code == 200 and file_resp.content == PNG
    assert "no-store" in file_resp.headers["cache-control"]

    # The link is not a login token, and stored files are never on the public /static route.
    token = path.rsplit("/", 1)[1]
    assert (await client.get("/receipts", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    assert (await client.get(f"/static/private/kyc/{profile['id']}")).status_code == 404
    assert (await client.get("/kyc/files/not-a-token")).status_code == 404

    # Another business can't mint a link.
    other = await signup("b@example.com", "Other")
    assert (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=other)).status_code == 404

    accessed = (await client.get("/audit-logs", params={"action": "kyc.document_accessed"}, headers=owner)).json()["items"]
    assert len(accessed) == 1


async def test_expired_link_is_refused(client, signup, make_staff, monkeypatch):
    from app.core.config import get_settings

    owner = await signup()
    _, staff = await make_staff(owner)
    profile = (await client.get("/kyc/me", headers=staff)).json()
    doc = (await _upload(client, staff, profile["id"])).json()
    monkeypatch.setattr(get_settings(), "kyc_download_link_seconds", -1)
    link = (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=owner)).json()
    assert (await client.get(link["url"].replace("http://testserver", ""))).status_code == 404


async def test_upload_rejects_disguised_files(client, signup, make_staff):
    owner = await signup()
    _, staff = await make_staff(owner)
    profile = (await client.get("/kyc/me", headers=staff)).json()
    resp = await _upload(client, staff, profile["id"], data=b"<html>not an image</html>", name="id.png", ctype="image/png")
    assert resp.status_code == 400


async def test_renter_kyc_permissions(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    prop = await make_property(owner)
    other_prop = await make_property(owner, name="Elsewhere")
    manager_member, manager_staff = await make_staff(owner, email="m@example.com")
    reviewer_member, reviewer = await make_staff(
        owner, email="r@example.com", permissions=["renters.manage", "renters.kyc_review"]
    )
    outsider_member, outsider = await make_staff(owner, email="o@example.com", permissions=["renters.manage", "renters.kyc_review"])
    await assign(owner, prop["id"], manager_member["id"])
    await assign(owner, prop["id"], reviewer_member["id"])
    await assign(owner, other_prop["id"], outsider_member["id"])

    renter = await make_renter(manager_staff, prop["id"])
    profile = (await client.get(f"/renters/{renter['id']}/kyc", headers=manager_staff)).json()
    assert profile["can_edit"] and not profile["can_view_documents"]

    await client.put(f"/kyc/profiles/{profile['id']}", json={
        "legal_name": "Rita Renter", "id_document_type": "passport", "id_number": "B1234567",
    }, headers=manager_staff)
    doc = (await _upload(client, manager_staff, profile["id"], kind="passport_page")).json()

    # Uploading doesn't grant download; staff outside the property see nothing at all.
    assert (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=manager_staff)).status_code == 403
    assert (await client.get(f"/kyc/profiles/{profile['id']}", headers=outsider)).status_code == 404
    assert (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=outsider)).status_code == 404

    await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=manager_staff)
    # Plain staff can't review; a reviewer in scope can, and can open the document.
    assert (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=manager_staff)).status_code == 403
    assert [i["profile_id"] for i in (await client.get("/kyc/review-queue", headers=reviewer)).json()] == [profile["id"]]
    assert (await client.get("/kyc/review-queue", headers=outsider)).json() == []
    assert (await client.post(f"/kyc/documents/{doc['id']}/download-url", headers=reviewer)).status_code == 200
    approved = await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=reviewer)
    assert approved.json()["status"] == "verified"


async def test_staff_reviewer_cannot_approve_their_own_submission(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    prop = await make_property(owner)
    member, reviewer = await make_staff(owner, permissions=["renters.manage", "renters.kyc_review"])
    await assign(owner, prop["id"], member["id"])
    renter = await make_renter(reviewer, prop["id"])
    profile = (await client.get(f"/renters/{renter['id']}/kyc", headers=reviewer)).json()
    await client.put(f"/kyc/profiles/{profile['id']}", json={"legal_name": "R", "id_document_type": "national_id", "id_number": "X12345"}, headers=reviewer)
    await _upload(client, reviewer, profile["id"])
    await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=reviewer)
    assert (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=reviewer)).status_code == 403
    assert (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=owner)).status_code == 200


async def test_kyc_verification_does_not_change_account_or_tenancy_status(client, signup, make_property, make_renter):
    owner = await signup()
    prop = await make_property(owner)
    renter = await make_renter(owner, prop["id"], tenancy={
        "property_id": prop["id"], "start_date": "2026-01-01", "rent_amount_cents": 1, "status": "draft",
    })
    profile = (await client.get(f"/renters/{renter['id']}/kyc", headers=owner)).json()
    await client.put(f"/kyc/profiles/{profile['id']}", json={"legal_name": "R", "id_document_type": "national_id", "id_number": "X12345"}, headers=owner)
    await _upload(client, owner, profile["id"])
    await client.post(f"/kyc/profiles/{profile['id']}/submit", headers=owner)
    # The owner submitted it, but managers may still review renter KYC.
    assert (await client.post(f"/kyc/profiles/{profile['id']}/review", json={"decision": "approved"}, headers=owner)).json()["status"] == "verified"
    detail = (await client.get(f"/renters/{renter['id']}", headers=owner)).json()
    assert detail["tenancies"][0]["status"] == "draft"

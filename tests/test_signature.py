import base64

import pytest

from app.services.pdf import local_file_uri

pytestmark = pytest.mark.asyncio

# 1x1 transparent PNG, the format the portal's signature pad uploads.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


async def _upload(client, headers, data=PNG, content_type="image/png"):
    return await client.post("/users/me/signature", files={"file": ("signature.png", data, content_type)}, headers=headers)


async def _issue_receipt(client, headers):
    resp = await client.post(
        "/receipts",
        json={"customer_name": "John Payer", "customer_email": "payer@example.com", "amount_cents": 150000,
              "currency": "UGX", "reason": "Rent", "payment_method": "cash", "paid_at": "2026-07-01"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _static_path(url: str) -> str:
    return "/static/" + url.split("/static/", 1)[1]


async def test_saved_signature_is_returned_and_served(client, signup):
    headers = await signup()
    assert (await client.get("/users/me", headers=headers)).json()["signature_image_url"] is None

    resp = await _upload(client, headers)
    assert resp.status_code == 200
    url = resp.json()["signature_image_url"]

    me = (await client.get("/users/me", headers=headers)).json()
    assert me["signature_image_url"] == url and me["email"] == "owner@example.com" and me["role"] == "owner"
    served = await client.get(_static_path(url))
    assert served.status_code == 200 and served.content == PNG and served.headers["content-type"] == "image/png"


async def test_new_receipts_carry_the_signature_into_the_pdf(client, signup):
    headers = await signup()
    url = (await _upload(client, headers)).json()["signature_image_url"]

    receipt = await _issue_receipt(client, headers)
    assert receipt["signature_image_url"] == url
    # The PDF renderer embeds the stored image rather than fetching it over HTTP.
    assert local_file_uri(url).startswith("data:image/png;base64,")
    pdf = await client.get(_static_path(receipt["pdf_url"]))
    assert pdf.content.startswith(b"%PDF")


async def test_replacing_keeps_earlier_receipts_signature(client, signup):
    headers = await signup()
    first = (await _upload(client, headers)).json()["signature_image_url"]
    earlier = await _issue_receipt(client, headers)

    second = (await _upload(client, headers)).json()["signature_image_url"]
    assert second != first
    assert (await client.get("/users/me", headers=headers)).json()["signature_image_url"] == second
    assert (await _issue_receipt(client, headers))["signature_image_url"] == second
    # The earlier receipt still links to the signature it was issued with.
    assert earlier["signature_image_url"] == first
    assert (await client.get(_static_path(first))).status_code == 200


async def test_removing_the_signature(client, signup):
    headers = await signup()
    url = (await _upload(client, headers)).json()["signature_image_url"]
    earlier = await _issue_receipt(client, headers)

    assert (await client.delete("/users/me/signature", headers=headers)).status_code == 204
    assert (await client.get("/users/me", headers=headers)).json()["signature_image_url"] is None

    later = await _issue_receipt(client, headers)
    assert later["signature_image_url"] is None
    assert (await client.get(_static_path(later["pdf_url"]))).content.startswith(b"%PDF")
    assert (await client.get(_static_path(url))).status_code == 200
    assert earlier["signature_image_url"] == url


async def test_signatures_are_per_user(client, signup):
    owner_a = await signup()
    owner_b = await signup(email="other@example.com", business="Other Co")
    await _upload(client, owner_a)

    assert (await client.get("/users/me", headers=owner_b)).json()["signature_image_url"] is None
    await client.delete("/users/me/signature", headers=owner_b)
    assert (await client.get("/users/me", headers=owner_a)).json()["signature_image_url"] is not None


async def test_rejects_non_images_and_anonymous_requests(client, signup):
    headers = await signup()
    assert (await _upload(client, headers, data=b"not an image", content_type="text/plain")).status_code == 400
    assert (await client.get("/users/me")).status_code == 401
    assert (await client.delete("/users/me/signature")).status_code == 401

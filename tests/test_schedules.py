from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.asyncio

TODAY = date.today()


# ------------------------------------------------------------------ API


def _body(**overrides):
    body = {
        "customer_name": "Grace Tenant",
        "customer_email": "grace@example.com",
        "customer_phone": "+256701234567",
        "description": "Monthly rent - Plot 12",
        "reference_label": "Plot 12 tenancy",
        "amount_cents": 50000000,
        "frequency": "monthly",
        "first_due_date": str(TODAY + timedelta(days=10)),
    }
    body.update(overrides)
    return body


async def test_create_monthly_schedule_generates_occurrences(client, signup):
    headers = await signup()
    resp = await client.post("/schedules", json=_body(), headers=headers)
    assert resp.status_code == 201, resp.text
    s = resp.json()
    assert s["status"] == "active" and s["next_due_date"] == str(TODAY + timedelta(days=10))
    occ = s["occurrences"]
    assert 3 <= len(occ) <= 5  # first + those inside the 90-day horizon
    assert [o["seq"] for o in occ] == list(range(len(occ)))
    assert all(o["status"] == "expected" and o["amount_cents"] == 50000000 for o in occ)
    assert occ[0]["days_until_due"] == 10


async def test_one_time_schedule_and_limits(client, signup):
    headers = await signup()
    once = (await client.post("/schedules", json=_body(frequency="once"), headers=headers)).json()
    assert len(once["occurrences"]) == 1
    limited = (await client.post("/schedules", json=_body(max_occurrences=2), headers=headers)).json()
    assert len(limited["occurrences"]) == 2
    ended = (await client.post("/schedules", json=_body(end_date=str(TODAY + timedelta(days=11))), headers=headers)).json()
    assert len(ended["occurrences"]) == 1
    bad = await client.post("/schedules", json=_body(frequency="once", max_occurrences=3), headers=headers)
    assert bad.status_code == 422
    bad2 = await client.post("/schedules", json=_body(end_date=str(TODAY - timedelta(days=1))), headers=headers)
    assert bad2.status_code == 422


async def test_pause_resume_cancel(client, signup, db_session):
    from app.models.notification import NotificationEvent

    headers = await signup()
    s = (await client.post("/schedules", json=_body(first_due_date=str(TODAY + timedelta(days=5))), headers=headers)).json()
    sid = s["id"]

    scheduled = (await db_session.execute(select(NotificationEvent).where(NotificationEvent.status == "scheduled"))).scalars().all()
    assert scheduled and all(e.subject_type == "occurrence" for e in scheduled)

    paused = await client.post(f"/schedules/{sid}/pause", headers=headers)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    db_session.expire_all()
    still = (await db_session.execute(select(NotificationEvent).where(NotificationEvent.status == "scheduled"))).scalars().all()
    assert still == []  # pausing stops future reminders

    resumed = await client.post(f"/schedules/{sid}/resume", headers=headers)
    assert resumed.json()["status"] == "active"
    db_session.expire_all()
    replanned = (await db_session.execute(select(NotificationEvent).where(NotificationEvent.status == "scheduled"))).scalars().all()
    assert replanned  # reactivated, not duplicated
    keys = [e.dedupe_key for e in (await db_session.execute(select(NotificationEvent))).scalars().all()]
    assert len(keys) == len(set(keys))

    cancelled = await client.post(f"/schedules/{sid}/cancel", headers=headers)
    assert cancelled.json()["status"] == "cancelled"
    assert all(o["status"] == "skipped" for o in cancelled.json()["occurrences"])
    assert (await client.post(f"/schedules/{sid}/resume", headers=headers)).status_code == 409
    assert (await client.post(f"/schedules/{sid}/pause", headers=headers)).status_code == 409


async def test_create_invoice_from_occurrence_once_and_sync_payment(client, signup):
    headers = await signup()
    s = (await client.post("/schedules", json=_body(first_due_date=str(TODAY + timedelta(days=3))), headers=headers)).json()
    occ = s["occurrences"][0]

    resp = await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    inv = resp.json()
    assert inv["status"] == "issued" and inv["total_cents"] == 50000000 and inv["due_date"] == occ["due_date"]

    dup = await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={}, headers=headers)
    assert dup.status_code == 409

    detail = (await client.get(f"/schedules/{s['id']}", headers=headers)).json()
    first = detail["occurrences"][0]
    assert first["status"] == "invoiced" and first["invoice_number"] == inv["number"]

    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 50000000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    detail = (await client.get(f"/schedules/{s['id']}", headers=headers)).json()
    assert detail["occurrences"][0]["status"] == "paid"
    assert detail["next_due_date"] == detail["occurrences"][1]["due_date"]


async def test_cancelling_invoice_releases_occurrence(client, signup):
    headers = await signup()
    s = (await client.post("/schedules", json=_body(), headers=headers)).json()
    occ = s["occurrences"][0]
    inv = (await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={"issue_now": True}, headers=headers)).json()
    await client.post(f"/invoices/{inv['id']}/cancel", headers=headers)
    detail = (await client.get(f"/schedules/{s['id']}", headers=headers)).json()
    assert detail["occurrences"][0]["status"] == "expected"
    again = await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={}, headers=headers)
    assert again.status_code == 201


async def test_skip_occurrence_and_edit_amount(client, signup):
    headers = await signup()
    s = (await client.post("/schedules", json=_body(), headers=headers)).json()
    first, second = s["occurrences"][0], s["occurrences"][1]
    skipped = await client.post(f"/schedules/occurrences/{first['id']}/skip", headers=headers)
    assert skipped.status_code == 200 and skipped.json()["status"] == "skipped"
    assert (await client.post(f"/schedules/occurrences/{first['id']}/skip", headers=headers)).status_code == 409

    edit = await client.patch(f"/schedules/{s['id']}", json={"amount_cents": 60000000}, headers=headers)
    assert edit.status_code == 200
    amounts = {o["seq"]: o["amount_cents"] for o in edit.json()["occurrences"]}
    assert amounts[second["seq"]] == 60000000


async def test_upcoming_and_overdue_views(client, signup, make_invoice):
    headers = await signup()
    await make_invoice(headers, title="Due soon", due_date=str(TODAY + timedelta(days=5)))
    await make_invoice(headers, title="Due today", due_date=str(TODAY), issue_date=str(TODAY))
    await make_invoice(headers, title="Late", due_date=str(TODAY - timedelta(days=4)), issue_date=str(TODAY - timedelta(days=20)))
    far = await make_invoice(headers, title="Far away", due_date=str(TODAY + timedelta(days=90)))
    paid = await make_invoice(headers, title="Already paid", due_date=str(TODAY - timedelta(days=1)), issue_date=str(TODAY - timedelta(days=9)))
    await client.post(
        f"/invoices/{paid['id']}/payments",
        json={"amount_cents": paid["total_cents"], "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    await client.post("/schedules", json=_body(description="Service plan", first_due_date=str(TODAY + timedelta(days=2)), frequency="once"), headers=headers)

    allrows = (await client.get("/payments/upcoming", headers=headers)).json()
    titles = [r["description"] for r in allrows["items"]]
    assert titles == ["Late", "Due today", "Service plan", "Due soon"]  # sorted by due date; far + paid excluded
    by_title = {r["description"]: r for r in allrows["items"]}
    assert by_title["Late"]["status"] == "overdue" and by_title["Late"]["days_overdue"] == 4 and by_title["Late"]["days_until_due"] == -4
    assert by_title["Due today"]["status"] == "due_today"
    assert by_title["Due soon"]["days_until_due"] == 5 and by_title["Service plan"]["kind"] == "occurrence"

    overdue = (await client.get("/payments/overdue", headers=headers)).json()
    assert [r["description"] for r in overdue["items"]] == ["Late"]
    wide = (await client.get("/payments/upcoming?window_days=120&status=upcoming", headers=headers)).json()
    assert far["title"] in [r["description"] for r in wide["items"]]

    cust = by_title["Late"]["customer_id"]
    assert (await client.get(f"/payments/upcoming?customer_id={cust}", headers=headers)).json()["total"] == 4
    assert (await client.get(f"/payments/upcoming?customer_id={cust}&date_from={TODAY}&date_to={TODAY}", headers=headers)).json()["total"] == 1
    page = (await client.get("/payments/upcoming?limit=2&offset=1", headers=headers)).json()
    assert page["total"] == 4 and len(page["items"]) == 2


async def test_schedules_are_business_isolated(client, signup):
    headers_a = await signup("a@example.com", "Biz A")
    headers_b = await signup("b@example.com", "Biz B")
    s = (await client.post("/schedules", json=_body(), headers=headers_a)).json()
    occ = s["occurrences"][0]
    assert (await client.get(f"/schedules/{s['id']}", headers=headers_b)).status_code == 404
    assert (await client.post(f"/schedules/{s['id']}/pause", headers=headers_b)).status_code == 404
    assert (await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={}, headers=headers_b)).status_code == 404
    assert (await client.get("/schedules", headers=headers_b)).json()["total"] == 0
    assert (await client.get("/payments/upcoming", headers=headers_b)).json()["total"] == 0


async def test_auto_invoice_is_opt_in_and_not_duplicated(client, signup, db_session, outbox):
    from app.services import scheduler

    headers = await signup()
    off = (await client.post("/schedules", json=_body(first_due_date=str(TODAY + timedelta(days=2)), frequency="once"), headers=headers)).json()
    on = (await client.post("/schedules", json=_body(description="Auto plan", first_due_date=str(TODAY + timedelta(days=2)),
                                                    frequency="once", auto_invoice=True), headers=headers)).json()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    first = await scheduler.run_cycle(factory)
    second = await scheduler.run_cycle(factory)
    assert first["auto_invoice"] == 1 and second["auto_invoice"] == 0

    invoices = (await client.get("/invoices", headers=headers)).json()
    assert invoices["total"] == 1 and invoices["items"][0]["title"] == "Auto plan"
    assert any("Invoice" in kw["subject"] for kind, kw in outbox if kind == "email")
    off_detail = (await client.get(f"/schedules/{off['id']}", headers=headers)).json()
    assert off_detail["occurrences"][0]["status"] == "expected"  # not auto-invoiced
    assert on["auto_invoice"] is True


async def test_finished_schedule_completes(client, signup, db_session):
    from app.services import scheduler

    headers = await signup()
    s = (await client.post("/schedules", json=_body(frequency="once", first_due_date=str(TODAY + timedelta(days=1))), headers=headers)).json()
    occ = s["occurrences"][0]
    inv = (await client.post(f"/schedules/occurrences/{occ['id']}/create-invoice", json={}, headers=headers)).json()
    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": inv["total_cents"], "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    stats = await scheduler.run_cycle(async_sessionmaker(db_session.bind, expire_on_commit=False))
    assert stats["schedules_completed"] == 1
    assert (await client.get(f"/schedules/{s['id']}", headers=headers)).json()["status"] == "completed"

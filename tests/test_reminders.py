from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.notification import NotificationAttempt, NotificationEvent
from app.services import notifications as notif
from app.services import reminders

pytestmark = pytest.mark.asyncio

TODAY = date.today()
DUE = TODAY + timedelta(days=14)


def at(day: date, hour: int = 7) -> datetime:
    """A UTC instant on `day` (07:00 UTC is after 09:00 in Kampala? no: 09:00 EAT == 06:00 UTC, so 07:00 UTC is just after)."""
    return datetime.combine(day, time(hour, 0), tzinfo=timezone.utc)


async def events(db_session, **filters):
    db_session.expire_all()
    stmt = select(NotificationEvent).order_by(NotificationEvent.scheduled_for, NotificationEvent.channel)
    for k, v in filters.items():
        stmt = stmt.where(getattr(NotificationEvent, k) == v)
    return (await db_session.execute(stmt)).scalars().all()


async def run_due(db_session, now: datetime) -> int:
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as db:
        return await notif.process_due_events(db, now=now)


async def test_issuing_plans_reminders_once_per_offset_and_channel(client, signup, make_invoice, db_session):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    evs = await events(db_session, subject_id=__import__("uuid").UUID(inv["id"]))
    assert len(evs) == 6  # offsets -3, 0, +3 x email, sms
    assert {e.kind for e in evs} == {"reminder_upcoming", "reminder_due", "reminder_overdue"}
    assert all(e.status == "scheduled" for e in evs)

    # planning again (e.g. the periodic job) must not create duplicates
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    for _ in range(2):
        async with factory() as db:
            await reminders.plan_all(db)
            await db.commit()
    assert len(await events(db_session)) == 6


async def test_send_times_follow_business_timezone(client, signup, make_invoice, db_session):
    headers = await signup()
    default_inv = await make_invoice(headers, due_date=str(DUE))
    due_event = [e for e in await events(db_session, subject_id=__import__("uuid").UUID(default_inv["id"]), kind="reminder_due")][0]
    # 09:00 in Africa/Kampala (UTC+3) is 06:00 UTC
    assert due_event.scheduled_for.replace(tzinfo=timezone.utc) == at(DUE, 6)

    resp = await client.patch("/tenant/me", json={"timezone": "America/New_York"}, headers=headers)
    assert resp.status_code == 200 and resp.json()["timezone"] == "America/New_York"
    ny_inv = await make_invoice(headers, due_date=str(DUE), customer_email="other@example.com", customer_phone=None, customer_name="Other")
    ny_event = [e for e in await events(db_session, subject_id=__import__("uuid").UUID(ny_inv["id"]), kind="reminder_due")][0]
    expected = datetime.combine(DUE, time(9, 0), tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert ny_event.scheduled_for.replace(tzinfo=timezone.utc) == expected

    assert (await client.patch("/tenant/me", json={"timezone": "Mars/Olympus"}, headers=headers)).status_code == 422


async def test_due_reminders_are_sent_once_and_in_order(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    outbox.clear()

    # three days before: only the "upcoming" pair is due
    assert await run_due(db_session, at(DUE - timedelta(days=3))) == 2
    assert {kw["subject"] for kind, kw in outbox if kind == "email"} == {f"Reminder: invoice {inv['number']} is due in 3 days"}

    # same instant again: nothing new goes out
    assert await run_due(db_session, at(DUE - timedelta(days=3))) == 0
    assert len(outbox) == 2

    # on the due date
    await run_due(db_session, at(DUE))
    assert any(kw["subject"].endswith("is due today") for kind, kw in outbox if kind == "email")
    # three days after: overdue follow-up
    await run_due(db_session, at(DUE + timedelta(days=3)))
    assert any("overdue" in kw["subject"] for kind, kw in outbox if kind == "email")
    assert len(outbox) == 6

    done = await events(db_session)
    assert all(e.status == "sent" and e.sent_at for e in done)
    attempts = (await db_session.execute(select(NotificationAttempt))).scalars().all()
    assert len(attempts) == 6 and all(a.status == "sent" and a.provider_message_id for a in attempts)


async def test_no_reminders_for_paid_invoices(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    outbox.clear()
    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": inv["total_cents"], "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    outbox.clear()  # drop the payment confirmation
    pending = [e for e in await events(db_session, kind="reminder_due")]
    assert pending and all(e.status == "cancelled" for e in pending)  # stopped at payment time
    assert await run_due(db_session, at(DUE + timedelta(days=5))) == 0
    assert outbox == []


async def test_safety_net_skips_if_paid_without_cancellation(client, signup, make_invoice, db_session, outbox):
    """Even if a pending event survives (e.g. a payment path forgot to cancel it), sending re-checks the invoice."""
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    outbox.clear()
    await db_session.execute(text("UPDATE invoices SET amount_paid_cents = total_cents"))
    await db_session.commit()

    await run_due(db_session, at(DUE + timedelta(days=5)))
    assert outbox == []
    evs = await events(db_session)
    assert evs and all(e.status == "skipped" and e.skip_reason == "invoice is fully paid" for e in evs)
    log = (await db_session.execute(select(NotificationAttempt))).scalars().all()
    assert log and all(a.status == "skipped" for a in log)  # audit trail records the skip
    assert inv["id"]


async def test_no_reminders_for_cancelled_invoices(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    outbox.clear()
    await client.post(f"/invoices/{inv['id']}/cancel", headers=headers)
    assert all(e.status == "cancelled" for e in await events(db_session))
    assert await run_due(db_session, at(DUE + timedelta(days=5))) == 0
    assert outbox == []
    # a manual retry of a cancelled reminder still cannot chase a cancelled invoice
    ev = (await events(db_session))[0]
    resp = await client.post(f"/notifications/{ev.id}/retry", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "skipped" and outbox == []


async def test_suppressed_recipients_are_skipped(client, signup, make_invoice, db_session, outbox):
    from app.models.enums import DeliveryChannel
    from app.models.suppression import Suppression

    headers = await signup()
    await make_invoice(headers, due_date=str(DUE))
    outbox.clear()
    db_session.add(Suppression(tenant_id=None, channel=DeliveryChannel.email, contact="grace@example.com", reason="hard_bounce"))
    db_session.add(Suppression(tenant_id=None, channel=DeliveryChannel.sms, contact="+256701234567", reason="stop_keyword"))
    await db_session.commit()

    await run_due(db_session, at(DUE + timedelta(days=5)))
    assert outbox == []
    evs = await events(db_session)
    assert all(e.status == "skipped" and "suppression" in e.skip_reason for e in evs)


async def test_failed_sends_are_retried_with_cap(client, signup, make_invoice, db_session, monkeypatch):
    headers = await signup()
    await make_invoice(headers, due_date=str(DUE), customer_phone=None)  # email only: 3 events
    calls = {"n": 0, "fail": True}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError("provider outage")
        return {"MessageID": "ok"}

    monkeypatch.setattr(notif, "send_document_email", flaky)
    now = at(DUE - timedelta(days=3))
    await run_due(db_session, now)
    first = await events(db_session, kind="reminder_upcoming")
    assert [e.status for e in first] == ["failed"] and first[0].attempts == 1
    assert calls["n"] == notif.EMAIL_RETRY_ATTEMPTS

    # the provider recovers: the next cycle retries and succeeds, exactly once
    calls["fail"] = False
    await run_due(db_session, now)
    await run_due(db_session, now)
    sent = await events(db_session, kind="reminder_upcoming")
    assert sent[0].status == "sent" and sent[0].attempts == 2
    log = (await db_session.execute(select(NotificationAttempt).where(NotificationAttempt.event_id == sent[0].id))).scalars().all()
    assert sorted(a.status for a in log) == ["failed", "sent"]

    # a permanently failing provider is not hammered forever
    calls["fail"] = True
    for _ in range(6):
        await run_due(db_session, at(DUE + timedelta(days=3)))
    stuck = await events(db_session, kind="reminder_overdue")
    assert stuck[0].status == "failed" and stuck[0].attempts == notif.MAX_AUTO_ATTEMPTS


async def test_concurrent_processing_only_sends_once(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    await make_invoice(headers, due_date=str(DUE), customer_phone=None)
    outbox.clear()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    ev = (await events(db_session, kind="reminder_upcoming"))[0]
    async with factory() as a, factory() as b:
        ea = (await a.execute(select(NotificationEvent).where(NotificationEvent.id == ev.id))).scalar_one()
        eb = (await b.execute(select(NotificationEvent).where(NotificationEvent.id == ev.id))).scalar_one()
        await notif.process_event(a, ea)
        await a.commit()
        await notif.process_event(b, eb)
        await b.commit()
    assert len(outbox) == 1


async def test_manual_reminder_rules(client, signup, make_invoice, outbox):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    outbox.clear()
    first = await client.post(f"/invoices/{inv['id']}/remind", json={"channels": ["email"]}, headers=headers)
    assert first.status_code == 200 and first.json()[0]["status"] == "sent"
    assert any("Payment reminder" in kw["subject"] for kind, kw in outbox)
    again = await client.post(f"/invoices/{inv['id']}/remind", json={"channels": ["email"]}, headers=headers)
    assert again.status_code == 409  # cooldown: no excessive messaging
    other_channel = await client.post(f"/invoices/{inv['id']}/remind", json={"channels": ["sms"]}, headers=headers)
    assert other_channel.status_code == 200

    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": inv["total_cents"], "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    assert (await client.post(f"/invoices/{inv['id']}/remind", json={"channels": ["email"]}, headers=headers)).status_code == 409

    draft = await make_invoice(headers, issue=False, customer_email="d@example.com")
    assert (await client.post(f"/invoices/{draft['id']}/remind", json={"channels": ["email"]}, headers=headers)).status_code == 409


async def test_per_invoice_reminder_controls(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE), customer_phone=None)
    outbox.clear()

    off = await client.patch(f"/invoices/{inv['id']}/reminders", json={"enabled": False}, headers=headers)
    assert off.status_code == 200 and off.json()["reminders_enabled"] is False
    assert all(e.status == "cancelled" for e in await events(db_session))
    await run_due(db_session, at(DUE + timedelta(days=5)))
    assert outbox == []

    on = await client.patch(f"/invoices/{inv['id']}/reminders", json={"enabled": True, "offsets": [-1]}, headers=headers)
    assert on.status_code == 200
    active = [e for e in await events(db_session) if e.status == "scheduled"]
    assert len(active) == 1 and active[0].kind == "reminder_upcoming"
    assert active[0].dedupe_key.endswith(":-1:email")

    bad = await client.patch(f"/invoices/{inv['id']}/reminders", json={"offsets": [9999]}, headers=headers)
    assert bad.status_code == 422


async def test_business_wide_settings_and_roles(client, signup, make_invoice, db_session):
    headers = await signup()
    current = (await client.get("/reminder-settings", headers=headers)).json()
    assert current["offsets"] == [-3, 0, 3] and current["channels"] == ["email", "sms"] and current["send_hour"] == 9

    inv = await make_invoice(headers, due_date=str(DUE), customer_phone=None)
    put = await client.put("/reminder-settings", json={"offsets": [-7, -1], "send_hour": 8}, headers=headers)
    assert put.status_code == 200 and put.json()["offsets"] == [-7, -1] and put.json()["send_hour"] == 8
    live = [e for e in await events(db_session) if e.status == "scheduled"]
    assert sorted(e.dedupe_key.split(":")[-2] for e in live) == ["-1", "-7"]

    assert (await client.put("/reminder-settings", json={"channels": ["pigeon"]}, headers=headers)).status_code == 422
    assert (await client.put("/reminder-settings", json={"send_hour": 30}, headers=headers)).status_code == 422
    assert inv["id"]

    # staff members can see but not change business-wide settings
    await db_session.execute(text("UPDATE users SET role = 'staff'"))
    await db_session.commit()
    assert (await client.get("/reminder-settings", headers=headers)).status_code == 200
    assert (await client.put("/reminder-settings", json={"enabled": False}, headers=headers)).status_code == 403


async def test_business_copy_when_enabled(client, signup, make_invoice, db_session, outbox):
    headers = await signup()
    await client.patch("/tenant/me", json={"contact_email": "owner@acme-biz.example.com"}, headers=headers)
    await client.put("/reminder-settings", json={"notify_business": True, "channels": ["email"]}, headers=headers)
    await make_invoice(headers, due_date=str(DUE), customer_phone=None)
    outbox.clear()
    await run_due(db_session, at(DUE))
    to_business = [kw for kind, kw in outbox if kw["to_email"] == "owner@acme-biz.example.com"]
    assert len(to_business) == 1 and "due today" in to_business[0]["subject"].lower()


async def test_notification_history_filters_and_cancel(client, signup, make_invoice, db_session):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))
    listing = (await client.get(f"/notifications?subject_id={inv['id']}&status=scheduled", headers=headers)).json()
    assert listing["total"] == 6 and len(listing["items"]) == 6
    page = (await client.get("/notifications?limit=2&offset=0", headers=headers)).json()
    assert len(page["items"]) == 2 and page["total"] == 6

    target = listing["items"][0]
    cancelled = await client.post(f"/notifications/{target['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await client.post(f"/notifications/{target['id']}/cancel", headers=headers)).status_code == 409

    other = await signup("b@example.com", "Biz B")
    assert (await client.get("/notifications", headers=other)).json()["total"] == 0
    assert (await client.post(f"/notifications/{target['id']}/retry", headers=other)).status_code == 404


async def test_dashboard_separates_invoiced_from_received(client, signup, make_invoice):
    headers = await signup()
    inv = await make_invoice(headers, due_date=str(DUE))  # 500,000.00 invoiced
    await make_invoice(headers, due_date=str(TODAY - timedelta(days=2)), issue_date=str(TODAY - timedelta(days=10)))  # overdue
    await client.post(
        f"/invoices/{inv['id']}/payments",
        json={"amount_cents": 20000000, "paid_at": str(TODAY), "payment_method": "cash"},
        headers=headers,
    )
    q = (await client.post("/quotations", json={
        "customer_name": "P", "customer_email": "p@example.com", "valid_until": str(DUE),
        "line_items": [{"description": "x", "quantity": "1", "unit_price_cents": 7000}]}, headers=headers)).json()
    await client.post(f"/quotations/{q['id']}/send", json={"channels": ["email"]}, headers=headers)

    summary = (await client.get("/dashboard/summary?period=all", headers=headers)).json()
    cur = summary["by_currency"]["UGX"]
    assert summary["default_currency"] == "UGX" and summary["period"] == "all"
    assert cur["invoiced_cents"] == 100000000
    assert cur["received_cents"] == 20000000 and cur["received_on_invoices_cents"] == 20000000
    assert cur["outstanding_cents"] == 80000000
    assert cur["overdue_cents"] == 50000000 and cur["overdue_count"] == 1
    assert cur["upcoming_30_count"] == 1 and cur["upcoming_30_cents"] == 30000000
    assert cur["pending_quotations_count"] == 1 and cur["pending_quotations_cents"] == 7000
    assert len(summary["recent_payments"]) == 1 and summary["recent_activity"]

    assert (await client.get("/dashboard/summary?period=nonsense", headers=headers)).status_code == 422
    other = await signup("b@example.com", "Biz B")
    empty = (await client.get("/dashboard/summary", headers=other)).json()["by_currency"]["UGX"]
    assert empty["invoiced_cents"] == 0 and empty["outstanding_cents"] == 0

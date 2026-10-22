import os

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
# Keep tests on local disk even when .env points storage at Neon Object Storage.
os.environ["STORAGE_BACKEND"] = "local"


import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.main import app


@pytest_asyncio.fixture
async def db_engine(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine, tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path))

    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def outbox(monkeypatch):
    """Captures invoice/quotation/reminder messages instead of calling Gmail/Twilio."""
    import app.services.notifications as notifications_module

    sent: list[tuple[str, dict]] = []

    def fake_email(**kwargs):
        sent.append(("email", kwargs))
        return {"MessageID": f"gmail-{len(sent)}"}

    def fake_sms(**kwargs):
        sent.append(("sms", kwargs))
        return f"SM{len(sent)}"

    monkeypatch.setattr(notifications_module, "send_document_email", fake_email)
    monkeypatch.setattr(notifications_module, "send_text_sms", fake_sms)
    return sent


@pytest_asyncio.fixture
async def signup(client, monkeypatch):
    """Factory: registers a business + owner and returns auth headers."""
    monkeypatch.setattr("app.api.auth.send_verification_email", lambda **kwargs: {"MessageID": "fake"})

    async def _signup(email="owner@example.com", business="Acme Rentals"):
        await client.post(
            "/auth/signup",
            json={"business_name": business, "full_name": "Jane Landlord", "email": email, "password": "supersecret"},
        )
        login = await client.post("/auth/login", json={"email": email, "password": "supersecret"})
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    return _signup


@pytest_asyncio.fixture
async def make_invoice(client):
    """Factory: creates (and by default issues) an invoice; returns its JSON."""
    from datetime import date, timedelta

    async def _make(headers, *, issue=True, **overrides):
        body = {
            "customer_name": "Grace Tenant",
            "customer_email": "grace@example.com",
            "customer_phone": "+256701234567",
            "title": "Rent",
            "due_date": str(date.today() + timedelta(days=14)),
            "line_items": [{"description": "Rent", "quantity": "1", "unit_price_cents": 50000000}],
            "issue_now": issue,
        }
        body.update(overrides)
        resp = await client.post("/invoices", json=body, headers=headers)
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make


@pytest.fixture(autouse=True)
def _fast_email_retry(monkeypatch):
    import app.api.receipts as receipts_module
    import app.services.notifications as notifications_module

    monkeypatch.setattr(receipts_module, "EMAIL_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(notifications_module, "EMAIL_RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """The rate limiters are process-wide singletons; clear their state
    between tests so one test's requests don't trip another's limit."""
    from app.core.rate_limit import (
        auth_rate_limiter,
        public_doc_rate_limiter,
        public_response_rate_limiter,
        receipt_create_rate_limiter,
    )

    for limiter in (auth_rate_limiter, receipt_create_rate_limiter, public_doc_rate_limiter, public_response_rate_limiter):
        limiter._hits.clear()
    yield

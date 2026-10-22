from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.api import audit, auth, customers, invoices, public_docs, quotations, receipts, reminders, schedules, tenant, webhooks
from app.core.config import get_settings
from app.core.logging import RequestLogMiddleware, setup_logging
from app.db.seed import seed_default_users

settings = get_settings()
setup_logging(settings.log_level, settings.log_json)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.seed_default_users_enabled:
        await seed_default_users()
    scheduler = None
    if settings.scheduler_enabled:
        from app.services.scheduler import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Receipt Issuance App", version="1.0.0", lifespan=lifespan)

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_origin_regex=settings.cors_allow_origin_regex or None,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Request-ID"],
)



@app.get("/static/{key:path}", include_in_schema=False)
async def static_file(key: str):
    # Uploads and generated PDFs, served from app/services/storage.py (Neon Object Storage or local disk).
    data = await asyncio.to_thread(storage.get, key)
    if data is None:
        raise HTTPException(status_code=404, detail="Not found")
    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


app.include_router(auth.router)
app.include_router(tenant.router)
app.include_router(customers.router)
app.include_router(receipts.router)
app.include_router(receipts.public_router)
app.include_router(webhooks.router)
app.include_router(invoices.router)
app.include_router(quotations.router)
app.include_router(schedules.router)
app.include_router(reminders.router)
app.include_router(audit.router)
app.include_router(public_docs.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "status_code": exc.status_code}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "status_code": 500}},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

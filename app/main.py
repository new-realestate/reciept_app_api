from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, customers, receipts, tenant, webhooks
from app.core.config import get_settings
from app.db.seed import seed_default_users

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.seed_default_users_enabled:
        await seed_default_users()
    yield


app = FastAPI(title="Receipt Issuance App", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_origin_regex=settings.cors_allow_origin_regex or None,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=settings.storage_dir), name="static")

app.include_router(auth.router)
app.include_router(tenant.router)
app.include_router(customers.router)
app.include_router(receipts.router)
app.include_router(receipts.public_router)
app.include_router(webhooks.router)


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

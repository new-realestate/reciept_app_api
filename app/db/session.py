import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

connect_args: dict = {}
if settings.database_is_pooled:
    # Neon's pooled endpoint is PgBouncer in transaction mode: a statement prepared
    # on one server connection may not exist on the next, so asyncpg must not cache
    # prepared statements and must give each one a unique name.
    connect_args = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }

# Neon suspends idle compute after 5 minutes, which drops its connections: recycle them
# before that, and (by default) ping each one before use to catch any other drop.
engine = create_async_engine(
    settings.app_database_url,
    pool_size=5,
    max_overflow=5,
    pool_recycle=240,
    pool_pre_ping=settings.database_pool_pre_ping,
    connect_args=connect_args,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

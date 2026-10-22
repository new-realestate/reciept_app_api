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

# pool_pre_ping/pool_recycle: Neon scales idle compute to zero, which drops connections.
engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

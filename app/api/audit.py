"""Read-only browsing of the business's audit trail (Owner/Admin only)."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser
from app.core.permissions import require_manager
from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.documents import Page

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_name: str | None  # None for system or customer actions (e.g. a customer accepting a quotation)
    actor_email: str | None
    metadata: dict[str, Any] | None
    created_at: datetime


def _day_start(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


@router.get("", response_model=Page[AuditLogOut])
async def list_audit_logs(
    action: str | None = Query(default=None, description="Exact action, e.g. invoice.issued"),
    action_prefix: str | None = Query(default=None, description="Action family, e.g. invoice or quotation"),
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, description="Inclusive, in the business timezone"),
    date_to: date | None = Query(default=None, description="Inclusive, in the business timezone"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(require_manager),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. Always scoped to the caller's business."""
    tenant = (await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))).scalar_one()
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001 - unknown timezone falls back to UTC
        tz = ZoneInfo("UTC")

    conds = [AuditLog.tenant_id == tenant.id]
    if action:
        conds.append(AuditLog.action == action)
    if action_prefix:
        conds.append(or_(AuditLog.action == action_prefix, AuditLog.action.like(f"{action_prefix}.%")))
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if entity_id:
        conds.append(AuditLog.entity_id == entity_id)
    if actor_user_id:
        conds.append(AuditLog.actor_user_id == actor_user_id)
    if date_from:
        conds.append(AuditLog.created_at >= _day_start(date_from, tz))
    if date_to:
        conds.append(AuditLog.created_at < _day_start(date_to + timedelta(days=1), tz))

    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog, User.full_name, User.email)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .where(*conds)
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        AuditLogOut(
            id=log.id, action=log.action, entity_type=log.entity_type, entity_id=log.entity_id,
            actor_user_id=log.actor_user_id, actor_name=name, actor_email=str(email) if email else None,
            metadata=log.metadata_, created_at=log.created_at,
        )
        for log, name, email in rows
    ]
    return Page[AuditLogOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/actions", response_model=list[str])
async def list_audit_actions(current_user: CurrentUser = Depends(require_manager), db: AsyncSession = Depends(get_db)):
    """Distinct actions recorded for this business, for building filter menus."""
    rows = await db.execute(
        select(AuditLog.action).where(AuditLog.tenant_id == current_user.tenant_id).distinct().order_by(AuditLog.action)
    )
    return list(rows.scalars().all())

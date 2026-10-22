import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def record_audit_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_=metadata,
        )
    )

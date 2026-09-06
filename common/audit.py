from sqlalchemy.ext.asyncio import AsyncSession

from common.models import AuditLog


async def log_action(
    session: AsyncSession,
    actor: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    **metadata,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            event_metadata=metadata,
        )
    )

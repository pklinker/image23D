from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db import get_session
from common.models import ApiKey
from common.security import hash_api_key


async def require_api_key(request: Request, session: AsyncSession = Depends(get_session)) -> ApiKey:
    # EventSource (used for SSE progress) can't set custom headers, so the
    # events route is reachable via ?api_key=... too. Everything else uses
    # the Authorization header.
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
    token = token or request.query_params.get("api_key")

    if not token:
        raise HTTPException(401, "missing API key")

    key_hash = hash_api_key(token)
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()

    if api_key is None or api_key.revoked_at is not None:
        raise HTTPException(401, "invalid or revoked API key")

    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return api_key


async def require_admin_key(api_key: ApiKey = Depends(require_api_key)) -> ApiKey:
    """Key management is admin-only.

    Previously any valid key could mint or revoke any other, so a key handed to
    an integration could issue itself more, or revoke the ones it did not like.
    """
    if api_key.scope != "admin":
        raise HTTPException(403, "this endpoint requires an admin-scoped API key")
    return api_key

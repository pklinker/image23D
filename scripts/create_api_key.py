#!/usr/bin/env python3
"""Bootstrap the first API key by writing directly to Postgres.

Every /v1 route now requires a key, so there's no authenticated way to mint
the first one through the API itself. Requires the alembic migrations to
have already been applied (`alembic upgrade head`, or just start the api
container -- its entrypoint runs migrations before serving). Run from the
repo root with the venv/deps from api/requirements.txt installed, and
DATABASE_URL pointed at the host-exposed Postgres port (the default in .env
uses the "postgres" compose hostname, which only resolves inside the
compose network):

    DATABASE_URL=postgresql+asyncpg://image23d:image23d@localhost:5432/image23d \\
        PYTHONPATH=. python3 scripts/create_api_key.py --name "viewer"

Prints the plaintext key once. It is not recoverable after this -- only its
hash is stored.
"""
import argparse
import asyncio

from common.db import SessionLocal
from common.models import ApiKey
from common.security import generate_api_key, hash_api_key


async def main(name: str, scope: str) -> None:
    plaintext = generate_api_key()
    async with SessionLocal() as session:
        session.add(ApiKey(name=name, scope=scope, key_hash=hash_api_key(plaintext)))
        await session.commit()

    print(f"API key for {name!r} (scope={scope}; save this, it will not be shown again):")
    print(plaintext)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="label for this key, e.g. 'viewer' or 'cli'")
    parser.add_argument(
        "--scope",
        default="admin",
        choices=ApiKey.SCOPES,
        help=(
            "admin (default here, since this is the bootstrap path) can mint and "
            "revoke keys; service can only run jobs"
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.name, args.scope))

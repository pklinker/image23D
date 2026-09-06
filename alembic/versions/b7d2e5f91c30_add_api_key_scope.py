"""add api key scope

Revision ID: b7d2e5f91c30
Revises: 8b31c04d7a15
Create Date: 2026-09-06 19:15:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7d2e5f91c30'
down_revision: Union[str, Sequence[str], None] = '8b31c04d7a15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the scope column.

    New keys default to "service". Existing keys are backfilled to "admin":
    they were issued when any key could manage any other, and demoting them
    would lock whoever holds them out of key management with no way back --
    the bootstrap script writes directly to Postgres precisely because there
    is no unauthenticated path to mint one.
    """
    op.add_column(
        'api_keys',
        sa.Column('scope', sa.String(length=16), nullable=False, server_default='service'),
    )
    op.execute("UPDATE api_keys SET scope = 'admin'")


def downgrade() -> None:
    op.drop_column('api_keys', 'scope')

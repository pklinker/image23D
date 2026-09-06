"""add job run metrics: total_seconds, gpu_peak_mb

Revision ID: 8b31c04d7a15
Revises: 4f8c7a2773e0
Create Date: 2026-09-06 13:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8b31c04d7a15'
down_revision: Union[str, Sequence[str], None] = '4f8c7a2773e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Nullable so existing rows, which predate the measurement, stay valid."""
    op.add_column('jobs', sa.Column('total_seconds', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('gpu_peak_mb', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'gpu_peak_mb')
    op.drop_column('jobs', 'total_seconds')

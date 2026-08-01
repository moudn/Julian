"""org plan and period start

Revision ID: b8d4a2f6c9e1
Revises: c3e8f1a5d7b9
Create Date: 2026-08-01 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d4a2f6c9e1'
down_revision: Union[str, Sequence[str], None] = 'c3e8f1a5d7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=True))
    op.add_column('organizations', sa.Column('plan', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'plan')
    op.drop_column('organizations', 'current_period_start')

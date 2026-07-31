"""ai fit scoring

Revision ID: c3e8f1a5d7b9
Revises: f2a7c9e1b4d6
Create Date: 2026-07-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e8f1a5d7b9'
down_revision: Union[str, Sequence[str], None] = 'f2a7c9e1b4d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('ai_fit_scoring_enabled', sa.Boolean(),
                  nullable=False, server_default=sa.text('false')))
    op.add_column('organizations', sa.Column('ai_fit_weight', sa.Float(),
                  nullable=False, server_default='30.0'))
    op.add_column('leads', sa.Column('ai_fit_score', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('leads', 'ai_fit_score')
    op.drop_column('organizations', 'ai_fit_weight')
    op.drop_column('organizations', 'ai_fit_scoring_enabled')

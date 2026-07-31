"""org signature and step templates

Revision ID: f2a7c9e1b4d6
Revises: d4f6a8b1c2e3
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a7c9e1b4d6'
down_revision: Union[str, Sequence[str], None] = 'd4f6a8b1c2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('step_templates', sa.JSON(), nullable=True))
    op.add_column('organizations', sa.Column('email_signature_enabled', sa.Boolean(),
                  nullable=False, server_default=sa.text('false')))
    op.add_column('organizations', sa.Column('signature_title', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('signature_phone', sa.String(length=64), nullable=True))
    op.add_column('organizations', sa.Column('signature_website', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('logo_image', sa.LargeBinary(), nullable=True))
    op.add_column('organizations', sa.Column('logo_content_type', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'logo_content_type')
    op.drop_column('organizations', 'logo_image')
    op.drop_column('organizations', 'signature_website')
    op.drop_column('organizations', 'signature_phone')
    op.drop_column('organizations', 'signature_title')
    op.drop_column('organizations', 'email_signature_enabled')
    op.drop_column('organizations', 'step_templates')

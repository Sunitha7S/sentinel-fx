"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    # ... migration body ...
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    # ... migration body ...
    op.execute("RESET ROLE")

"""Private provider acceptance reference; no delivery or mailbox receipt claim."""

import sqlalchemy as sa
from alembic import op

revision = "0008_invitation_provider_message"
down_revision = "0007_inbox_check_setups"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("invitations", sa.Column("provider_message_id", sa.Text(), nullable=True))


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("invitations", "provider_message_id")

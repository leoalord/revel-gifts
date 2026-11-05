"""Nonexpiring signup invitations and joined attribution without connection creation."""

import sqlalchemy as sa
from alembic import op

revision = "0009_invitation_join"
down_revision = "0008_invitation_provider_message"
branch_labels = None
depends_on = None


NEW_STATUSES = "status IN ('pending','joined','accepted','declined','cancelled','expired')"
OLD_STATUSES = "status IN ('pending','accepted','declined','cancelled','expired')"


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column(
        "invitations", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True
    )
    # Preserve capabilities and all send metadata, including invitations already emailed.
    # Only pending invites become indefinite; terminal historical decisions are not revived.
    op.execute(
        "UPDATE invitations SET expires_at = NULL, updated_at = now() WHERE status = 'pending'"
    )
    op.drop_constraint(op.f("ck_invitations_status"), "invitations", type_="check")
    op.create_check_constraint(op.f("ck_invitations_status"), "invitations", NEW_STATUSES)


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM invitations WHERE expires_at IS NULL OR status = 'joined') THEN
            RAISE EXCEPTION 'Cannot downgrade 0009_invitation_join: nonexpiring or joined rows';
        END IF;
    END $$""")
    op.drop_constraint(op.f("ck_invitations_status"), "invitations", type_="check")
    op.create_check_constraint(op.f("ck_invitations_status"), "invitations", OLD_STATUSES)
    op.alter_column(
        "invitations", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )

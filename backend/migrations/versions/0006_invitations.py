"""0006_invitations; frozen DDL, no runtime model imports."""

from alembic import op

revision = "0006_invitations"
down_revision = "0005_connection_owner_confirmed"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
CREATE TABLE invitations (
    id UUID NOT NULL,
    sender_user_id UUID NOT NULL,
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    sealed_token TEXT NOT NULL,
    status TEXT DEFAULT 'pending' NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    delivery_status TEXT DEFAULT 'not_sent' NOT NULL,
    sent_at TIMESTAMP WITH TIME ZONE,
    responded_at TIMESTAMP WITH TIME ZONE,
    recipient_user_id UUID,
    connection_request_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_invitations PRIMARY KEY (id),
    CONSTRAINT ck_invitations_status CHECK (status IN
('pending','accepted','declined','cancelled','expired')),
    CONSTRAINT ck_invitations_delivery_status CHECK (delivery_status IN
('not_sent','sending','sent','unknown')),
    CONSTRAINT ck_invitations_positive_lifetime CHECK (expires_at > created_at),
    CONSTRAINT fk_invitations_sender_user_id_users FOREIGN KEY(sender_user_id) REFERENCES users
(id) ON DELETE RESTRICT,
    CONSTRAINT uq_invitations_token_hash UNIQUE (token_hash),
    CONSTRAINT fk_invitations_recipient_user_id_users FOREIGN KEY(recipient_user_id) REFERENCES
users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_invitations_connection_request_id_connection_requests FOREIGN
KEY(connection_request_id) REFERENCES connection_requests (id) ON DELETE RESTRICT
)

"""
    )
    op.execute(
        "CREATE INDEX ix_invitations_sender_created ON invitations (sender_user_id, created_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_invitations_pending_email ON invitations (sender_user_id, email) "
        "WHERE status = 'pending'"
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("invitations")

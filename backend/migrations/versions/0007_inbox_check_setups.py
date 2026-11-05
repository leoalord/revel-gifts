"""0007_inbox_check_setups; frozen DDL, no runtime model imports."""

from alembic import op

revision = "0007_inbox_check_setups"
down_revision = "0006_invitations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
CREATE TABLE inbox_check_setups (
    user_id UUID NOT NULL,
    delegation_id UUID,
    enabled BOOLEAN DEFAULT false NOT NULL,
    timezone TEXT,
    check_hours JSONB DEFAULT '[6,10,14,18]'::jsonb NOT NULL,
    host_task_id TEXT,
    schedule_revision INTEGER DEFAULT '0' NOT NULL,
    owner_confirmed_at TIMESTAMP WITH TIME ZONE,
    host_task_reported_at TIMESTAMP WITH TIME ZONE,
    last_successful_check_at TIMESTAMP WITH TIME ZONE,
    last_successful_check_delegation_id UUID,
    last_check_item_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_inbox_check_setups PRIMARY KEY (user_id),
    CONSTRAINT fk_inbox_setup_designated_delegation FOREIGN KEY(delegation_id, user_id)
REFERENCES agent_delegations (id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_inbox_setup_observed_delegation FOREIGN
KEY(last_successful_check_delegation_id, user_id) REFERENCES agent_delegations (id, user_id) ON
DELETE RESTRICT,
    CONSTRAINT ck_inbox_check_setups_schedule_revision CHECK (schedule_revision >= 0),
    CONSTRAINT ck_inbox_check_setups_check_item_count CHECK (last_check_item_count IS NULL OR
last_check_item_count >= 0),
    CONSTRAINT fk_inbox_check_setups_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON
DELETE RESTRICT
)

"""
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("inbox_check_setups")

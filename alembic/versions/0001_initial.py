"""Initial telemedicine schema.

Revision ID: 0001
Revises:
"""

from alembic import op
from app import models  # noqa: F401
from app.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_doctors_specialty_trgm ON doctors USING gin (specialty gin_trgm_ops)"
    )
    op.execute(
        "ALTER TABLE availability_slots ADD CONSTRAINT ex_doctor_slot_overlap "
        "EXCLUDE USING gist (doctor_id WITH =, tstzrange(starts_at, ends_at) WITH &&)"
    )


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())

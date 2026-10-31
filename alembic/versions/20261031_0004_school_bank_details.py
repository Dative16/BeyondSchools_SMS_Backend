"""Where a parent actually pays.

Payments are manual in Phase 1: there is no aggregator, no payment button and
no callback. A family pays at the office counter or into the school's bank
account, keeps the receipt, and the bursar records it against the child. That
makes the bank details the only payment instruction the system can give — and
the parent portal had nowhere to read them from.

Three nullable columns rather than a `bank_account` table, because a school
has one account for fees and the day it has two is the day it needs a table
with a purpose column, which is not today.

The account number is not a secret: it is printed on every invoice a school
sends and read out over the phone to anybody asking where to pay. It is stored
as text for the same reason — "2011 4409 118" is how it is written on a
deposit slip, and normalising the spaces out would make a parent check it
character by character against something that looks different.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("school", sa.Column("bank_name", sa.String(length=80), nullable=True))
    op.add_column(
        "school", sa.Column("bank_account_name", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "school", sa.Column("bank_account_number", sa.String(length=40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("school", "bank_account_number")
    op.drop_column("school", "bank_account_name")
    op.drop_column("school", "bank_name")

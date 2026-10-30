"""One live fee structure per level per year, enforced for real.

The initial schema put a unique constraint on
`(academic_year_id, level_id, stream_id)` and it did not do what it says.
PostgreSQL treats NULL as distinct in a unique index, and a level-wide
structure — which is nearly every structure a school writes — has a NULL
stream. So the constraint covered the stream-scoped case, the rare one, and
left the common one wide open.

What that costs, left alone: `published_items_by_level` keys the term run's
prices by `(level_id, stream_id)`, so two published level-wide structures for
one level merge into a single list of items and every child in that level is
billed the sum of both. Nothing in the run looks wrong; the invoices are just
too big, for a whole level, and the structure screen shows two rows that each
look right on their own.

Replaced with two partial unique indexes that between them say the intended
thing, and that also exclude archived rows so a discarded draft does not block
the structure written to replace it.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Any existing duplicate has to go before the index can exist. Archived
    # rather than deleted, and the published one is the row that stays: a
    # published structure may already have invoices raised from it, while a
    # draft has billed nobody. Where two are the same status the older one
    # stays, because the newer is the accident.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY academic_year_id, level_id, stream_id
                       ORDER BY (status = 'published') DESC, created_at
                   ) AS rank
              FROM fee_structure
             WHERE status <> 'archived'
        )
        UPDATE fee_structure
           SET status = 'archived', updated_at = now()
         WHERE id IN (SELECT id FROM ranked WHERE rank > 1)
        """
    )

    op.drop_constraint(
        "uq_fee_structure_academic_year_id_level_id_stream_id",
        "fee_structure",
        type_="unique",
    )

    # The whole level. This is the index the old constraint should have been.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_fee_structure_level_wide
            ON fee_structure (academic_year_id, level_id)
         WHERE stream_id IS NULL AND status <> 'archived'
        """
    )
    # One stream of it, charged differently — boarders, usually.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_fee_structure_per_stream
            ON fee_structure (academic_year_id, level_id, stream_id)
         WHERE stream_id IS NOT NULL AND status <> 'archived'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_fee_structure_per_stream")
    op.execute("DROP INDEX IF EXISTS uq_fee_structure_level_wide")
    op.create_unique_constraint(
        "uq_fee_structure_academic_year_id_level_id_stream_id",
        "fee_structure",
        ["academic_year_id", "level_id", "stream_id"],
    )

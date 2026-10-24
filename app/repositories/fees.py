"""Fee structure and invoice persistence. SQLAlchemy lives here and nowhere
above it.

What this file does *not* do is set `invoice.billed` or `invoice.settled`.
Those are maintained by database triggers from the lines and the payments, so
the only way to change what a child owes is to write a line or record a
payment. A repository method that assigned a total would be silently discarded
by the trigger, which is the design working — see the initial migration.

Phase 1.5 needs the deposit invoice an offer raises. Term invoice runs,
statements and arrears reporting land with the rest of fees in 1.6, and build
on the same two methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.invoicing import DraftLine, FeeCategory, FeeFrequency, FeeItem
from app.models.fees import Invoice, InvoiceLine


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    invoice_id: UUID
    reference: str
    billed: Decimal
    settled: Decimal
    is_void: bool
    due_date: date

    @property
    def balance(self) -> Decimal:
        return max(self.billed - self.settled, Decimal(0))

    @property
    def is_settled(self) -> bool:
        return not self.is_void and self.balance == Decimal(0) and self.billed > Decimal(0)


class FeesRepository(Protocol):
    async def published_items(
        self, *, academic_year_id: UUID, level_id: UUID, stream_id: UUID | None
    ) -> list[FeeItem]: ...
    async def create_invoice(self, **values: Any) -> Invoice: ...
    async def add_lines(
        self, *, invoice_id: UUID, school_id: UUID, lines: list[DraftLine]
    ) -> None: ...
    async def totals(self, invoice_id: UUID) -> InvoiceTotals | None: ...
    async def next_invoice_sequence(self, *, school_id: UUID, scope: str) -> int: ...
    async def structure_is_published(
        self, *, academic_year_id: UUID, level_id: UUID
    ) -> bool: ...
    async def invoice(self, invoice_id: UUID) -> Invoice | None: ...


class SqlFeesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def published_items(
        self, *, academic_year_id: UUID, level_id: UUID, stream_id: UUID | None
    ) -> list[FeeItem]:
        """The fee items a child at this level pays, from the published structure.

        Only published: nothing can be invoiced from a draft, which is why the
        dashboard warns when a level's structure is still unpublished in week
        four of term.

        A structure scoped to the child's stream wins over the level-wide one,
        because that is how a school expresses "boarders pay more" without
        writing two whole structures.
        """
        rows = await self._session.execute(
            text(
                "SELECT i.id, i.name, i.amount, i.category::text AS category, "
                "       i.frequency::text AS frequency, i.is_mandatory, i.term_index "
                "  FROM fee_item i "
                "  JOIN fee_structure s ON s.id = i.fee_structure_id "
                " WHERE s.academic_year_id = :year AND s.level_id = :level "
                "   AND s.status = 'published' "
                "   AND (s.stream_id = CAST(:stream AS uuid) OR s.stream_id IS NULL) "
                " ORDER BY (s.stream_id IS NULL), i.sort_order, i.name"
            ),
            {"year": academic_year_id, "level": level_id, "stream": stream_id},
        )

        # A stream-scoped structure shadows the level-wide one item for item,
        # so the first row for each name wins — the ORDER BY above puts the
        # stream's own rows first.
        chosen: dict[str, FeeItem] = {}
        for row in rows:
            if row.name in chosen:
                continue
            chosen[row.name] = FeeItem(
                fee_item_id=row.id,
                name=row.name,
                amount=Decimal(row.amount),
                category=FeeCategory(row.category),
                frequency=FeeFrequency(row.frequency),
                is_mandatory=row.is_mandatory,
                term_index=row.term_index,
            )
        return list(chosen.values())

    async def structure_is_published(
        self, *, academic_year_id: UUID, level_id: UUID
    ) -> bool:
        found = await self._session.scalar(
            text(
                "SELECT 1 FROM fee_structure "
                " WHERE academic_year_id = :year AND level_id = :level "
                "   AND status = 'published' LIMIT 1"
            ),
            {"year": academic_year_id, "level": level_id},
        )
        return found is not None

    async def next_invoice_sequence(self, *, school_id: UUID, scope: str) -> int:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO school_sequence (id, school_id, name, scope, value) "
                    "VALUES (gen_random_uuid(), :school_id, 'invoice_ref', :scope, 1) "
                    "ON CONFLICT (school_id, name, scope) "
                    "DO UPDATE SET value = school_sequence.value + 1 "
                    "RETURNING value"
                ),
                {"school_id": school_id, "scope": scope},
            )
        ).one()
        return int(row.value)

    async def create_invoice(self, **values: Any) -> Invoice:
        invoice = Invoice(**values)
        self._session.add(invoice)
        await self._session.flush()
        return invoice

    async def add_lines(
        self, *, invoice_id: UUID, school_id: UUID, lines: list[DraftLine]
    ) -> None:
        """Write the lines. The trigger recomputes the total.

        Nothing here touches `billed`: a repository that assigned it would
        have its value discarded by the guard trigger, which is the point.
        """
        for order, line in enumerate(lines):
            self._session.add(
                InvoiceLine(
                    school_id=school_id,
                    invoice_id=invoice_id,
                    item=line.item,
                    amount=line.amount,
                    kind=line.kind,
                    fee_item_id=line.fee_item_id,
                    sort_order=order,
                )
            )
        await self._session.flush()

    async def totals(self, invoice_id: UUID) -> InvoiceTotals | None:
        row = (
            await self._session.execute(
                select(
                    Invoice.id,
                    Invoice.reference,
                    Invoice.billed,
                    Invoice.settled,
                    Invoice.is_void,
                    Invoice.due_date,
                ).where(Invoice.id == invoice_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return InvoiceTotals(
            invoice_id=row.id,
            reference=row.reference,
            billed=Decimal(row.billed),
            settled=Decimal(row.settled),
            is_void=row.is_void,
            due_date=row.due_date,
        )

    async def invoice(self, invoice_id: UUID) -> Invoice | None:
        return await self._session.get(Invoice, invoice_id)

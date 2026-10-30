"""Fee structure and invoice persistence. SQLAlchemy lives here and nowhere
above it.

What this file does *not* do is set `invoice.billed` or `invoice.settled`.
Those are maintained by database triggers from the lines and the payments, so
the only way to change what a child owes is to write a line or record a
payment. A repository method that assigned a total would be silently discarded
by the trigger, which is the design working — see the initial migration.

Everything a school charges, bills and chases runs through here: the
structures, the term run, statements, arrears and the two corrections. The
corrections are worth the attention — `void` for a bill that should never have
been raised, `add_credit_note` for one that was right at the time. Neither
rewrites a charge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.invoicing import (
    Discount,
    DraftLine,
    FeeCategory,
    FeeFrequency,
    FeeItem,
    FeeStructureStatus,
    InvoiceLineKind,
)
from app.models.fees import FeeItem as FeeItemRow
from app.models.fees import FeeStructure, Invoice, InvoiceLine

#: The children one fee structure covers, as a scalar subquery over `s`.
#:
#: Written once and used twice — in the list and on its own — because the two
#: must agree. It mirrors the term run's choice of structure exactly: a
#: structure scoped to a stream covers only that stream, and a level-wide one
#: covers the whole level, every stream in it included.
_ENROLLED_UNDER = (
    "SELECT count(*) FROM enrolment e "
    "  JOIN class_group g ON g.id = e.class_group_id "
    " WHERE e.academic_year_id = s.academic_year_id "
    "   AND g.level_id = s.level_id "
    "   AND (s.stream_id IS NULL OR g.stream_id = s.stream_id) "
    "   AND e.outcome = 'active'"
)


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
    async def term(self, term_id: UUID) -> dict[str, Any] | None: ...

    # --- fee structures ---------------------------------------------------
    async def structures(
        self, *, academic_year_id: UUID | None
    ) -> list[dict[str, Any]]: ...
    async def structure(self, structure_id: UUID) -> FeeStructure | None: ...
    async def enrolled_under(self, structure_id: UUID) -> int: ...
    async def live_structure_for(
        self, *, academic_year_id: UUID, level_id: UUID, stream_id: UUID | None
    ) -> FeeStructure | None: ...
    async def add_structure(self, **values: Any) -> FeeStructure: ...
    async def items_of(self, structure_id: UUID) -> list[dict[str, Any]]: ...
    async def replace_items(
        self, *, structure_id: UUID, school_id: UUID, items: list[dict[str, Any]]
    ) -> None: ...

    # --- the term run -----------------------------------------------------
    async def enrolments_to_invoice(
        self, academic_year_id: UUID
    ) -> list[dict[str, Any]]: ...
    async def enrolments_already_invoiced(self, term_id: UUID) -> frozenset[UUID]: ...
    async def discounts_for(self, *, as_of: date) -> dict[UUID, list[Discount]]: ...
    async def published_items_by_level(
        self, academic_year_id: UUID
    ) -> dict[tuple[UUID, UUID | None], list[FeeItem]]: ...

    # --- reading ----------------------------------------------------------
    async def invoices(
        self,
        *,
        term_id: UUID | None,
        class_group_id: UUID | None,
        student_id: UUID | None,
        unpaid_only: bool,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]: ...
    async def lines(self, invoice_id: UUID) -> list[dict[str, Any]]: ...
    async def payments_for(self, invoice_id: UUID) -> list[dict[str, Any]]: ...
    async def payment(self, payment_id: UUID) -> dict[str, Any] | None: ...
    async def recent_payments(self, *, limit: int) -> list[dict[str, Any]]: ...
    async def invoice_subject(self, invoice_id: UUID) -> dict[str, Any] | None: ...
    async def statement(self, student_id: UUID) -> list[dict[str, Any]]: ...
    async def arrears(
        self, *, as_of: date, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]: ...
    async def collection_totals(
        self, academic_year_id: UUID | None, *, as_of: date
    ) -> dict[str, Decimal]: ...

    # --- corrections ------------------------------------------------------
    async def void(
        self, invoice_id: UUID, *, reason: str, by_id: UUID | None
    ) -> None: ...
    async def add_credit_note(
        self,
        *,
        invoice_id: UUID,
        school_id: UUID,
        amount: Decimal,
        note: str,
        by_id: UUID | None,
    ) -> UUID: ...
    async def match_payment(
        self, *, payment_id: UUID, invoice_id: UUID, applied: Decimal, by_id: UUID | None
    ) -> None: ...


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

    async def term(self, term_id: UUID) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, academic_year_id, index, label, starts_on, ends_on, "
                    "       is_current "
                    "  FROM term WHERE id = :id"
                ),
                {"id": term_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    # --- fee structures ---------------------------------------------------
    async def structures(self, *, academic_year_id: UUID | None) -> list[dict[str, Any]]:
        """Every structure for a year, with what it holds and whether it is live.

        The count and total come back with the row because the settings screen
        shows them: a structure with no items is the commonest reason a term
        run finds nothing to bill.

        So does the headcount, and that one is here rather than worked out on
        the screen because publishing cannot be undone. "This will apply to 38
        children" is the sentence that stops a bursar publishing Standard 6's
        prices against Standard 1.
        """
        rows = await self._session.execute(
            # The only thing concatenated in is a literal defined in this
            # module; nothing from a request reaches the SQL, and the year is
            # a bound parameter as it must be. Hence the suppression.
            text(
                "SELECT s.id, s.academic_year_id, s.level_id, l.name AS level_name, "  # noqa: S608
                "       s.stream_id, st.name AS stream_name, s.label, "
                "       s.status::text AS status, s.published_at, s.note, "
                "       count(i.id) AS item_count, "
                "       coalesce(sum(i.amount), 0) AS total_amount, "
                "       (" + _ENROLLED_UNDER + ") AS enrolled_count "
                "  FROM fee_structure s "
                "  JOIN level l ON l.id = s.level_id "
                "  LEFT JOIN stream st ON st.id = s.stream_id "
                "  LEFT JOIN fee_item i ON i.fee_structure_id = s.id "
                # A discarded draft is gone as far as this list is
                # concerned. The list's job is to show which levels are not
                # ready to bill, and a draft somebody abandoned in October is
                # not one of them.
                " WHERE s.status <> 'archived' "
                "   AND (CAST(:year AS uuid) IS NULL "
                "        OR s.academic_year_id = CAST(:year AS uuid)) "
                " GROUP BY s.id, s.academic_year_id, s.level_id, l.name, s.stream_id, "
                "          st.name, s.label, s.status, s.published_at, s.note, "
                "          l.order_index "
                " ORDER BY l.order_index, st.name NULLS FIRST"
            ),
            {"year": academic_year_id},
        )
        return [dict(row._mapping) for row in rows]

    async def structure(self, structure_id: UUID) -> FeeStructure | None:
        return await self._session.get(FeeStructure, structure_id)

    async def live_structure_for(
        self, *, academic_year_id: UUID, level_id: UUID, stream_id: UUID | None
    ) -> FeeStructure | None:
        """The structure already pricing this scope, draft or published.

        What the partial unique indexes forbid a second of. Asked before one
        is created, so the answer is a sentence rather than an integrity error
        surfacing as a 500.
        """
        return (
            await self._session.scalars(
                select(FeeStructure)
                .where(FeeStructure.academic_year_id == academic_year_id)
                .where(FeeStructure.level_id == level_id)
                .where(FeeStructure.stream_id == stream_id)
                .where(FeeStructure.status != FeeStructureStatus.ARCHIVED)
                .limit(1)
            )
        ).first()

    async def enrolled_under(self, structure_id: UUID) -> int:
        """How many children this structure would bill, were it published.

        Counted the way the term run chooses a structure — the level, the
        year, and the stream if the structure is scoped to one — so the figure
        on the publish button is the figure the run will act on rather than a
        second opinion about it.
        """
        count = await self._session.scalar(
            text(
                "SELECT (" + _ENROLLED_UNDER + ") "  # noqa: S608 - module literal, :id bound
                "  FROM fee_structure s WHERE s.id = :id"
            ),
            {"id": structure_id},
        )
        return int(count or 0)

    async def add_structure(self, **values: Any) -> FeeStructure:
        structure = FeeStructure(**values)
        self._session.add(structure)
        await self._session.flush()
        return structure

    async def items_of(self, structure_id: UUID) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            text(
                "SELECT id, name, amount, category::text AS category, "
                "       frequency::text AS frequency, term_index, is_mandatory, sort_order "
                "  FROM fee_item WHERE fee_structure_id = :id "
                " ORDER BY sort_order, name"
            ),
            {"id": structure_id},
        )
        return [dict(row._mapping) for row in rows]

    async def replace_items(
        self, *, structure_id: UUID, school_id: UUID, items: list[dict[str, Any]]
    ) -> None:
        """Replace a draft structure's items.

        A whole set, because the screen shows all of them and the total is the
        thing a school checks. Only ever called on a draft — a published
        structure is frozen, so invoices already raised against it cannot
        change retrospectively.
        """
        await self._session.execute(
            text("DELETE FROM fee_item WHERE fee_structure_id = :id"),
            {"id": structure_id},
        )
        for order, item in enumerate(items):
            self._session.add(
                FeeItemRow(
                    school_id=school_id,
                    fee_structure_id=structure_id,
                    sort_order=order,
                    **item,
                )
            )
        await self._session.flush()

    # --- the term run -----------------------------------------------------
    async def enrolments_to_invoice(self, academic_year_id: UUID) -> list[dict[str, Any]]:
        """Every child a term invoice should be raised for.

        Active enrolments only. A child who transferred out in week two is not
        billed for the term, and one who repeated has exactly one open
        enrolment — the partial unique index guarantees that, which is why
        this query does not have to worry about it.
        """
        rows = await self._session.execute(
            text(
                "SELECT e.id AS enrolment_id, e.student_id, cg.level_id, cg.stream_id, "
                "       cg.name AS class_name, s.admission_no, "
                "       s.first_name || ' ' || s.surname AS student_name, "
                "       s.arrears_brought_forward "
                "  FROM enrolment e "
                "  JOIN class_group cg ON cg.id = e.class_group_id "
                "  JOIN student s ON s.id = e.student_id "
                " WHERE e.academic_year_id = :year AND e.outcome = 'active' "
                " ORDER BY cg.name, s.surname, s.first_name"
            ),
            {"year": academic_year_id},
        )
        return [dict(row._mapping) for row in rows]

    async def enrolments_already_invoiced(self, term_id: UUID) -> frozenset[UUID]:
        """Who already has a bill for this term.

        What makes the run repeatable. A run over 500 children will be
        interrupted sooner or later, and running it again has to be the safe
        recovery rather than the thing that double-bills a school.
        """
        rows = await self._session.execute(
            text(
                "SELECT DISTINCT enrolment_id FROM invoice "
                " WHERE term_id = :term AND kind = 'term' AND enrolment_id IS NOT NULL"
            ),
            {"term": term_id},
        )
        return frozenset(row.enrolment_id for row in rows)

    async def discounts_for(self, *, as_of: date) -> dict[UUID, list[Discount]]:
        """Standing discounts, by student, that apply on a given day.

        Fetched for the whole school in one query rather than per child: a run
        over 500 children would otherwise be 500 extra round trips.
        """
        rows = await self._session.execute(
            text(
                "SELECT student_id, label, amount, percent, category::text AS category, "
                "       starts_on, ends_on "
                "  FROM fee_discount "
                " WHERE starts_on <= :as_of AND (ends_on IS NULL OR ends_on >= :as_of)"
            ),
            {"as_of": as_of},
        )
        found: dict[UUID, list[Discount]] = {}
        for row in rows:
            found.setdefault(row.student_id, []).append(
                Discount(
                    label=row.label,
                    amount=Decimal(row.amount) if row.amount is not None else None,
                    percent=row.percent,
                    category=FeeCategory(row.category) if row.category else None,
                    starts_on=row.starts_on,
                    ends_on=row.ends_on,
                )
            )
        return found

    async def published_items_by_level(
        self, academic_year_id: UUID
    ) -> dict[tuple[UUID, UUID | None], list[FeeItem]]:
        """Every published fee structure for the year, keyed by level and stream.

        One query for the whole run. Looking each level up as the run walks the
        roll would be the same handful of structures fetched hundreds of times.
        """
        rows = await self._session.execute(
            text(
                "SELECT s.level_id, s.stream_id, i.id, i.name, i.amount, "
                "       i.category::text AS category, i.frequency::text AS frequency, "
                "       i.is_mandatory, i.term_index, i.sort_order "
                "  FROM fee_item i "
                "  JOIN fee_structure s ON s.id = i.fee_structure_id "
                " WHERE s.academic_year_id = :year AND s.status = 'published' "
                " ORDER BY s.level_id, s.stream_id NULLS FIRST, i.sort_order, i.name"
            ),
            {"year": academic_year_id},
        )

        by_scope: dict[tuple[UUID, UUID | None], list[FeeItem]] = {}
        for row in rows:
            by_scope.setdefault((row.level_id, row.stream_id), []).append(
                FeeItem(
                    fee_item_id=row.id,
                    name=row.name,
                    amount=Decimal(row.amount),
                    category=FeeCategory(row.category),
                    frequency=FeeFrequency(row.frequency),
                    is_mandatory=row.is_mandatory,
                    term_index=row.term_index,
                )
            )
        return by_scope

    # --- reading ----------------------------------------------------------
    async def invoices(
        self,
        *,
        term_id: UUID | None,
        class_group_id: UUID | None,
        student_id: UUID | None,
        unpaid_only: bool,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = ["NOT i.is_void"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if term_id:
            clauses.append("i.term_id = :term_id")
            params["term_id"] = term_id
        if class_group_id:
            clauses.append("e.class_group_id = :class_group_id")
            params["class_group_id"] = class_group_id
        if student_id:
            clauses.append("i.student_id = :student_id")
            params["student_id"] = student_id
        if unpaid_only:
            clauses.append("i.billed > i.settled")
        if q:
            clauses.append(
                "(i.reference ILIKE :q OR s.first_name ILIKE :q "
                " OR s.surname ILIKE :q OR s.admission_no ILIKE :q)"
            )
            params["q"] = f"%{q.strip()}%"

        where = f"WHERE {' AND '.join(clauses)}"
        source = (
            " FROM invoice i "
            " LEFT JOIN student s ON s.id = i.student_id "
            " LEFT JOIN enrolment e ON e.id = i.enrolment_id "
            " LEFT JOIN class_group cg ON cg.id = e.class_group_id "
            " LEFT JOIN term t ON t.id = i.term_id "
        )

        total = await self._session.scalar(
            text(f"SELECT count(*) {source} {where}"),  # noqa: S608
            params,
        )
        rows = await self._session.execute(
            text(  # noqa: S608
                "SELECT i.id, i.reference, i.kind::text AS kind, i.student_id, "
                "       s.admission_no, "
                "       s.first_name || ' ' || s.surname AS student_name, "
                "       cg.name AS class_name, t.label AS term_label, "
                "       i.issued_on, i.due_date, i.billed, i.settled, i.is_void, "
                "       i.notified_at "
                f"{source} {where} "
                " ORDER BY i.due_date, s.surname NULLS LAST "
                " LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        return [dict(row._mapping) for row in rows], int(total or 0)

    async def lines(self, invoice_id: UUID) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            text(
                "SELECT id, item, amount, kind::text AS kind, note, sort_order "
                "  FROM invoice_line WHERE invoice_id = :id "
                " ORDER BY sort_order, item"
            ),
            {"id": invoice_id},
        )
        return [dict(row._mapping) for row in rows]

    async def payments_for(self, invoice_id: UUID) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            text(
                "SELECT p.id, p.external_ref, p.amount, p.applied, "
                "       p.channel::text AS channel, p.state::text AS state, "
                "       p.msisdn, p.received_at, p.settled_on, p.is_manual, "
                "       p.reversed_at, u.full_name AS recorded_by "
                "  FROM payment p "
                "  LEFT JOIN app_user u ON u.id = p.recorded_by_id "
                " WHERE p.invoice_id = :id "
                " ORDER BY p.received_at"
            ),
            {"id": invoice_id},
        )
        return [dict(row._mapping) for row in rows]

    async def statement(self, student_id: UUID) -> list[dict[str, Any]]:
        """Every bill a child has ever had, oldest first.

        Across years, because arrears follow the child: a statement that
        started at the current year would not explain the balance on it.
        """
        rows = await self._session.execute(
            text(
                "SELECT i.id, i.reference, i.kind::text AS kind, y.label AS academic_year, "
                "       t.label AS term_label, i.issued_on, i.due_date, "
                "       i.billed, i.settled, i.is_void "
                "  FROM invoice i "
                "  LEFT JOIN term t ON t.id = i.term_id "
                "  LEFT JOIN academic_year y ON y.id = t.academic_year_id "
                " WHERE i.student_id = :student_id "
                " ORDER BY i.issued_on, i.reference"
            ),
            {"student_id": student_id},
        )
        return [dict(row._mapping) for row in rows]

    async def arrears(
        self, *, as_of: date, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Who owes what, and for how long.

        One row per child rather than per invoice: a bursar chasing arrears
        rings a family, not a document, and three overdue bills for one child
        is one phone call.
        """
        source = (
            " FROM invoice i "
            " JOIN student s ON s.id = i.student_id "
            " LEFT JOIN enrolment e ON e.id = i.enrolment_id "
            " LEFT JOIN class_group cg ON cg.id = e.class_group_id "
            " LEFT JOIN guardian_student gs ON gs.student_id = s.id AND gs.is_primary "
            " LEFT JOIN guardian g ON g.id = gs.guardian_id "
            " WHERE NOT i.is_void AND i.billed > i.settled AND i.due_date < :as_of "
        )
        params: dict[str, Any] = {"as_of": as_of, "limit": limit, "offset": offset}

        total = await self._session.scalar(
            text(f"SELECT count(DISTINCT s.id) {source}"),  # noqa: S608
            params,
        )
        rows = await self._session.execute(
            text(  # noqa: S608
                "SELECT s.id AS student_id, s.admission_no, "
                "       s.first_name || ' ' || s.surname AS student_name, "
                "       cg.name AS class_name, g.full_name AS guardian_name, "
                "       g.phone AS guardian_phone, "
                "       sum(i.billed - i.settled) AS balance, "
                "       max((:as_of)::date - i.due_date) AS days_overdue, "
                "       count(*) AS overdue_invoices "
                f"{source} "
                " GROUP BY s.id, s.admission_no, student_name, cg.name, "
                "          g.full_name, g.phone "
                " ORDER BY days_overdue DESC, balance DESC "
                " LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        return [dict(row._mapping) for row in rows], int(total or 0)

    async def collection_totals(
        self, academic_year_id: UUID | None, *, as_of: date
    ) -> dict[str, Decimal]:
        """The dashboard's two money tiles, and the arrears counts beneath them.

        The over-60 count comes back with the rest rather than being worked out
        from a page of the arrears list: a dashboard that fetched a hundred rows
        to count nine of them would be slower than the screen it sits on, and
        would quietly undercount as soon as the list paginated.

        `as_of` is an argument for the same reason it is one on `arrears`: a
        figure read against "whenever the query ran" cannot be reconciled with
        a report, and cannot be tested at all.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT coalesce(sum(i.billed), 0) AS billed, "
                    "       coalesce(sum(i.settled), 0) AS settled, "
                    "       coalesce(sum(CASE WHEN i.due_date < CAST(:as_of AS date) "
                    "                         THEN i.billed - i.settled ELSE 0 END), 0) "
                    "         AS overdue, "
                    "       count(DISTINCT CASE WHEN i.billed > i.settled "
                    "                           AND i.due_date < CAST(:as_of AS date) "
                    "                      THEN i.student_id END) AS students_in_arrears, "
                    "       count(DISTINCT CASE WHEN i.billed > i.settled "
                    "                           AND i.due_date < CAST(:as_of AS date) - 60 "
                    "                      THEN i.student_id END) AS students_over_60_days "
                    "  FROM invoice i "
                    "  LEFT JOIN term t ON t.id = i.term_id "
                    " WHERE NOT i.is_void "
                    "   AND (CAST(:year AS uuid) IS NULL "
                    "        OR t.academic_year_id = CAST(:year AS uuid))"
                ),
                {"year": academic_year_id, "as_of": as_of},
            )
        ).one()
        return {
            "billed": Decimal(row.billed),
            "settled": Decimal(row.settled),
            "overdue": Decimal(row.overdue),
            "students_in_arrears": row.students_in_arrears,
            "students_over_60_days": row.students_over_60_days,
        }

    # --- corrections ------------------------------------------------------
    async def void(self, invoice_id: UUID, *, reason: str, by_id: UUID | None) -> None:
        """Voiding rather than deleting.

        A bill a parent has already seen has to stay visible, with a reason,
        or the next statement makes no sense to them.
        """
        await self._session.execute(
            text(
                "UPDATE invoice SET is_void = true, void_reason = :reason, "
                "       voided_at = now(), voided_by_id = :by_id "
                " WHERE id = :id"
            ),
            {"id": invoice_id, "reason": reason, "by_id": by_id},
        )

    async def add_credit_note(
        self, *, invoice_id: UUID, school_id: UUID, amount: Decimal, note: str, by_id: UUID | None
    ) -> UUID:
        """How a mistake is corrected: another line, not an edit.

        The original charge stays on the bill. A parent can see what they were
        charged and what was taken off, and the trigger moves the total.
        """
        line = InvoiceLine(
            school_id=school_id,
            invoice_id=invoice_id,
            item=note,
            amount=amount,
            kind=InvoiceLineKind.CREDIT_NOTE,
            created_by_id=by_id,
            sort_order=99,
        )
        self._session.add(line)
        await self._session.flush()
        return line.id

    async def match_payment(
        self, *, payment_id: UUID, invoice_id: UUID, applied: Decimal, by_id: UUID | None
    ) -> None:
        """Attach a payment that arrived with nothing to match it.

        The bursar decides which invoice; the system records who decided.
        """
        await self._session.execute(
            text(
                "UPDATE payment SET invoice_id = :invoice_id, applied = :applied, "
                "       matched_at = now(), matched_by_id = :by_id, "
                "       reason = NULL "
                " WHERE id = :payment_id"
            ),
            {
                "payment_id": payment_id,
                "invoice_id": invoice_id,
                "applied": applied,
                "by_id": by_id,
            },
        )

    async def payment(self, payment_id: UUID) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, invoice_id, amount, applied, state::text AS state, "
                    "       external_ref, msisdn, received_at, reversed_at "
                    "  FROM payment WHERE id = :id"
                ),
                {"id": payment_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def recent_payments(self, *, limit: int) -> list[dict[str, Any]]:
        """The money that came in, newest first — the dashboard's second card.

        Joined out to the child and the class because that is what makes the
        row readable: a bursar recognises "Neema Mwanjala, Standard 4 Blue"
        and recognises nothing at all about an invoice reference.

        Reversed payments are left out. A reversal is not a receipt, and a
        feed that showed one would have a bursar chasing a payment that was
        already undone.
        """
        rows = await self._session.execute(
            text(
                "SELECT p.id, p.amount, p.applied, p.channel::text AS channel, "
                "       p.external_ref, p.received_at, p.is_manual, "
                "       i.id AS invoice_id, i.reference, "
                "       s.id AS student_id, "
                "       trim(coalesce(s.first_name, '') || ' ' || "
                "            coalesce(s.surname, '')) AS student_name, "
                "       cg.name AS class_name "
                "  FROM payment p "
                "  LEFT JOIN invoice i ON i.id = p.invoice_id "
                "  LEFT JOIN student s ON s.id = i.student_id "
                "  LEFT JOIN enrolment e ON e.id = i.enrolment_id "
                "  LEFT JOIN class_group cg ON cg.id = e.class_group_id "
                " WHERE p.state = 'settled' AND p.reversed_at IS NULL "
                " ORDER BY p.received_at DESC "
                " LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(row._mapping) for row in rows]

    async def invoice_subject(self, invoice_id: UUID) -> dict[str, Any] | None:
        """Who the bill is for, and which term it belongs to.

        The detail response carried neither, so the header could only say
        "INV/Term1/0094" — and the screen had to go and fetch the child
        separately, which is a second request and a second moment: the name
        and the balance could be read from different seconds of the term run.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT s.admission_no, "
                    "       trim(coalesce(s.first_name, '') || ' ' || "
                    "            coalesce(s.surname, '')) AS student_name, "
                    "       cg.name AS class_name, t.label AS term_label, "
                    "       y.label AS academic_year "
                    "  FROM invoice i "
                    "  LEFT JOIN student s ON s.id = i.student_id "
                    "  LEFT JOIN enrolment e ON e.id = i.enrolment_id "
                    "  LEFT JOIN class_group cg ON cg.id = e.class_group_id "
                    "  LEFT JOIN term t ON t.id = i.term_id "
                    "  LEFT JOIN academic_year y ON y.id = t.academic_year_id "
                    " WHERE i.id = :id"
                ),
                {"id": invoice_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

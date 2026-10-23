"""Payment and invoice persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.payments import InvoiceFacts


class PaymentRepository(Protocol):
    """What the payment service needs from storage. Nothing more."""

    async def external_ref_exists(self, school_id: UUID, external_ref: str) -> bool: ...

    async def invoice_facts(self, invoice_id: UUID) -> InvoiceFacts | None: ...

    async def find_invoice_by_reference(
        self, school_id: UUID, reference: str
    ) -> InvoiceFacts | None: ...

    async def find_open_invoice_by_msisdn(
        self, school_id: UUID, msisdn: str, amount: Decimal
    ) -> InvoiceFacts | None: ...

    async def record_payment(
        self,
        *,
        school_id: UUID,
        invoice_id: UUID | None,
        external_ref: str,
        amount: Decimal,
        applied: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
    ) -> UUID: ...

    async def record_suspense(
        self,
        *,
        school_id: UUID,
        external_ref: str,
        amount: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        reason: str,
    ) -> UUID: ...


class SqlPaymentRepository:
    """SQLAlchemy implementation. The only file in the payment path that
    writes SQL — if you find yourself reaching for `select()` in a service or a
    route, the query belongs here instead."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def external_ref_exists(self, school_id: UUID, external_ref: str) -> bool:
        raise NotImplementedError

    async def invoice_facts(self, invoice_id: UUID) -> InvoiceFacts | None:
        raise NotImplementedError

    async def find_invoice_by_reference(
        self, school_id: UUID, reference: str
    ) -> InvoiceFacts | None:
        raise NotImplementedError

    async def find_open_invoice_by_msisdn(
        self, school_id: UUID, msisdn: str, amount: Decimal
    ) -> InvoiceFacts | None:
        raise NotImplementedError

    async def record_payment(
        self,
        *,
        school_id: UUID,
        invoice_id: UUID | None,
        external_ref: str,
        amount: Decimal,
        applied: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
    ) -> UUID:
        raise NotImplementedError

    async def record_suspense(
        self,
        *,
        school_id: UUID,
        external_ref: str,
        amount: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        reason: str,
    ) -> UUID:
        raise NotImplementedError

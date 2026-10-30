"""Telling a family something, by the channel they actually read.

The spec files Communications under Phase 3, and for templates, a delivery log
and opt-out management that is right. This is not that. This is the one send
the Phase 1 gate cannot do without: a school running its January intake on
this system has stopped phoning parents by hand, so when a child is enrolled
the system is what says so.

Three decisions worth stating, because each is the kind that looks arbitrary
later.

**It sends after the commit, never inside it.** A message is not transactional
and cannot be rolled back. Sending inside the enrolment's transaction risks
telling a parent their child is enrolled and then rolling the enrolment back —
a phone call the school cannot win. Sending after it risks losing the message
while the enrolment stands, which is the right way round: the record is
correct and the message can be sent again.

**A failed send never fails the caller.** By the time this runs the deposit is
taken and the child is on the roll. Raising here would turn a gateway with no
credit into a 500 on an enrolment that actually succeeded, and the registrar
would try again and make a second pupil.

**An opt-out does not silence it.** `guardian.sms_opted_out` is for the
newsletter. A guardian who stopped wanting messages from the school did not
stop wanting to know their own child is enrolled, which is why every send here
is `SmsKind.TRANSACTIONAL`.

What is deliberately absent is a delivery log. Until there is a table for it,
a send leaves an audit row — enough to answer "was this parent told, and
when", which is the question that gets asked. A retry queue, a per-guardian
message history and delivery reports arrive with the Communications module,
and `SmsSender.status` is already there waiting for them.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.core.config import settings
from app.domain.notifications import EnrolmentFacts, enrolment_confirmed
from app.integrations.sms.base import SmsKind, SmsMessage, SmsSender, SmsState
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.schools import SchoolRepository

logger = logging.getLogger("app.notifications")


class NotificationsService:
    """One method per thing the school says. Composition lives in the domain.

    It commits its own audit row rather than joining the caller's transaction,
    because by the time it runs that transaction is deliberately already
    closed — the caller committed the enrolment before asking for a message
    about it.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        sms: SmsSender,
        schools: SchoolRepository,
        audit: AuditRepository,
    ) -> None:
        self._uow = uow
        self._sms = sms
        self._schools = schools
        self._audit = audit

    async def enrolment_confirmed(
        self,
        *,
        school_id: UUID,
        phone: str,
        child_first_name: str,
        admission_no: str,
        student_id: UUID | None = None,
    ) -> None:
        """"Your child is in, here is the number, here is how to sign in."

        Called after the enrolment has committed. Returns None whatever
        happens: see the module docstring.
        """
        school = await self._schools.get(school_id)
        if school is None:
            # Cannot name the school, so there is nothing worth sending.
            logger.warning("no school %s to send an enrolment message for", school_id)
            return

        body = enrolment_confirmed(
            EnrolmentFacts(
                child_first_name=child_first_name,
                admission_no=admission_no,
                school_short_name=school.short_name or school.name,
                portal=settings.PORTAL_HOST,
            ),
            locale=school.locale,
        )

        await self._send(
            school_id=school_id,
            message=SmsMessage(
                to=phone,
                body=body,
                kind=SmsKind.TRANSACTIONAL,
                reference=f"enrolment:{admission_no}",
            ),
            action="notification.enrolment_confirmed",
            entity_id=student_id,
        )

    async def _send(
        self,
        *,
        school_id: UUID,
        message: SmsMessage,
        action: str,
        entity_id: UUID | None,
    ) -> None:
        """Hand it over, write down what happened, swallow the failure.

        The audit row is written for a rejected message too. "We tried to tell
        this parent and the gateway refused" is the answer to a complaint; no
        row at all looks identical to never having tried.
        """
        try:
            result = await self._sms.send(message)
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("enrolment message to %s could not be sent", message.to)
            result = None

        delivered = result is not None and result.state is not SmsState.FAILED
        try:
            await self._audit.record(
                school_id=school_id,
                action=action,
                entity_type="student" if entity_id else None,
                entity_id=entity_id,
                note=f"{message.to} ({'sent' if delivered else 'not sent'})",
                changed={
                    "to": message.to,
                    # The body is kept because the question that gets asked is
                    # not "was a message sent" but "what was this parent
                    # told". It holds the child's name and admission number,
                    # both of which the audit trail already carries.
                    "body": message.body,
                    "state": result.state.value if result else "error",
                    "external_ref": result.external_ref if result else None,
                    "error": result.error if result else "the sender raised",
                },
            )
            await self._uow.commit()
        except Exception:  # noqa: BLE001 - the enrolment has already committed
            logger.exception("could not record that a message was sent to %s", message.to)
            await self._uow.rollback()

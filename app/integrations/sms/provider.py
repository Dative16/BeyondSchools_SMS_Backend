"""SMS gateway adapters, and the factory that picks one.

Two are real here: a logging sender for local work and tests, and Beem Africa,
which is the gateway most Tanzanian schools' existing bulk-SMS accounts are
with. Which gateway the pilot school actually uses is still an open decision —
the same shape of decision as the payment aggregator — so `SMS_PROVIDER`
chooses, and an unconfigured name fails loudly at startup rather than silently
dropping a parent's one-time code.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.integrations.sms.base import SmsMessage, SmsResult, SmsSender, SmsState, to_e164

logger = logging.getLogger("app.sms")


class LoggingSmsSender:
    """Writes the message to the log instead of sending it.

    The default for local work, and what the tests use. It logs the body in
    full on purpose: a developer running the admissions flow needs to read the
    one-time code, and in production this adapter is not the one in use.
    """

    name = "mock"

    async def send(self, message: SmsMessage) -> SmsResult:
        logger.info(
            "sms (not sent: mock provider) to=%s kind=%s ref=%s body=%r",
            message.to,
            message.kind,
            message.reference,
            message.body,
        )
        return SmsResult(
            external_ref=f"mock-{abs(hash((message.to, message.body))):x}",
            state=SmsState.SENT,
            raw={"provider": "mock", "to": message.to},
        )

    async def status(self, external_ref: str) -> SmsResult:
        return SmsResult(external_ref=external_ref, state=SmsState.DELIVERED, raw={})


class BeemSmsSender:
    """Beem Africa's bulk SMS API.

    Basic auth over HTTPS, one POST per send. Nothing here retries: a retry
    that duplicates a fee reminder annoys a parent, and a retry that duplicates
    a one-time code invalidates the first one. The caller decides, with the
    delivery log in front of it.
    """

    name = "beem"
    BASE_URL = "https://apisms.beem.africa/v1"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        sender_id: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("SMS_API_KEY and SMS_API_SECRET are required for the beem provider")
        self._auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
        self._sender_id = sender_id
        self._timeout = timeout_seconds

    async def send(self, message: SmsMessage) -> SmsResult:
        payload: dict[str, Any] = {
            "source_addr": self._sender_id,
            "encoding": 0,
            "message": message.body,
            "recipients": [{"recipient_id": 1, "dest_addr": to_e164(message.to).lstrip("+")}],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self.BASE_URL}/send",
                    json=payload,
                    headers={"Authorization": f"Basic {self._auth}"},
                )
        except httpx.HTTPError as exc:
            # A gateway being unreachable is a delivery failure, not a crash:
            # the reminder run has 399 more messages to get through.
            return SmsResult(None, SmsState.FAILED, {"error": str(exc)}, error=str(exc))

        body: dict[str, Any]
        try:
            body = response.json()
        except ValueError:
            body = {"status_code": response.status_code, "text": response.text[:500]}

        if response.is_success:
            return SmsResult(
                external_ref=str(body.get("request_id") or ""),
                state=SmsState.SENT,
                raw=body,
            )
        return SmsResult(
            external_ref=None,
            state=SmsState.FAILED,
            raw=body,
            error=str(body.get("message") or response.status_code),
        )

    async def status(self, external_ref: str) -> SmsResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self.BASE_URL}/delivery-reports",
                    params={"request_id": external_ref},
                    headers={"Authorization": f"Basic {self._auth}"},
                )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SmsResult(external_ref, SmsState.QUEUED, {"error": str(exc)}, error=str(exc))

        delivered = str(body.get("status", "")).upper() == "DELIVRD"
        return SmsResult(
            external_ref=external_ref,
            state=SmsState.DELIVERED if delivered else SmsState.SENT,
            raw=body,
        )


def get_sms_sender() -> SmsSender:
    """The one configured gateway.

    An unknown provider name raises here rather than returning a no-op. A
    school whose one-time codes are being quietly discarded looks, from the
    outside, exactly like a school whose parents cannot type.
    """
    provider = (settings.SMS_PROVIDER or "mock").strip().lower()
    if provider == "mock":
        return LoggingSmsSender()
    if provider == "beem":
        return BeemSmsSender(
            api_key=settings.SMS_API_KEY,
            secret_key=settings.SMS_API_SECRET,
            sender_id=settings.SMS_SENDER_ID,
        )
    raise ValueError(
        f"SMS_PROVIDER={provider!r} has no adapter. Implement one in "
        "app/integrations/sms/provider.py or set SMS_PROVIDER=mock."
    )

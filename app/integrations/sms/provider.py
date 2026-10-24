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
import json
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

    The wire format follows the implementation already in production on the
    .NET side, down to three details that look like noise and are not:

    * the body is serialised with ASCII-escaped JSON and sent as ASCII bytes,
      so a Kiswahili message with a non-ASCII character travels as an escape
      sequence rather than as UTF-8 the gateway may mishandle;
    * `encoding` is the string "0", not the number 0;
    * the Basic credentials are base64 of ISO-8859-1 bytes.

    Nothing here retries. A retry that duplicates a fee reminder annoys a
    parent; a retry that duplicates a one-time code invalidates the first one.
    The caller decides, with the delivery log in front of it.
    """

    name = "beem"
    DELIVERY_REPORTS_URL = "https://dlrapi.beem.africa/public/v1/delivery-reports"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        sender_id: str,
        base_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("SMS_API_KEY and SMS_API_SECRET are required for the beem provider")
        credentials = f"{api_key}:{secret_key}".encode("iso-8859-1")
        self._auth = base64.b64encode(credentials).decode("ascii")
        self._sender_id = sender_id
        self._base_url = base_url
        self._timeout = timeout_seconds
        #: Only set by tests, which assert the wire format rather than send.
        self._transport = transport

    async def send(self, message: SmsMessage) -> SmsResult:
        payload: dict[str, Any] = {
            "source_addr": self._sender_id,
            "schedule_time": "",
            "encoding": "0",
            "message": message.body,
            "recipients": [
                {"recipient_id": 1, "dest_addr": to_e164(message.to).lstrip("+")}
            ],
        }
        body_bytes = json.dumps(payload).encode("ascii")

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    self._base_url,
                    content=body_bytes,
                    headers={
                        "Authorization": f"Basic {self._auth}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            # A gateway being unreachable is a delivery failure, not a crash:
            # the reminder run has 399 more messages to get through.
            logger.warning("sms send failed to reach the gateway: %s", exc)
            return SmsResult(None, SmsState.FAILED, {"error": str(exc)}, error=str(exc))

        parsed: dict[str, Any]
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"status_code": response.status_code, "text": response.text[:500]}

        # Beem answers 200 with `successful: false` for a rejected message —
        # an unapproved sender ID, or no credit — so the status code alone is
        # not the answer.
        accepted = response.is_success and parsed.get("successful") is not False
        if accepted:
            return SmsResult(
                external_ref=str(parsed.get("request_id") or ""),
                state=SmsState.SENT,
                raw=parsed,
            )

        error = str(parsed.get("message") or parsed.get("error") or response.status_code)
        logger.warning("sms rejected by the gateway: %s", error)
        return SmsResult(external_ref=None, state=SmsState.FAILED, raw=parsed, error=error)

    async def status(self, external_ref: str) -> SmsResult:
        """Ask for a delivery report.

        A gateway that cannot answer leaves the message QUEUED rather than
        calling it failed: "we do not know yet" and "it did not arrive" are
        different things to show a bursar chasing a fee reminder.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    self.DELIVERY_REPORTS_URL,
                    params={"request_id": external_ref},
                    headers={"Authorization": f"Basic {self._auth}"},
                )
            parsed = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SmsResult(external_ref, SmsState.QUEUED, {"error": str(exc)}, error=str(exc))

        delivered = str(parsed.get("status", "")).upper() in ("DELIVRD", "DELIVERED")
        return SmsResult(
            external_ref=external_ref,
            state=SmsState.DELIVERED if delivered else SmsState.SENT,
            raw=parsed,
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
            base_url=settings.SMS_BASE_URL,
        )
    raise ValueError(
        f"SMS_PROVIDER={provider!r} has no adapter. Implement one in "
        "app/integrations/sms/provider.py or set SMS_PROVIDER=mock."
    )

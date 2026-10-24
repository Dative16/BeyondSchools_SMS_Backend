"""The Beem adapter's wire format, pinned without touching the network.

Worth testing for one reason: this is already working in production on the
.NET side, and the three details that make it work are the ones nobody would
guess from the documentation — an ASCII-escaped body, `encoding` as the string
"0", and Basic credentials built from ISO-8859-1 bytes. A refactor that
"tidies" any of them stops every one-time code arriving, and a parent who
cannot receive a code cannot finish an application.
"""

import base64
import json

import httpx
import pytest

from app.integrations.sms.base import SmsKind, SmsMessage, SmsState, to_e164
from app.integrations.sms.provider import BeemSmsSender, LoggingSmsSender

API_KEY = "test-key"
API_SECRET = "test-secret"  # noqa: S105 - a stand-in, never a real credential
SENDER_ID = "Beyond OTP"
SEND_URL = "https://apisms.beem.africa/v1/send"


def sender_with(handler) -> BeemSmsSender:
    return BeemSmsSender(
        api_key=API_KEY,
        secret_key=API_SECRET,
        sender_id=SENDER_ID,
        base_url=SEND_URL,
        transport=httpx.MockTransport(handler),
    )


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "successful": True,
            "request_id": 77338882,
            "code": 100,
            "message": "Message Submitted Successfully",
            "valid": 1,
            "invalid": 0,
            "duplicates": 0,
        },
    )


async def test_the_payload_matches_what_the_gateway_accepts() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        return accepted(request)

    result = await sender_with(handler).send(
        SmsMessage(to="0712345678", body="588218 is your BeyondSchools code.")
    )

    assert result.state is SmsState.SENT
    assert result.external_ref == "77338882"

    payload = json.loads(seen["body"])
    assert payload["source_addr"] == SENDER_ID
    assert payload["encoding"] == "0", "the gateway wants the string, not the number"
    assert payload["schedule_time"] == ""
    assert payload["recipients"] == [{"recipient_id": 1, "dest_addr": "255712345678"}]
    assert seen["content_type"] == "application/json"
    assert seen["url"] == SEND_URL


async def test_the_credentials_are_iso_8859_1_before_base64() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return accepted(request)

    await sender_with(handler).send(SmsMessage(to="0712345678", body="hello"))

    expected = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode("iso-8859-1")).decode()
    assert seen["auth"] == f"Basic {expected}"


async def test_a_swahili_message_travels_as_ascii() -> None:
    """The body is sent as ASCII bytes, so anything outside it has to be
    escaped rather than silently mangled by the gateway."""
    seen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return accepted(request)

    await sender_with(handler).send(
        SmsMessage(to="0712345678", body="Karibu shuleni — ada imelipwa")
    )

    body = seen["body"]
    body.decode("ascii")  # raises if anything slipped through unescaped
    assert "\\u2014" in body.decode("ascii"), "the em dash should be escaped, not dropped"


async def test_a_rejected_message_is_a_result_not_an_exception() -> None:
    """Beem answers 200 with successful: false for an unapproved sender ID or
    an empty account. A fee-reminder run of 400 messages must not stop at the
    first one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"successful": False, "code": 112, "message": "Invalid Sender Id"},
        )

    result = await sender_with(handler).send(SmsMessage(to="0712345678", body="hello"))

    assert result.state is SmsState.FAILED
    assert result.error == "Invalid Sender Id"
    assert result.external_ref is None


async def test_an_unreachable_gateway_is_a_delivery_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await sender_with(handler).send(SmsMessage(to="0712345678", body="hello"))

    assert result.state is SmsState.FAILED
    assert "no route to host" in (result.error or "")


async def test_an_http_error_is_reported_with_the_gateways_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    result = await sender_with(handler).send(SmsMessage(to="0712345678", body="hello"))

    assert result.state is SmsState.FAILED
    assert result.error == "Unauthorized"


async def test_the_adapter_refuses_to_exist_without_credentials() -> None:
    """Better to fail at startup than to discard a parent's one-time code."""
    with pytest.raises(ValueError, match="SMS_API_KEY"):
        BeemSmsSender(api_key="", secret_key="", sender_id=SENDER_ID, base_url=SEND_URL)


async def test_the_mock_sender_reports_a_send_without_making_one() -> None:
    result = await LoggingSmsSender().send(
        SmsMessage(to="0712345678", body="hello", kind=SmsKind.TRANSACTIONAL)
    )

    assert result.state is SmsState.SENT
    assert (result.external_ref or "").startswith("mock-")


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("0712345678", "+255712345678"),
        ("255712345678", "+255712345678"),
        ("+255712345678", "+255712345678"),
        ("0712 345 678", "+255712345678"),
        ("712345678", "+255712345678"),
    ],
)
def test_every_way_a_number_is_written_reaches_the_same_handset(
    written: str, expected: str
) -> None:
    """Parents write all of these for one phone. Stored locally, sent in
    E.164 — and duplicate screening depends on the two staying consistent."""
    assert to_e164(written) == expected

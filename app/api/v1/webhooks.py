"""Payment callbacks.

The whole file is the shape every endpoint should have: verify, delegate,
translate the answer into HTTP. There is no rule in here you could get wrong,
because there is no rule in here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.api.deps import payment_service, resolve_school_from_webhook
from app.integrations.payments.base import PaymentProvider
from app.schemas.payment import CallbackAck
from app.services.payments import PaymentRejected, PaymentService

router = APIRouter()


@router.post(
    "/payments/{provider_name}",
    response_model=CallbackAck,
    status_code=status.HTTP_200_OK,
    summary="Aggregator payment callback",
)
async def payment_callback(
    provider_name: str,
    request: Request,
    provider: Annotated[PaymentProvider, Depends()],
    service: Annotated[PaymentService, Depends(payment_service)],
    school_id: Annotated[str, Depends(resolve_school_from_webhook)],
    x_signature: Annotated[str | None, Header()] = None,
) -> CallbackAck:
    body = await request.body()

    try:
        event = provider.verify_callback(dict(request.headers), body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature") from exc

    try:
        result = await service.record_callback(school_id, event)
    except PaymentRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.reason) from exc

    # 200 even for a duplicate: the aggregator will keep retrying anything else,
    # and the duplicate was already handled the first time it arrived.
    return CallbackAck(accepted=True, disposition=result.disposition)

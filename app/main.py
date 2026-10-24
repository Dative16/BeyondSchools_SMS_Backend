"""Application entrypoint.

Two things are mounted outside `/api/v1` and have to stay there: the JWKS
endpoint and the discovery document. They are what aibeyondschools.co.tz reads
to validate a token this service signed, their addresses are published in that
document, and moving either one breaks the handoff for every student mid-term.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.security import TokenError, discovery_document, jwks

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fail at startup, not at the first sign-in.

    A missing signing key is the one configuration mistake that would otherwise
    stay invisible until a parent tried to log in, so the key is loaded here and
    the error is logged loudly. It is not fatal: `/health` staying up is what
    lets a deployment be rolled back rather than looked at in the dark.
    """
    try:
        logger.info("signing key loaded, kid=%s", next(iter(jwks()["keys"]))["kid"])
    except TokenError as exc:
        logger.error("no JWT signing key: %s", exc)
    yield


app = FastAPI(
    title="BeyondSchools SMS API",
    version="0.1.0",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/jwks.json", tags=["identity"])
async def jwks_endpoint() -> dict[str, list[dict[str, Any]]]:
    """The public half of the signing key.

    Served to anybody: that is what a public key is for, and it is how the AI
    platform validates a token without this service and that one sharing a
    secret.
    """
    return jwks()


@app.get("/.well-known/openid-configuration", tags=["identity"])
async def openid_configuration() -> dict[str, Any]:
    """Where to find the issuer and its keys.

    Deliberately not a full OpenID Provider — there is no authorisation-code
    flow here. It publishes what a relying party needs to validate a token it
    has been handed, and advertises no endpoints that do not exist.
    """
    return discovery_document()


app.include_router(api_router, prefix=settings.API_V1_PREFIX)


# --------------------------------------------------------------------------
# Local document storage
# --------------------------------------------------------------------------
# Mounted only when documents are kept on disk, which is development and the
# test suite. In production STORAGE_PROVIDER is r2 and these routes do not
# exist at all — the client talks to Cloudflare directly, and there is no
# file-serving path in this service to get wrong.
if settings.STORAGE_PROVIDER == "local" and settings.ENVIRONMENT != "production":
    from fastapi import HTTPException, Query, Request, Response, status

    from app.integrations.storage import get_object_store
    from app.integrations.storage.base import StorageError, check_stored_object
    from app.integrations.storage.local import LocalObjectStore

    def _local_store() -> LocalObjectStore:
        store = get_object_store()
        if not isinstance(store, LocalObjectStore):  # pragma: no cover - guarded above
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return store

    def _verify(
        store: LocalObjectStore, *, key: str, method: str, expires: int, signature: str
    ) -> None:
        """The same guarantee a presigned URL gives: this link, this method,
        until this moment. A dev URL that leaks has leaked something that
        expires, and the code above cannot tell the two adapters apart."""
        if not store.verify(object_key=key, method=method, expires=expires, signature=signature):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "That link is not valid")

    @app.put("/files/{object_key:path}", include_in_schema=False)
    async def put_local_file(
        object_key: str,
        request: Request,
        expires: Annotated[int, Query()],
        signature: Annotated[str, Query()],
    ) -> Response:
        store = _local_store()
        _verify(store, key=object_key, method="PUT", expires=expires, signature=signature)

        body = await request.body()
        content_type = request.headers.get("content-type", "application/octet-stream")
        stored = await store.write(object_key=object_key, data=body, content_type=content_type)
        try:
            check_stored_object(stored)
        except Exception as exc:
            await store.delete(object_key=object_key)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/files/{object_key:path}", include_in_schema=False)
    async def get_local_file(
        object_key: str,
        expires: Annotated[int, Query()],
        signature: Annotated[str, Query()],
    ) -> Response:
        store = _local_store()
        _verify(store, key=object_key, method="GET", expires=expires, signature=signature)

        try:
            data, content_type = await store.read(object_key=object_key)
        except StorageError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document") from exc
        return Response(content=data, media_type=content_type)

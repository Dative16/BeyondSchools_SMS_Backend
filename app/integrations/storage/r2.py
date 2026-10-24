"""Cloudflare R2, through its S3-compatible API.

R2 speaks S3, so this is an S3 adapter with R2's endpoint and its one quirk:
the region is always `auto`. Switching to S3 proper, or to DigitalOcean
Spaces, would mean changing the endpoint and nothing else.

Signing is done with botocore, which computes an HMAC over a canonical request
locally — no network call, which is why `presign_upload` and
`presign_download` are ordinary functions. The two calls that do touch the
network, `head` and `delete`, go through httpx against a presigned URL, so
this module keeps one HTTP client and stays async throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.integrations.storage.base import (
    ObjectStore,
    StorageError,
    StoredObject,
    UploadTicket,
)

#: R2 ignores the region but the signature does not: it is part of the
#: canonical request, and "auto" is what Cloudflare signs with.
R2_REGION = "auto"
SERVICE = "s3"


class R2ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        endpoint: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not (account_id and access_key_id and secret_access_key and bucket):
            raise ValueError(
                "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET "
                "are all required for the r2 storage provider"
            )
        self.name = "r2"
        self._bucket = bucket
        self._endpoint = (endpoint or f"https://{account_id}.r2.cloudflarestorage.com").rstrip("/")
        self._credentials = Credentials(access_key_id, secret_access_key)
        self._timeout = timeout_seconds

    # --- signing ----------------------------------------------------------
    def _url_for(self, object_key: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{object_key}"

    def _presign(
        self,
        *,
        method: str,
        object_key: str,
        expires_in: int,
        headers: dict[str, str] | None = None,
    ) -> str:
        request = AWSRequest(method=method, url=self._url_for(object_key), headers=headers or {})
        SigV4QueryAuth(self._credentials, SERVICE, R2_REGION, expires=expires_in).add_auth(request)
        return str(request.url)

    def presign_upload(
        self, *, object_key: str, content_type: str, expires_in: int
    ) -> UploadTicket:
        """A URL the client PUTs the file to.

        `Content-Type` is signed, so the client has to send exactly the type it
        declared — which is a small part of why the confirm step still checks
        what actually arrived.
        """
        headers = {"Content-Type": content_type}
        url = self._presign(
            method="PUT", object_key=object_key, expires_in=expires_in, headers=headers
        )
        return UploadTicket(
            url=url,
            object_key=object_key,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            headers=headers,
        )

    def presign_download(self, *, object_key: str, expires_in: int) -> str:
        return self._presign(method="GET", object_key=object_key, expires_in=expires_in)

    # --- the two that touch the network -----------------------------------
    async def head(self, *, object_key: str) -> StoredObject | None:
        url = self._presign(method="HEAD", object_key=object_key, expires_in=60)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.head(url)
        except httpx.HTTPError as exc:
            raise StorageError(f"could not reach storage: {exc}") from exc

        if response.status_code == 404:
            return None
        if not response.is_success:
            raise StorageError(f"storage answered {response.status_code} for a HEAD")

        return StoredObject(
            object_key=object_key,
            size_bytes=int(response.headers.get("content-length", 0)),
            content_type=response.headers.get("content-type"),
            etag=response.headers.get("etag"),
        )

    async def delete(self, *, object_key: str) -> None:
        url = self._presign(method="DELETE", object_key=object_key, expires_in=60)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.delete(url)
        except httpx.HTTPError as exc:
            raise StorageError(f"could not reach storage: {exc}") from exc

        # 204 for a delete, 404 for one that was already gone. Both are the
        # outcome the caller wanted.
        if response.status_code not in (200, 204, 404):
            raise StorageError(f"storage answered {response.status_code} for a DELETE")

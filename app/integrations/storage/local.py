"""Local-disk storage, for development and tests.

Not a pretend adapter: the admissions flow has to be runnable end to end
before R2 exists, and a flow that only works in production is a flow nobody
has tried. Files go under a directory, and the URLs point back at this API's
own `/files` routes, which exist only outside production.

The signature is an HMAC over the key and an expiry, which is the same shape
of guarantee a presigned URL gives — so a developer who leaks a dev URL has
leaked something that expires, and the code paths above this one cannot tell
the two adapters apart.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.integrations.storage.base import (
    ObjectStore,
    StorageError,
    StoredObject,
    UploadTicket,
)


class LocalObjectStore(ObjectStore):
    def __init__(self, *, root: str, base_url: str, secret: str) -> None:
        self.name = "local"
        self._root = Path(root)
        self._base_url = base_url.rstrip("/")
        self._secret = secret.encode()

    # --- signing ----------------------------------------------------------
    def _sign(self, *, object_key: str, method: str, expires: int) -> str:
        message = f"{method}:{object_key}:{expires}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def verify(self, *, object_key: str, method: str, expires: int, signature: str) -> bool:
        if expires < int(datetime.now(UTC).timestamp()):
            return False
        expected = self._sign(object_key=object_key, method=method, expires=expires)
        return hmac.compare_digest(expected, signature)

    def _url(self, *, object_key: str, method: str, expires_in: int) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        expires = int(expires_at.timestamp())
        signature = self._sign(object_key=object_key, method=method, expires=expires)
        return (
            f"{self._base_url}/files/{object_key}?expires={expires}&signature={signature}",
            expires_at,
        )

    def presign_upload(
        self, *, object_key: str, content_type: str, expires_in: int
    ) -> UploadTicket:
        url, expires_at = self._url(object_key=object_key, method="PUT", expires_in=expires_in)
        return UploadTicket(
            url=url,
            object_key=object_key,
            expires_at=expires_at,
            headers={"Content-Type": content_type},
        )

    def presign_download(self, *, object_key: str, expires_in: int) -> str:
        url, _ = self._url(object_key=object_key, method="GET", expires_in=expires_in)
        return url

    # --- the disk ---------------------------------------------------------
    def _path_for(self, object_key: str) -> Path:
        """Resolve a key to a path, refusing anything that escapes the root.

        The key is built by `build_object_key` and never comes from a client,
        but a path traversal check next to a path join costs nothing and the
        one time it matters is the time somebody wires a client-supplied key
        in by mistake.
        """
        candidate = (self._root / object_key).resolve()
        root = self._root.resolve()
        if not candidate.is_relative_to(root):
            raise StorageError("that object key is not inside the storage root")
        return candidate

    async def write(self, *, object_key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path_for(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (path.parent / f"{path.name}.type").write_text(content_type, encoding="utf-8")
        return StoredObject(object_key=object_key, size_bytes=len(data), content_type=content_type)

    async def read(self, *, object_key: str) -> tuple[bytes, str]:
        path = self._path_for(object_key)
        if not path.is_file():
            raise StorageError("no such object")
        type_file = path.parent / f"{path.name}.type"
        content_type = (
            type_file.read_text(encoding="utf-8").strip()
            if type_file.is_file()
            else "application/octet-stream"
        )
        return path.read_bytes(), content_type

    async def head(self, *, object_key: str) -> StoredObject | None:
        path = self._path_for(object_key)
        if not path.is_file():
            return None
        type_file = path.parent / f"{path.name}.type"
        return StoredObject(
            object_key=object_key,
            size_bytes=path.stat().st_size,
            content_type=(
                type_file.read_text(encoding="utf-8").strip() if type_file.is_file() else None
            ),
        )

    async def delete(self, *, object_key: str) -> None:
        path = self._path_for(object_key)
        path.unlink(missing_ok=True)
        (path.parent / f"{path.name}.type").unlink(missing_ok=True)

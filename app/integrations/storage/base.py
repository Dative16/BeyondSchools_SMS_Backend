"""Object storage: the protocol, and the key layout.

The bytes never pass through this API. A parent photographing a birth
certificate on a phone uploads it straight to storage with a presigned URL, and
this service only ever handles the reference. That is not a micro-optimisation:
a 4 MB photo through an application container on a 3G connection ties up a
worker for the length of the upload, and a school's intake day is several
hundred of them.

Three decisions worth stating, because each one is hard to reverse once a term
of documents exists:

**The bucket is private.** These are children's birth certificates. Reads go
through a short-lived presigned URL generated when somebody with permission
opens the document, never a public address — a public URL is a URL that ends up
in a WhatsApp group.

**The key carries the tenant.** `school/<school_id>/...` first, so a
misconfigured policy, a bulk lifecycle rule or a mistaken prefix delete cannot
cross schools. It also means a school that leaves can have its objects
enumerated and removed, which the Personal Data Protection Act's retention
duties will eventually require.

**Size and type are verified after the upload, not trusted before it.** A
presigned PUT cannot enforce a length, so the client declares what it is about
to send, and the confirm step asks storage what actually arrived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

#: What a phone camera and a scanner produce, and nothing else. An upload
#: endpoint that accepts any content type is an upload endpoint that will be
#: asked to serve somebody's HTML one day.
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "application/pdf": "pdf",
}

#: 8 MB. A phone photograph of a certificate is one to four; a scanned PDF of
#: a report card is rarely more. Large enough not to reject real documents,
#: small enough that a mistake is not a bill.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_SAFE_SEGMENT = re.compile(r"[^a-z0-9_-]+")


class StorageError(Exception):
    """Storage could not be reached, or refused. Never a reason to lose the
    record of what was supposed to be there."""


class UploadRejected(Exception):
    """The declared or actual upload is not something we will store."""


class Scope(StrEnum):
    """What an object belongs to.

    `APPLICATION` exists because documents arrive before a child does: a
    parent uploads a birth certificate during an application, and the object
    has to survive that application becoming a student.
    """

    STUDENT = "student"
    APPLICATION = "application"
    SCHOOL = "school"


@dataclass(frozen=True, slots=True)
class UploadTicket:
    """What a client needs to put one file in storage itself."""

    url: str
    #: The key the service will record, and the only part the client cannot
    #: choose. A client-chosen key is a client that can overwrite another
    #: school's object.
    object_key: str
    expires_at: datetime
    #: Headers the client must send for the signature to match.
    headers: dict[str, str]
    max_bytes: int = MAX_UPLOAD_BYTES


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    size_bytes: int
    content_type: str | None
    etag: str | None = None


def build_object_key(
    *,
    school_id: UUID,
    scope: Scope,
    owner_id: UUID,
    kind: str,
    content_type: str,
) -> str:
    """Where one file lives.

    `school/<id>/student/<id>/birth_certificate/<uuid>.jpg`

    A random name rather than the one the phone gave it: "IMG_0421.jpg" tells
    us nothing, and a parent's filename can carry the child's name, which is
    then in a URL, a log and a storage listing.
    """
    extension = ALLOWED_CONTENT_TYPES.get(content_type)
    if extension is None:
        raise UploadRejected(f"{content_type} is not a kind of file this school can upload")

    safe_kind = _SAFE_SEGMENT.sub("-", kind.strip().lower()) or "other"
    return f"school/{school_id}/{scope.value}/{owner_id}/{safe_kind}/{uuid4().hex}.{extension}"


def check_declared_upload(*, content_type: str, size_bytes: int | None) -> None:
    """What can be known before the bytes move.

    Rejecting here saves a parent on a weak connection from uploading four
    megabytes before being told no.
    """
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadRejected(f"{content_type} is not a kind of file this school can upload")
    if size_bytes is not None and size_bytes > MAX_UPLOAD_BYTES:
        megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise UploadRejected(f"that file is larger than {megabytes} MB")
    if size_bytes is not None and size_bytes <= 0:
        raise UploadRejected("that file is empty")


def check_stored_object(stored: StoredObject) -> None:
    """What can only be known once the bytes have arrived.

    The client declared a type and a size; this is what storage says actually
    turned up. A presigned PUT cannot enforce a length, so this is the check
    that makes the limit real.
    """
    if stored.size_bytes <= 0:
        raise UploadRejected("nothing was uploaded")
    if stored.size_bytes > MAX_UPLOAD_BYTES:
        megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise UploadRejected(f"the uploaded file is larger than {megabytes} MB")
    if stored.content_type and stored.content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadRejected(
            f"the uploaded file is {stored.content_type}, which this school cannot store"
        )


class ObjectStore(Protocol):
    """Four methods. Anything a document needs fits in them."""

    name: str

    def presign_upload(
        self, *, object_key: str, content_type: str, expires_in: int
    ) -> UploadTicket:
        """A URL the client can PUT one file to.

        Signing is arithmetic, not a network call, which is why this is not a
        coroutine: an HMAC over a canonical request, computed locally.
        """
        ...

    def presign_download(self, *, object_key: str, expires_in: int) -> str:
        """A short-lived URL for somebody who is allowed to read the file."""
        ...

    async def head(self, *, object_key: str) -> StoredObject | None:
        """What actually arrived, or None if nothing did."""
        ...

    async def delete(self, *, object_key: str) -> None:
        """Remove one object. Used when an upload is abandoned, and when a
        retention policy eventually reaches it."""
        ...

"""Prove the configured object store actually works, end to end.

    make storage-check

Runs a real round trip against whichever provider `STORAGE_PROVIDER` names:
presign an upload, PUT a file, ask storage what arrived, read it back through a
download link, then delete it. Nothing is left behind.

Worth having as a script rather than only a test, for two reasons. The unit
tests prove our signatures are *correct*; only Cloudflare can tell us they are
*accepted*, and that answer depends on credentials nobody should put in a test
fixture. And when an upload fails in production the question is always "is it
the signature, the credentials, the bucket policy or CORS" — this separates
the first three from the fourth in about a second.

What it cannot check is CORS, because CORS is a browser rule and this is not a
browser. See the note it prints at the end.
"""

from __future__ import annotations

import asyncio
import struct
import sys
import zlib
from uuid import uuid4

import httpx

from app.core.config import settings
from app.integrations.storage import get_object_store
from app.integrations.storage.base import Scope, build_object_key, check_stored_object


def sample_png() -> bytes:
    """A real 1x1 PNG, so the content type we sign for is the truth."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(bytes([0, 255, 255, 255]))
    return (
        signature + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
    )


def tick(message: str) -> None:
    print(f"  ok    {message}")


def fail(message: str, detail: str = "") -> None:
    print(f"  FAIL  {message}")
    if detail:
        print(f"        {detail}")


async def main() -> int:
    store = get_object_store()
    print(f"storage provider: {store.name}")
    if store.name == "r2":
        print(f"bucket:           {settings.R2_BUCKET}")
        print(f"endpoint:         {settings.R2_ENDPOINT or 'derived from the account id'}")
    else:
        print(f"path:             {settings.STORAGE_LOCAL_PATH}")
    print()

    payload = sample_png()
    content_type = "image/png"
    object_key = build_object_key(
        school_id=uuid4(),
        scope=Scope.SCHOOL,
        owner_id=uuid4(),
        kind="storage_check",
        content_type=content_type,
    )

    ticket = store.presign_upload(
        object_key=object_key, content_type=content_type, expires_in=300
    )
    tick(f"signed an upload URL for {object_key}")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.put(
                ticket.url, content=payload, headers=ticket.headers
            )
        except httpx.HTTPError as exc:
            # The exception class is the useful part: ConnectError is DNS or
            # the account id, ConnectTimeout is the network, and an SSL error
            # is a trust store. "could not reach storage" on its own sends
            # somebody looking at the wrong thing.
            fail(
                "could not reach storage to upload",
                f"{type(exc).__name__}: {str(exc) or 'the client gave no detail'}",
            )
            return 1

        if not response.is_success:
            fail(
                f"storage refused the upload with {response.status_code}",
                response.text[:400]
                or "an empty body usually means the signature or the credentials",
            )
            return 1
        tick(f"uploaded {len(payload)} bytes")

        stored = await store.head(object_key=object_key)
        if stored is None:
            fail("storage says the object is not there after a successful upload")
            return 1
        check_stored_object(stored)
        tick(f"storage reports {stored.size_bytes} bytes, {stored.content_type}")

        url = store.presign_download(object_key=object_key, expires_in=300)
        fetched = await client.get(url)
        if not fetched.is_success:
            fail(f"the download link answered {fetched.status_code}", fetched.text[:400])
            return 1
        if fetched.content != payload:
            fail("what came back is not what went up")
            return 1
        tick("read it back through a presigned link, byte for byte")

        await store.delete(object_key=object_key)
        if await store.head(object_key=object_key) is not None:
            fail("the object survived a delete")
            return 1
        tick("deleted it, nothing left behind")

    print("\nstorage is working.")
    if store.name == "r2":
        print(
            "\nOne thing this cannot check: CORS. The browser uploads straight to\n"
            "R2, so the bucket needs a CORS rule allowing PUT from the web app's\n"
            "origin with the Content-Type header — otherwise uploads work from\n"
            "here and fail in the app. R2 > the bucket > Settings > CORS policy:\n"
            '  AllowedOrigins: the web app\'s origin\n'
            "  AllowedMethods: PUT, GET\n"
            "  AllowedHeaders: content-type\n"
            "Keep the public development URL disabled: these are children's\n"
            "documents, and reads go through short-lived presigned links."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Object keys, upload limits, and the local adapter's signatures.

The key layout is worth testing because it is the thing that cannot be changed
once a term of documents exists, and because the tenant prefix is a safety
property rather than a convenience: it is what stops a lifecycle rule or a
prefix delete crossing schools.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.integrations.storage.base import (
    MAX_UPLOAD_BYTES,
    Scope,
    StoredObject,
    UploadRejected,
    build_object_key,
    check_declared_upload,
    check_stored_object,
)
from app.integrations.storage.local import LocalObjectStore


# --------------------------------------------------------------------------
# Where a file lives
# --------------------------------------------------------------------------
def test_the_key_starts_with_the_school() -> None:
    """A safety property, not a tidiness one: a misconfigured policy, a bulk
    lifecycle rule or a mistaken prefix delete cannot reach another school."""
    school_id, student_id = uuid4(), uuid4()

    key = build_object_key(
        school_id=school_id,
        scope=Scope.STUDENT,
        owner_id=student_id,
        kind="birth_certificate",
        content_type="image/jpeg",
    )

    assert key.startswith(f"school/{school_id}/student/{student_id}/birth_certificate/")
    assert key.endswith(".jpg")


def test_the_filename_is_random_not_the_one_the_phone_gave() -> None:
    """A parent's filename can carry the child's name, and that name would
    then be in a URL, a log and a storage listing."""
    common = {
        "school_id": uuid4(),
        "scope": Scope.STUDENT,
        "owner_id": uuid4(),
        "kind": "passport_photo",
        "content_type": "image/jpeg",
    }

    first = build_object_key(**common)
    second = build_object_key(**common)

    assert first != second


def test_an_application_can_own_a_document_before_a_student_exists() -> None:
    """Documents arrive during an application, and have to survive it becoming
    a pupil."""
    key = build_object_key(
        school_id=uuid4(),
        scope=Scope.APPLICATION,
        owner_id=uuid4(),
        kind="birth_certificate",
        content_type="application/pdf",
    )

    assert "/application/" in key
    assert key.endswith(".pdf")


def test_a_kind_with_awkward_characters_is_made_safe() -> None:
    key = build_object_key(
        school_id=uuid4(),
        scope=Scope.STUDENT,
        owner_id=uuid4(),
        kind="Cheti cha Kuzaliwa!!",
        content_type="image/png",
    )

    assert "/cheti-cha-kuzaliwa-/" in key


def test_a_type_the_school_cannot_store_has_no_key() -> None:
    with pytest.raises(UploadRejected, match="not a kind of file"):
        build_object_key(
            school_id=uuid4(),
            scope=Scope.STUDENT,
            owner_id=uuid4(),
            kind="birth_certificate",
            content_type="application/x-msdownload",
        )


# --------------------------------------------------------------------------
# What may be uploaded
# --------------------------------------------------------------------------
def test_a_phone_photograph_is_accepted() -> None:
    for content_type in ("image/jpeg", "image/png", "image/webp", "image/heic"):
        check_declared_upload(content_type=content_type, size_bytes=2 * 1024 * 1024)
    check_declared_upload(content_type="application/pdf", size_bytes=400_000)


def test_an_executable_is_refused_before_a_byte_moves() -> None:
    """Rejecting on the declaration saves a parent on a weak connection from
    uploading four megabytes before being told no."""
    with pytest.raises(UploadRejected):
        check_declared_upload(content_type="text/html", size_bytes=100)


def test_an_oversized_declaration_is_refused_with_the_limit_in_the_message() -> None:
    with pytest.raises(UploadRejected, match="larger than 8 MB"):
        check_declared_upload(content_type="image/jpeg", size_bytes=MAX_UPLOAD_BYTES + 1)


def test_an_empty_declaration_is_refused() -> None:
    with pytest.raises(UploadRejected, match="empty"):
        check_declared_upload(content_type="image/jpeg", size_bytes=0)


def test_an_unstated_size_is_allowed_because_the_confirm_step_checks_it() -> None:
    check_declared_upload(content_type="image/jpeg", size_bytes=None)


# --------------------------------------------------------------------------
# What actually arrived
# --------------------------------------------------------------------------
def test_the_stored_object_is_checked_not_just_the_declaration() -> None:
    """A presigned PUT cannot enforce a length, so this is the check that
    makes the limit real."""
    too_big = StoredObject(
        object_key="k", size_bytes=MAX_UPLOAD_BYTES + 1, content_type="image/jpeg"
    )

    with pytest.raises(UploadRejected, match="uploaded file is larger"):
        check_stored_object(too_big)


def test_an_upload_that_arrived_as_something_else_is_refused() -> None:
    """The client signed for one content type and sent another."""
    wrong = StoredObject(object_key="k", size_bytes=1000, content_type="text/html")

    with pytest.raises(UploadRejected, match="which this school cannot store"):
        check_stored_object(wrong)


def test_an_empty_upload_is_refused() -> None:
    with pytest.raises(UploadRejected, match="nothing was uploaded"):
        check_stored_object(StoredObject(object_key="k", size_bytes=0, content_type="image/jpeg"))


def test_storage_that_reports_no_content_type_is_still_accepted() -> None:
    """Some backends do not return one on a HEAD, and the size check is the
    one that matters."""
    check_stored_object(StoredObject(object_key="k", size_bytes=1000, content_type=None))


# --------------------------------------------------------------------------
# The local adapter's signatures
# --------------------------------------------------------------------------
def store(tmp_path) -> LocalObjectStore:
    return LocalObjectStore(
        root=str(tmp_path),
        base_url="http://testserver",
        secret="a-test-secret",  # noqa: S106 - a signing key for a temp directory
    )


def test_a_local_link_carries_an_expiry_and_a_signature(tmp_path) -> None:
    local = store(tmp_path)

    ticket = local.presign_upload(
        object_key="school/a/student/b/bc/c.jpg", content_type="image/jpeg", expires_in=900
    )

    assert "expires=" in ticket.url
    assert "signature=" in ticket.url
    assert ticket.headers == {"Content-Type": "image/jpeg"}
    assert ticket.expires_at > datetime.now(UTC)


def test_a_signature_is_bound_to_the_key_and_the_method(tmp_path) -> None:
    """So a download link cannot be turned into an upload link, and a link for
    one child's document cannot be pointed at another's."""
    local = store(tmp_path)
    expires = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    signature = local._sign(object_key="a/b.jpg", method="GET", expires=expires)

    assert local.verify(object_key="a/b.jpg", method="GET", expires=expires, signature=signature)
    assert not local.verify(
        object_key="a/other.jpg", method="GET", expires=expires, signature=signature
    )
    assert not local.verify(
        object_key="a/b.jpg", method="PUT", expires=expires, signature=signature
    )


def test_an_expired_signature_is_refused(tmp_path) -> None:
    local = store(tmp_path)
    expires = int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())
    signature = local._sign(object_key="a/b.jpg", method="GET", expires=expires)

    assert not local.verify(
        object_key="a/b.jpg", method="GET", expires=expires, signature=signature
    )


async def test_a_file_round_trips_through_the_local_store(tmp_path) -> None:
    local = store(tmp_path)
    key = "school/a/student/b/bc/c.png"

    await local.write(object_key=key, data=b"not really a png", content_type="image/png")

    head = await local.head(object_key=key)
    assert head is not None
    assert head.size_bytes == 16
    assert head.content_type == "image/png"

    data, content_type = await local.read(object_key=key)
    assert data == b"not really a png"
    assert content_type == "image/png"

    await local.delete(object_key=key)
    assert await local.head(object_key=key) is None


async def test_a_key_that_climbs_out_of_the_root_is_refused(tmp_path) -> None:
    """The key is built by `build_object_key` and never comes from a client,
    but the check costs nothing and the one time it matters is the time
    somebody wires a client-supplied key in by mistake."""
    local = store(tmp_path)

    with pytest.raises(Exception, match="not inside the storage root"):
        await local.head(object_key="../../etc/passwd")

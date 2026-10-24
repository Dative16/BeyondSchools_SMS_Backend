"""Object storage: one protocol, two adapters, chosen by configuration.

`local` writes to disk and serves through this API, so the admissions flow is
runnable before R2 exists. `r2` is Cloudflare R2 through its S3-compatible
API, which is what production uses. Nothing above this package can tell them
apart.
"""

from functools import lru_cache

from app.core.config import settings
from app.integrations.storage.base import ObjectStore
from app.integrations.storage.local import LocalObjectStore
from app.integrations.storage.r2 import R2ObjectStore


@lru_cache
def get_object_store() -> ObjectStore:
    """The one configured store.

    An unknown provider raises rather than returning something that silently
    discards uploads: a school whose birth certificates are going nowhere
    looks, from the outside, exactly like a school whose parents have not
    uploaded any.
    """
    provider = (settings.STORAGE_PROVIDER or "local").strip().lower()

    if provider == "local":
        return LocalObjectStore(
            root=settings.STORAGE_LOCAL_PATH,
            base_url=settings.PUBLIC_BASE_URL.rstrip("/"),
            secret=settings.SECRET_KEY,
        )
    if provider == "r2":
        return R2ObjectStore(
            account_id=settings.R2_ACCOUNT_ID,
            access_key_id=settings.R2_ACCESS_KEY_ID,
            secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            bucket=settings.R2_BUCKET,
            endpoint=settings.R2_ENDPOINT or None,
        )

    raise ValueError(f"STORAGE_PROVIDER={provider!r} has no adapter. Use 'local' or 'r2'.")


__all__ = ["ObjectStore", "get_object_store"]

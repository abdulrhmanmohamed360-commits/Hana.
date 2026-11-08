
"""Private Supabase Storage integration for EduForge documents."""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qs

import httpx

from app.core.config import Settings


from app.services.docreader.security import SUPPORTED_EXTENSIONS as ALLOWED_EXTENSIONS  # noqa: E402  (single source of truth)


class StorageError(RuntimeError):
    """Raised when Supabase Storage operations fail."""


class FileTooLargeError(StorageError):
    """The stored object exceeds the configured size limit (permanent, never retried)."""


def _credentials(settings: Settings) -> tuple[str, str, str]:
    url = (settings.supabase_url or "").strip().rstrip("/")
    key = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key
        else ""
    )
    bucket = settings.supabase_storage_bucket.strip()

    if not url or not key or not bucket:
        raise StorageError("Supabase Storage is not configured")

    if not url.startswith("https://"):
        raise StorageError("Supabase URL must use HTTPS")

    return url, key, bucket


def _headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _validate_object_path(object_path: str) -> str:
    path = object_path.strip()

    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ".." in path.split("/")
        or not path.startswith("incoming/")
    ):
        raise StorageError("Invalid storage object path")

    return path


def _new_object_path(original_name: str) -> str:
    suffix = Path(original_name or "").suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        raise StorageError(
            f"Unsupported file type: {suffix or 'no extension'}"
        )

    return f"incoming/{uuid.uuid4().hex}{suffix}"


def create_signed_upload(
    settings: Settings,
    original_name: str,
    content_type: str = "application/octet-stream",
) -> tuple[str, str]:
    """
    Create a temporary signed upload URL.

    Returns:
        (object_path, signed_upload_url)

    The service-role key is used only on the backend.
    The client uploads directly to Supabase using PUT.
    """
    base, key, bucket = _credentials(settings)
    object_path = _new_object_path(original_name)

    endpoint = (
        f"{base}/storage/v1/object/upload/sign/"
        f"{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
    )

    try:
        response = httpx.post(
            endpoint,
            headers={
                **_headers(key),
                "Content-Type": "application/json",
            },
            json={"upsert": False},
            timeout=settings.supabase_upload_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()

    except (httpx.HTTPError, ValueError) as exc:
        raise StorageError(
            "Could not create a Supabase signed upload URL"
        ) from exc

    returned_url = (
        payload.get("url")
        or payload.get("signedURL")
        or payload.get("signedUrl")
    )

    if not isinstance(returned_url, str) or not returned_url.strip():
        raise StorageError(
            "Supabase returned an invalid signed upload URL"
        )

    if returned_url.startswith(("http://", "https://")):
        upload_url = returned_url
    elif returned_url.startswith("/storage/v1/"):
        upload_url = base + returned_url
    elif returned_url.startswith("/"):
        upload_url = f"{base}/storage/v1{returned_url}"
    else:
        upload_url = f"{base}/storage/v1/{returned_url.lstrip('/')}"

    base_parts = urlsplit(base)
    upload_parts = urlsplit(upload_url)

    if (
        upload_parts.scheme != "https"
        or upload_parts.netloc != base_parts.netloc
        or not parse_qs(upload_parts.query).get("token")
    ):
        raise StorageError(
            "Supabase returned an invalid or incomplete upload URL"
        )

    return object_path, upload_url


def object_exists(
    settings: Settings,
    object_path: str,
) -> bool:
    """Check whether an uploaded object exists in the private bucket."""
    base, key, bucket = _credentials(settings)
    object_path = _validate_object_path(object_path)

    endpoint = (
        f"{base}/storage/v1/object/info/"
        f"{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
    )

    try:
        response = httpx.get(
            endpoint,
            headers=_headers(key),
            timeout=settings.supabase_upload_timeout_seconds,
        )

        if response.status_code == 404:
            return False

        response.raise_for_status()
        return True

    except httpx.HTTPError as exc:
        raise StorageError(
            "Could not verify the uploaded document"
        ) from exc


def download_object(
    settings: Settings,
    object_path: str,
    destination: Path,
    max_bytes: int | None = None,
) -> None:
    """Download a private Supabase object to a local temporary file, aborting once `max_bytes` is exceeded."""
    base, key, bucket = _credentials(settings)
    object_path = _validate_object_path(object_path)

    endpoint = (
        f"{base}/storage/v1/object/"
        f"{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
    )

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        written = 0
        with httpx.stream(
            "GET",
            endpoint,
            headers=_headers(key),
            timeout=settings.supabase_download_timeout_seconds,
        ) as response:
            response.raise_for_status()

            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    if chunk:
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            raise FileTooLargeError(
                                f"Stored document exceeds the {max_bytes}-byte limit"
                            )
                        output.write(chunk)

    except FileTooLargeError:
        destination.unlink(missing_ok=True)
        raise

    except (httpx.HTTPError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise StorageError(
            "Could not download the stored document"
        ) from exc

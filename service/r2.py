"""Cloudflare R2 client for portal document storage.

R2 is S3-compatible, so we use boto3's S3 client pointed at the R2 endpoint.
Two access patterns:

  - Upload: admin browser uploads directly to R2 via a presigned PUT URL
    (keeps large files off Railway). The backend never touches the bytes.
  - Download: the backend issues a short-lived (5 min) presigned GET URL after
    RLS confirms the caller may see the document. Never expose the raw key or
    a long-lived URL; this is the access-control + revocation boundary.

Object key convention (matches the documents.storage_key comment in the
initial schema): {org_id}/{doc_id}/{version}

Config comes from env (see .env.example):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT
R2_ENDPOINT defaults to https://<account_id>.r2.cloudflarestorage.com.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import boto3
from botocore.client import Config

log = logging.getLogger("locke.r2")

# Presigned GET lifetime. Short by design: a download URL is for initiating a
# transfer, not for sharing. In-progress transfers continue past expiry on
# S3-compatible stores; only the act of starting a new download is gated.
DOWNLOAD_URL_TTL_SECONDS = 300

# Presigned PUT lifetime. Wider than download because a large admin upload may
# take a while to begin, but still bounded.
UPLOAD_URL_TTL_SECONDS = 900


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} is not set; R2 document storage is unavailable")
    return val


@lru_cache(maxsize=1)
def _client():
    """Build (once) the boto3 S3 client pointed at R2.

    Cached so we reuse the underlying connection pool across requests.
    signature_version s3v4 is required for R2 presigning.
    """
    account_id = _require_env("R2_ACCOUNT_ID")
    endpoint = os.environ.get(
        "R2_ENDPOINT",
        f"https://{account_id}.r2.cloudflarestorage.com",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_require_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_require_env("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4"),
        region_name="auto",  # R2 ignores region; "auto" is the documented value
    )


def bucket() -> str:
    return _require_env("R2_BUCKET")


def build_storage_key(org_id, doc_id, version: int = 1) -> str:
    """Canonical R2 object key for a document version."""
    return f"{org_id}/{doc_id}/{version}"


def presign_put(storage_key: str, content_type: Optional[str] = None) -> str:
    """Presigned PUT URL the admin browser uploads to directly.

    content_type, if given, is bound into the signature, so the browser must
    send a matching Content-Type header on the PUT.
    """
    params = {"Bucket": bucket(), "Key": storage_key}
    if content_type:
        params["ContentType"] = content_type
    url = _client().generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    log.info("r2.presign_put key=%s", storage_key)
    return url


def presign_get(
    storage_key: str,
    ttl: int = DOWNLOAD_URL_TTL_SECONDS,
    download_filename: Optional[str] = None,
) -> str:
    """Presigned GET URL for a download.

    download_filename, if given, sets a Content-Disposition response header so
    the browser saves the file under its friendly name instead of the opaque key.
    """
    params = {"Bucket": bucket(), "Key": storage_key}
    if download_filename:
        safe = download_filename.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    url = _client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl,
    )
    log.info("r2.presign_get key=%s ttl=%d", storage_key, ttl)
    return url


def put_bytes(storage_key: str, data: bytes, content_type: Optional[str] = None) -> None:
    """Server-side upload of in-memory bytes to R2.

    Unlike the presigned-PUT path (browser uploads directly), this is for bytes
    the backend already holds, e.g. an executed PDF pulled from an e-signature
    provider in a webhook handler. Stores under the same key convention so the
    object behaves like any other document.
    """
    params = {"Bucket": bucket(), "Key": storage_key, "Body": data}
    if content_type:
        params["ContentType"] = content_type
    _client().put_object(**params)
    log.info("r2.put_bytes key=%s bytes=%d", storage_key, len(data))


def delete(storage_key: str) -> None:
    """Hard-delete an object from R2. Soft-delete (documents.deleted_at) is the
    normal path; this exists for retention/purge jobs, not request handlers.
    """
    _client().delete_object(Bucket=bucket(), Key=storage_key)
    log.info("r2.delete key=%s", storage_key)

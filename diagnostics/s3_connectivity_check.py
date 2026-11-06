"""S3 put/head/delete check using S3_EXPORT_BUCKET, then S3_BUCKET.

Run later from the repo root:

    python -m diagnostics.s3_connectivity_check

Creates one tiny object under connectivity-tests/<utc>-<random>/probe.txt,
confirms it with head_object, deletes that exact key, and confirms the
follow-up head is a 404. Does not list, download, or delete any other key.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from datetime import datetime, timezone

from app.storage.s3_client import create_s3_client

_PROBE_BODY = b"s3-connectivity-probe"
_PREFIX = "connectivity-tests"
_OBJECT_NAME = "probe.txt"
_MISSING = {"404", "NoSuchKey", "NotFound"}


def _quiet_aws_loggers():
    for name in (
        "boto3",
        "botocore",
        "botocore.credentials",
        "s3transfer",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
        logger.disabled = True


def _report(stage, exc=None):
    if exc is None:
        print(f"S3 connectivity check failed: {stage}", file=sys.stderr)
    else:
        print(
            f"S3 connectivity check failed: {stage} ({type(exc).__name__})",
            file=sys.stderr,
        )


def _bucket():
    for name in ("S3_EXPORT_BUCKET", "S3_BUCKET"):
        raw = os.getenv(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    print("S3_EXPORT_BUCKET and S3_BUCKET are not set", file=sys.stderr)
    return None


def _probe_key():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{_PREFIX}/{stamp}-{secrets.token_hex(16)}/{_OBJECT_NAME}"


def _is_probe_key(key):
    if not isinstance(key, str):
        return False
    parts = key.split("/")
    if len(parts) != 3:
        return False
    prefix, folder, name = parts
    if prefix != _PREFIX or name != _OBJECT_NAME:
        return False
    if not folder or folder in {".", ".."} or "/" in folder or "\\" in folder:
        return False
    return True


def _is_missing(exc):
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    if isinstance(error, dict):
        code = error.get("Code")
        if isinstance(code, str) and code in _MISSING:
            return True
    meta = response.get("ResponseMetadata")
    return isinstance(meta, dict) and meta.get("HTTPStatusCode") == 404


def _delete_exact(client, bucket, key):
    if not _is_probe_key(key):
        return RuntimeError("refused")
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        return exc
    return None


def _fail(client, bucket, key, stage, exc=None):
    _report(stage, exc)
    if client is not None and _is_probe_key(key):
        cleanup_exc = _delete_exact(client, bucket, key)
        if cleanup_exc is not None:
            _report("cleanup", cleanup_exc)
    return 1


def main():
    _quiet_aws_loggers()
    bucket = _bucket()
    if bucket is None:
        return 1

    key = _probe_key()
    if not _is_probe_key(key):
        _report("put")
        return 1

    try:
        client = create_s3_client()
    except Exception as exc:
        _report("put", exc)
        return 1

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=_PROBE_BODY,
            ContentType="text/plain",
        )
    except Exception as exc:
        return _fail(client, bucket, key, "put", exc)

    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        return _fail(client, bucket, key, "head", exc)
    if head.get("ContentLength") != len(_PROBE_BODY):
        return _fail(client, bucket, key, "head")

    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        return _fail(client, bucket, key, "delete", exc)

    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _is_missing(exc):
            print("S3 connectivity check passed: put, head, delete")
            return 0
        return _fail(client, bucket, key, "head", exc)

    return _fail(client, bucket, key, "delete")


if __name__ == "__main__":
    sys.exit(main())

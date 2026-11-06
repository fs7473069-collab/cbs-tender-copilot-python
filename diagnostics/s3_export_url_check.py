"""Presigned download check for two tiny export objects.

Run later from the repo root:

    python -m diagnostics.s3_export_url_check

Uploads tender-export.xlsx and tender-export.docx under
connectivity-tests/<UTC timestamp>-<random>/, presigns each for 60
seconds, downloads the objects, and deletes those two keys. Does not
list the bucket or touch any other key.
"""

from __future__ import annotations

import logging
import secrets
import sys
import urllib.request
from datetime import datetime, timezone

from app.messaging.export_consumer import (
    DOCX_CONTENT_TYPE,
    XLSX_CONTENT_TYPE,
    default_bucket,
)
from app.storage.s3_client import (
    create_presigned_download_url,
    create_s3_client,
    upload_bytes,
)

_EXCEL_BODY = b"excel-export-url-probe"
_WORD_BODY = b"word-export-url-probe"
_PREFIX = "connectivity-tests"
_EXCEL_NAME = "tender-export.xlsx"
_WORD_NAME = "tender-export.docx"
_OBJECT_NAMES = frozenset({_EXCEL_NAME, _WORD_NAME})
_DOWNLOAD_TIMEOUT_SECONDS = 30


class CheckFailed(Exception):
    """Local probe failure. The message is never printed."""

    def __str__(self):
        return ""


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


def _report(stage, exc):
    print(
        f"S3 export URL check failed: {stage} ({type(exc).__name__})",
        file=sys.stderr,
    )


def _export_bucket():
    resolved = default_bucket()
    if not isinstance(resolved, tuple) or not resolved:
        return None
    bucket = resolved[0]
    if not isinstance(bucket, str) or not bucket.strip():
        return None
    return bucket.strip()


def _run_folder():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(16)}"


def _is_run_key(key, folder):
    if not isinstance(key, str) or not isinstance(folder, str):
        return False
    parts = key.split("/")
    if len(parts) != 3:
        return False
    prefix, middle, name = parts
    if prefix != _PREFIX or middle != folder or name not in _OBJECT_NAMES:
        return False
    if not middle or middle in {".", ".."} or "/" in middle or "\\" in middle:
        return False
    return True


def _download_exact(url, expected):
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(
        request, timeout=_DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        status = response.getcode()
        body = response.read(len(expected) + 1)
    if status != 200 or body != expected:
        raise CheckFailed()
    return body


def _delete_uploaded(client, bucket, folder, uploaded, allowed):
    if not uploaded:
        return None
    if client is None:
        return CheckFailed()
    allowed_keys = set(allowed)
    first_error = None
    for key in tuple(uploaded):
        if key not in allowed_keys or not _is_run_key(key, folder):
            if first_error is None:
                first_error = CheckFailed()
            continue
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _run_probes(
    bucket,
    folder,
    probes,
    uploaded,
    uploaded_labels,
    presigned_labels,
    downloaded_labels,
):
    """Upload, presign, and download. Returns (failure, client). Never raises."""
    client = None
    stage = "upload excel"
    try:
        try:
            client = create_s3_client()
        except Exception as exc:
            return ("upload excel", exc), client

        urls = {}
        for label, key, body, content_type in probes:
            stage = f"upload {label}"
            if not _is_run_key(key, folder):
                return (stage, CheckFailed()), client
            try:
                upload_bytes(body, bucket, key, content_type)
            except Exception as exc:
                return (stage, exc), client
            uploaded.append(key)
            uploaded_labels.append(label)
            print(f"upload {label}: pass {len(body)} bytes")

        for label, key, _body, _content_type in probes:
            stage = f"presign {label}"
            if not _is_run_key(key, folder):
                return (stage, CheckFailed()), client
            try:
                url = create_presigned_download_url(bucket, key, 60)
            except Exception as exc:
                return (stage, exc), client
            if not isinstance(url, str) or not url.strip():
                return (stage, CheckFailed()), client
            urls[label] = url
            presigned_labels.append(label)
            print(f"presign {label}: pass")

        for label, key, body, _content_type in probes:
            stage = f"download {label}"
            if not _is_run_key(key, folder):
                return (stage, CheckFailed()), client
            try:
                downloaded = _download_exact(urls[label], body)
            except Exception as exc:
                return (stage, exc), client
            downloaded_labels.append(label)
            print(f"download {label}: pass {len(downloaded)} bytes")

        return None, client
    except Exception as exc:
        return (stage, exc), client


def main():
    _quiet_aws_loggers()

    try:
        bucket = _export_bucket()
    except Exception as exc:
        _report("upload excel", exc)
        return 1
    if bucket is None:
        print("S3_EXPORT_BUCKET and S3_BUCKET are not set", file=sys.stderr)
        return 1

    folder = _run_folder()
    excel_key = f"{_PREFIX}/{folder}/{_EXCEL_NAME}"
    word_key = f"{_PREFIX}/{folder}/{_WORD_NAME}"
    probes = (
        ("excel", excel_key, _EXCEL_BODY, XLSX_CONTENT_TYPE),
        ("word", word_key, _WORD_BODY, DOCX_CONTENT_TYPE),
    )
    for label, key, _body, _content_type in probes:
        if not _is_run_key(key, folder):
            _report(f"upload {label}", CheckFailed())
            return 1

    uploaded = []
    uploaded_labels = []
    presigned_labels = []
    downloaded_labels = []
    allowed = (excel_key, word_key)
    client = None
    failure = None
    cleanup_error = None

    try:
        try:
            failure, client = _run_probes(
                bucket,
                folder,
                probes,
                uploaded,
                uploaded_labels,
                presigned_labels,
                downloaded_labels,
            )
        except Exception as exc:
            failure = ("upload excel", exc)
        if failure is not None:
            _report(failure[0], failure[1])
    finally:
        try:
            cleanup_error = _delete_uploaded(
                client, bucket, folder, uploaded, allowed
            )
        except Exception as exc:
            cleanup_error = exc
        if uploaded:
            if cleanup_error is None:
                print("cleanup: pass")
            else:
                _report("cleanup", cleanup_error)

    passed = (
        failure is None
        and cleanup_error is None
        and uploaded_labels == ["excel", "word"]
        and presigned_labels == ["excel", "word"]
        and downloaded_labels == ["excel", "word"]
    )
    if passed:
        print("S3 export URL check passed")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

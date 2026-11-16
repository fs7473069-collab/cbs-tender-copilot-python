"""Excel and Word uploads for a completed analysis.

Renders from the in-memory analysis snapshot, uploads both objects to the
private export bucket, and returns presigned GET URLs. Does not call Ollama,
the retriever, or the export worker, and does not write local export files.

Excel is uploaded before Word. If the Word upload or either presigned URL
fails, the exception propagates and the caller must not publish. An Excel
object already uploaded may remain; the next successful run overwrites the
same keys. Nothing is returned unless both uploads and both URLs succeed.
"""

import os
import re

from app.export.ai_recommendation import recommendation_from_evidence_match
from app.export.excel import render_excel
from app.export.suggested_actions import merge_suggested_actions
from app.export.word import render_word
from app.messaging.export_consumer import (
    DOCX_CONTENT_TYPE,
    EXPORT_BUCKET_ENV,
    FALLBACK_BUCKET_ENV,
    XLSX_CONTENT_TYPE,
    ExportRequestError,
    default_bucket,
    sanitize_segment,
)
from app.storage.s3_client import create_presigned_download_url, upload_bytes

EXCEL_FILENAME = "tender-export.xlsx"
WORD_FILENAME = "tender-export.docx"
KEY_PREFIX = "exports"
PRESIGNED_URL_EXPIRES_ENV = "S3_PRESIGNED_URL_EXPIRES_SECONDS"
# 4 days. AWS SigV4 allows at most 7 days (604800) when signing with IAM
# user access keys (S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY). Temporary
# STS or role credentials cannot outlive the session; those URLs often
# fail after 12 hours or less even if this value is 345600.
PRESIGNED_URL_EXPIRES_DEFAULT_SECONDS = 345600
EXPORT_FILE_ITEM_KEYS = (
    "fileName",
    "downloadUrl",
    "bucket",
    "key",
    "expiresIn",
)
_PATH_FIELDS = ("organizationId", "workspaceId", "jobId", "analysisId")
_TENDER_FIELDS = (
    "title",
    "customerName",
    "tenderReference",
    "submissionDeadline",
)
_SEPARATORS = re.compile(r"[/\\]")


class LocalExportError(Exception):
    """Analysis export failed. The message never includes tender content, credentials, or URLs."""


def presigned_url_expires_seconds():
    """Positive integer lifetime for analysis download URLs.

    Unset or blank uses the default. Any other value fails the export.
    The invalid text is not included in the error.
    """
    raw = os.getenv(PRESIGNED_URL_EXPIRES_ENV)
    if raw is None or not str(raw).strip():
        return PRESIGNED_URL_EXPIRES_DEFAULT_SECONDS
    text = str(raw).strip()
    if not text.isascii() or not text.isdigit():
        raise LocalExportError(
            f"{PRESIGNED_URL_EXPIRES_ENV} must be a positive integer"
        )
    try:
        value = int(text)
    except ValueError:
        raise LocalExportError(
            f"{PRESIGNED_URL_EXPIRES_ENV} must be a positive integer"
        ) from None
    if value <= 0:
        raise LocalExportError(
            f"{PRESIGNED_URL_EXPIRES_ENV} must be a positive integer"
        )
    return value


def _safe_segment(value):
    """One S3 key segment. ``.`` / ``..`` components are rejected."""
    if not isinstance(value, str) or not value.strip():
        raise LocalExportError("Invalid export path segment")
    raw = value.strip()
    if any(part in ("", ".", "..") for part in _SEPARATORS.split(raw)):
        raise LocalExportError("Invalid export path segment")
    try:
        sanitized = sanitize_segment(raw)
    except ExportRequestError:
        raise LocalExportError("Invalid export path segment") from None
    if (
        not isinstance(sanitized, str)
        or sanitized in ("", ".", "..")
        or _SEPARATORS.search(sanitized)
    ):
        raise LocalExportError("Invalid export path segment")
    return sanitized


def _object_key(response, file_name):
    if not isinstance(response, dict):
        raise LocalExportError("Analysis response must be an object")
    segments = [_safe_segment(response.get(field)) for field in _PATH_FIELDS]
    return "/".join([KEY_PREFIX, *segments, file_name])


def _require_bucket():
    """Same env order as the export worker when output.bucket is absent."""
    bucket, source = default_bucket()
    if not isinstance(bucket, str) or not bucket.strip() or not source:
        raise LocalExportError(
            "No export bucket: set "
            f"{EXPORT_BUCKET_ENV} or {FALLBACK_BUCKET_ENV}"
        )
    return bucket.strip(), source


def _existing_snapshot_drafts(response, result):
    for container in (response, result):
        value = container.get("draftSections") if isinstance(container, dict) else None
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _existing_snapshot_actions(response, result):
    """Java/human actions if already present. Analysis JSON normally has none."""
    for container in (response, result):
        value = container.get("actions") if isinstance(container, dict) else None
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    return []


def snapshot_from_completed_response(response):
    """Map an in-memory analysis result to the renderer snapshot.

    Human decisions and approved drafts stay empty. Suggested actions are
    derived from evidence status only and are not published on the analysis
    JSON.
    """
    if not isinstance(response, dict):
        raise LocalExportError("Analysis response must be an object")
    result = response.get("result")
    if not isinstance(result, dict):
        raise LocalExportError("Analysis result must be an object")
    raw_requirements = result.get("requirements")
    if not isinstance(raw_requirements, list):
        raise LocalExportError("Analysis requirements must be a list")

    filenames_by_id = {}
    for document in response.get("documents") or []:
        if not isinstance(document, dict):
            continue
        document_id = document.get("documentId")
        filename = document.get("filename")
        if isinstance(document_id, str) and document_id.strip() and isinstance(filename, str):
            filenames_by_id[document_id.strip()] = filename

    requirements = []
    for item in raw_requirements:
        if not isinstance(item, dict):
            raise LocalExportError("Analysis requirement must be an object")
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        copied_sources = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            copied = dict(source)
            document_id = copied.get("documentId")
            if (
                not copied.get("filename")
                and isinstance(document_id, str)
                and document_id.strip() in filenames_by_id
            ):
                copied["filename"] = filenames_by_id[document_id.strip()]
            copied_sources.append(copied)
        requirement = {
            "requirementId": item.get("sourceItemKey"),
            "sourceItemKey": item.get("sourceItemKey"),
            "content": item.get("content"),
            "mandatory": item.get("mandatory"),
            "category": item.get("category"),
            "confidence": item.get("confidence"),
            "sources": copied_sources,
        }
        evidence_match = item.get("evidenceMatch")
        if isinstance(evidence_match, dict):
            requirement["evidenceMatch"] = evidence_match
            derived = recommendation_from_evidence_match(evidence_match)
            if derived is not None:
                # Snapshot-only. Never written onto the published analysis JSON.
                requirement["aiRecommendation"] = derived
        requirements.append(requirement)

    return {
        "tender": {field: result.get(field) for field in _TENDER_FIELDS},
        "requirements": requirements,
        "actions": merge_suggested_actions(
            requirements,
            _existing_snapshot_actions(response, result),
        ),
        "draftSections": _existing_snapshot_drafts(response, result),
    }


def _upload_and_presign(data, bucket, key, file_name, content_type, expires_in):
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise LocalExportError("Rendered export is empty")
    payload = bytes(data)
    upload_bytes(payload, bucket, key, content_type)
    # Filename and size only. Never the bucket, key, or URL.
    print(f"Uploaded analysis export {file_name}: {len(payload)} bytes")
    url = create_presigned_download_url(bucket, key, expires_in)
    if not isinstance(url, str) or not url.strip():
        raise LocalExportError("Presigned download URL was empty")
    return {
        "fileName": file_name,
        "downloadUrl": url,
        "bucket": bucket,
        "key": key,
        "expiresIn": expires_in,
    }


def upload_analysis_exports(response):
    """Upload Excel then Word and return durable refs plus a presigned GET.

    Both uploads and both presigned URLs must succeed. A later failure raises
    and does not return a partial list. An object uploaded before that failure
    may remain; the same key is overwritten on the next success.
    """
    excel_key = _object_key(response, EXCEL_FILENAME)
    word_key = _object_key(response, WORD_FILENAME)
    bucket, source = _require_bucket()
    expires_in = presigned_url_expires_seconds()
    snapshot = snapshot_from_completed_response(response)
    # Source name only (S3_EXPORT_BUCKET or S3_BUCKET), never the bucket value.
    print(f"Analysis export bucket source: {source}")

    excel = _upload_and_presign(
        render_excel(snapshot),
        bucket,
        excel_key,
        EXCEL_FILENAME,
        XLSX_CONTENT_TYPE,
        expires_in,
    )
    word = _upload_and_presign(
        render_word(snapshot),
        bucket,
        word_key,
        WORD_FILENAME,
        DOCX_CONTENT_TYPE,
        expires_in,
    )
    return [excel, word]


def _export_file_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise LocalExportError(f"Export {field} is invalid")
    return value


def attach_export_files(response, export_files):
    """Return a copy of the analysis response plus top-level exportFiles.

    Existing analysis keys are kept. Each file entry is fileName, downloadUrl,
    bucket, key, and expiresIn. path and other extra fields are not copied.
    """
    if not isinstance(response, dict):
        raise LocalExportError("Analysis response must be an object")
    if not isinstance(export_files, list) or len(export_files) != 2:
        raise LocalExportError("Export files are incomplete")
    cleaned = []
    for item in export_files:
        if not isinstance(item, dict):
            raise LocalExportError("Export file entry is invalid")
        file_name = item.get("fileName")
        download_url = item.get("downloadUrl")
        if file_name not in (EXCEL_FILENAME, WORD_FILENAME):
            raise LocalExportError("Export file name is invalid")
        expires_in = item.get("expiresIn")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise LocalExportError("Export expiresIn is invalid")
        cleaned.append({
            "fileName": file_name,
            "downloadUrl": _export_file_text(download_url, "download URL"),
            "bucket": _export_file_text(item.get("bucket"), "bucket"),
            "key": _export_file_text(item.get("key"), "key"),
            "expiresIn": expires_in,
        })
    if [item["fileName"] for item in cleaned] != [EXCEL_FILENAME, WORD_FILENAME]:
        raise LocalExportError("Export files are incomplete")
    updated = dict(response)
    updated["exportFiles"] = cleaned
    return updated

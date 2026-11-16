"""Suite-wide isolation for analysis exports.

Analysis completion uploads Excel and Word through mocked S3 helpers. Tests
do not call AWS, and they do not write ``local_data/exports``. The fake
download URL is ``https://download.example.invalid/{key}``.
"""

import pytest

from app.llm.ollama_client import clear_extract_requirements_cache


@pytest.fixture(autouse=True)
def mock_analysis_bid_drafts(monkeypatch):
    """Consumer drafting is mocked; unit tests call the generator directly."""
    monkeypatch.setattr(
        "app.messaging.consumer.generate_draft_sections",
        lambda *args, **kwargs: [],
    )


@pytest.fixture(autouse=True)
def isolate_extract_disk_cache(tmp_path, monkeypatch):
    """Do not write extraction JSON into the shared knowledge-base directory."""
    monkeypatch.setenv(
        "TENDER_EXTRACT_CACHE_PATH",
        str(tmp_path / "extract_cache.json"),
    )
    clear_extract_requirements_cache()


@pytest.fixture(autouse=True)
def mock_analysis_export_s3(monkeypatch):
    """Mock analysis-export S3. The separate export worker is not patched."""
    calls = {
        "bucket": "analysis-export-bucket",
        "uploads": [],
        "presigns": [],
    }

    def upload_bytes(data, bucket, key, content_type):
        calls["uploads"].append({
            "data": data,
            "bucket": bucket,
            "key": key,
            "content_type": content_type,
        })
        return {
            "bucket": bucket,
            "key": key,
            "contentType": content_type,
            "sizeBytes": len(data),
            "sha256": "0" * 64,
        }

    def create_presigned_download_url(bucket, key, expires_in):
        calls["presigns"].append({
            "bucket": bucket,
            "key": key,
            "expires_in": expires_in,
        })
        return f"https://download.example.invalid/{key}"

    monkeypatch.setattr("app.export.local_export.upload_bytes", upload_bytes)
    monkeypatch.setattr(
        "app.export.local_export.create_presigned_download_url",
        create_presigned_download_url,
    )
    monkeypatch.setattr(
        "app.export.local_export.default_bucket",
        lambda: (calls["bucket"], "S3_EXPORT_BUCKET"),
    )
    monkeypatch.delenv("S3_PRESIGNED_URL_EXPIRES_SECONDS", raising=False)
    return calls

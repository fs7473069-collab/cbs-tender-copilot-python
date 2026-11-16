import hashlib
import os

import boto3
from dotenv import load_dotenv

load_dotenv()


def create_s3_client():
    return boto3.client(
        "s3",
        region_name=os.getenv("S3_REGION"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
    )
def download_file(bucket, key, local_path):
    s3 = create_s3_client()

    s3.download_file(
        Bucket=bucket,
        Key=key,
        Filename=local_path,
    )

    return local_path

def upload_file(local_path, bucket, key):
    if not os.path.isfile(local_path):
        raise FileNotFoundError(
            f"Report file not found: {local_path}"
        )

    s3 = create_s3_client()

    s3.upload_file(
        Filename=local_path,
        Bucket=bucket,
        Key=key,
    )

    return key


def upload_bytes(data, bucket, key, content_type):
    """Upload an in-memory object; returns bucket, key, size and SHA-256."""
    if not bucket or not key:
        raise ValueError("S3 bucket and key are required for upload")

    s3 = create_s3_client()

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )

    return {
        "bucket": bucket,
        "key": key,
        "contentType": content_type,
        "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def create_presigned_download_url(bucket, key, expires_in):
    """Presigned GET for a private object.

    Does not set a canned ACL, public-read, or object ACL. The bucket stays
    private; the URL is the only access, and only until it expires.

    AWS SigV4 allows at most 7 days (604800 seconds) when signing with IAM
    user access keys. This client uses S3_ACCESS_KEY_ID and
    S3_SECRET_ACCESS_KEY and does not send a session token. If those values
    are temporary STS credentials, S3 will reject the URL when the session
    ends, which can be far sooner than expires_in.
    """
    if not bucket or not key:
        raise ValueError("S3 bucket and key are required for a download URL")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
    ):
        raise ValueError("Presigned URL expiry must be a positive integer")

    s3 = create_s3_client()
    url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Presigned download URL was empty")
    return url

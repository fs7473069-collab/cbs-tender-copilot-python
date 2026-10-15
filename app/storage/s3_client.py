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
   
   
  
  
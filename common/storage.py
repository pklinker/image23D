import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from common.settings import settings


def _client(endpoint_url: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


# Server-to-server calls (inside the compose network) use the internal endpoint.
client = _client(settings.s3_endpoint_url)

# Presigned URLs are handed to a browser outside the compose network, so they
# must be signed for -- and point at -- the endpoint the browser can reach.
_presign_client = _client(settings.s3_public_endpoint_url)


def ensure_bucket() -> None:
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=settings.s3_bucket)


def presigned_put_url(object_key: str, content_type: str = "application/octet-stream") -> str:
    return _presign_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key, "ContentType": content_type},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )


def presigned_get_url(object_key: str) -> str:
    return _presign_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )


def upload_file(local_path: str, object_key: str) -> None:
    client.upload_file(local_path, settings.s3_bucket, object_key)


def download_file(object_key: str, local_path: str) -> None:
    client.download_file(settings.s3_bucket, object_key, local_path)


def object_exists(object_key: str) -> bool:
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=object_key)
        return True
    except ClientError:
        return False

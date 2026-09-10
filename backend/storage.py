from functools import lru_cache
import hashlib

import boto3
from botocore.config import Config

from config import settings


@lru_cache(maxsize=1)
def client():
    if not all((settings.R2_ENDPOINT_URL,settings.R2_ACCESS_KEY_ID,settings.R2_SECRET_ACCESS_KEY,settings.R2_BUCKET_NAME)):
        raise RuntimeError('R2 storage must be configured')
    return boto3.client('s3',endpoint_url=settings.R2_ENDPOINT_URL,aws_access_key_id=settings.R2_ACCESS_KEY_ID,aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,region_name='auto',config=Config(signature_version='s3v4',connect_timeout=10,read_timeout=60,retries={'max_attempts':3},request_checksum_calculation='when_required',response_checksum_validation='when_required'))


def object_key(folder, team_id, identifier, name):
    return f'{folder}/{settings.R2_PREFIX}{team_id}/{identifier}/{name}'


def put(key, content, mime_type):
    client().put_object(Bucket=settings.R2_BUCKET_NAME,Key=key,Body=content,ContentType=mime_type,CacheControl='private, no-store',Metadata={'sha256':hashlib.sha256(content).hexdigest()})
    return key


def read(key):
    response = client().get_object(Bucket=settings.R2_BUCKET_NAME,Key=key)
    try:
        return response['Body'].read()
    finally:
        response['Body'].close()


def stream(key):
    response = client().get_object(Bucket=settings.R2_BUCKET_NAME,Key=key)
    try:
        yield from response['Body'].iter_chunks(chunk_size=64*1024)
    finally:
        response['Body'].close()


def delete(key):
    client().delete_object(Bucket=settings.R2_BUCKET_NAME,Key=key)

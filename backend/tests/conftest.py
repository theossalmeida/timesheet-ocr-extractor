from uuid import uuid4

import os
import storage

import psycopg
import pytest
from psycopg import sql
from fastapi.testclient import TestClient

from config import settings
from database import initialize, pool
from main import app


@pytest.fixture(scope='session', autouse=True)
def isolated_database():
    schema = 'autus_test_' + uuid4().hex
    settings.R2_PREFIX = schema + '/'
    objects = {}
    patch = pytest.MonkeyPatch()
    if os.environ.get('AUTUS_R2_TEST') != '1':
        patch.setattr(storage, 'put', lambda key, content, mime: objects.__setitem__(key,content) or key)
        patch.setattr(storage, 'read', lambda key: objects[key])
        patch.setattr(storage, 'stream', lambda key: iter([objects[key]]))
        patch.setattr(storage, 'delete', lambda key: objects.pop(key,None))
    with psycopg.connect(settings.DATABASE_URL) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    pool.conninfo = settings.DATABASE_URL.replace('-pooler.', '.')
    pool.kwargs['options'] = '-csearch_path=' + schema
    settings.BOOTSTRAP_TOKEN = 'test-bootstrap-private-token'
    settings.COOKIE_SECURE = False
    initialize()
    yield
    pool.close()
    if os.environ.get('AUTUS_R2_TEST') == '1':
        for folder in ('raw_files/','processed_files/'):
            for page in storage.client().get_paginator('list_objects_v2').paginate(Bucket=settings.R2_BUCKET_NAME,Prefix=folder+settings.R2_PREFIX):
                for obj in page.get('Contents',[]):
                    storage.delete(obj['Key'])
    patch.undo()
    with psycopg.connect(settings.DATABASE_URL) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture(scope='session')
def owner(isolated_database):
    client = TestClient(app, headers={'x-autus-request':'1'})
    result = client.post('/auth/register', json={'email':'owner@example.com','name':'Owner','password':'a long secure password','bootstrap_token':settings.BOOTSTRAP_TOKEN})
    assert result.status_code == 200, result.text
    result = client.post('/teams', json={'name':'Primary team'})
    assert result.status_code == 200
    client.headers['x-team-id'] = result.json()['id']
    return client

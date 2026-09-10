from uuid import uuid4

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
    with psycopg.connect(settings.DATABASE_URL) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    pool.conninfo = settings.DATABASE_URL.replace('-pooler.', '.')
    pool.kwargs['options'] = '-csearch_path=' + schema
    settings.BOOTSTRAP_TOKEN = 'test-bootstrap-private-token'
    settings.COOKIE_SECURE = False
    initialize()
    yield
    pool.close()
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

import asyncio
import base64
import json
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from database import pool
from jobs import CHUNK_SIZE


def test_chunk_integrity_and_isolation(owner):
    upload = owner.post('/uploads',json={'filename':'chunked.pdf','mode':'guia','size_bytes':CHUNK_SIZE+5}).json()['id']
    assert owner.put(f'/uploads/{upload}/1',content=b'wrong-size').status_code == 400
    assert owner.put(f'/uploads/{upload}/1',content=b'final').status_code == 200
    assert owner.post(f'/uploads/{upload}/process').status_code == 400
    chunk = b'%PDF'+b'x'*(CHUNK_SIZE-4)
    assert owner.put(f'/uploads/{upload}/0',content=chunk).status_code == 200
    assert owner.put(f'/uploads/{upload}/0',content=chunk).status_code == 200
    other = owner.post('/teams',json={'name':'Upload isolation'}).json()['id']
    assert owner.put(f'/uploads/{upload}/1',content=b'final',headers={'x-team-id':other}).status_code == 404
    with pool.connection() as conn:
        count = conn.execute('SELECT count(*) AS n FROM upload_parts WHERE upload_id=%s',(upload,)).fetchone()['n']
    assert count == 2
    assert owner.delete('/uploads/'+upload).status_code == 200


def test_background_job_survives_logout(owner):
    pdf = b'%PDF test persistent job'
    app.state.limiter._limiter.storage.reset()
    client = TestClient(app,headers=dict(owner.headers),cookies=owner.cookies)
    with patch("main.initialize"), patch("main.pool.close"), client:
        upload = client.post('/uploads',json={'filename':'background.pdf','mode':'cartao','size_bytes':len(pdf)}).json()['id']
        assert client.put(f'/uploads/{upload}/0',content=pdf).status_code == 200

        async def stream(*args):
            await asyncio.sleep(.3)
            yield 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(b'PKdata').decode(),'excel_filename':'result.xlsx','provider':'pdfplumber'})+'\n\n'

        with patch('main.stream_timesheet_extraction',stream):
            result = client.post(f'/uploads/{upload}/process')
            assert result.status_code == 200,result.text
            job_id = result.json()['id']
            assert client.post(f'/uploads/{upload}/process').json()['id'] == job_id
            assert client.post('/auth/logout').status_code == 200
            assert client.post('/auth/login',json={'email':'owner@example.com','password':'a long secure password'}).status_code == 200
            owner.cookies.update(client.cookies)
            for _ in range(40):
                status = client.get('/documents/'+job_id).json()
                if status['status']!='processing':
                    break
                time.sleep(.1)
            assert status['status']=='done',status
            assert status['artifacts'][0]['kind']=='excel'


def test_upload_bounds(owner):
    assert owner.post('/uploads',json={'filename':'too-big.pdf','mode':'cartao','size_bytes':51*1024*1024}).status_code == 413
    result = owner.post('/uploads',json={'filename':'invalid.pdf','mode':'cartao','size_bytes':4})
    upload = result.json()['id']
    assert owner.put(f'/uploads/{upload}/0',content=b'nope').status_code == 400
    assert owner.put(f'/uploads/{upload}/25',content=b'nope').status_code == 400
    assert owner.delete('/uploads/'+upload).status_code == 200

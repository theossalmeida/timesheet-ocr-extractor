import asyncio
import base64
import json
import time
from threading import Event
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
            with patch('jobs.jobs', {object(), object()}):
                assert client.post(f'/uploads/{upload}/process').json()['id'] == job_id
            assert client.delete('/uploads/'+upload).status_code == 200
            with pool.connection() as conn:
                assert str(conn.execute('SELECT extraction_id FROM uploads WHERE id=%s', (upload,)).fetchone()['extraction_id']) == job_id
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


def test_failed_job_is_discarded_and_still_reported_to_the_poller(owner):
    pdf = b'%PDF test discarded job'
    app.state.limiter._limiter.storage.reset()
    client = TestClient(app,headers=dict(owner.headers),cookies=owner.cookies)
    with patch("main.initialize"), patch("main.pool.close"), client:
        upload = client.post('/uploads',json={'filename':'descartado.pdf','mode':'guia','size_bytes':len(pdf)}).json()['id']
        assert client.put(f'/uploads/{upload}/0',content=pdf).status_code == 200

        async def stream(*args):
            yield 'data: '+json.dumps({'type':'error','message':'Nenhum registro encontrado nas guias ministeriais.'})+'\n\n'

        with patch('main.stream_guia_extraction',stream):
            job_id = client.post(f'/uploads/{upload}/process').json()['id']
            for _ in range(40):
                status = client.get('/documents/'+job_id).json()
                if status['status'] != 'processing':
                    break
                time.sleep(.1)

    # The poller still learns it failed, even though nothing was kept.
    assert status['status'] == 'failed'
    assert status['artifacts'] == []
    with pool.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM extractions WHERE id=%s',(job_id,)).fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM uploads WHERE id=%s',(upload,)).fetchone()['n'] == 0
    assert owner.get('/documents',params={'q':'descartado.pdf'}).json()['documents'] == []


def test_duplicate_upload_reuses_the_existing_result(owner):
    app.state.limiter._limiter.storage.reset()
    pdf = b'%PDF idempotent guia'
    calls = []

    async def stream(*args):
        calls.append(args)
        yield 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(b'PKidempotent').decode(),'excel_filename':'r.xlsx','provider':'gemini-guia','rows_extracted':1,'ai_usage':[]})+'\n\n'

    with patch('main.stream_guia_extraction',stream):
        first_upload = owner.post('/uploads',json={'filename':'guia.pdf','mode':'guia','size_bytes':len(pdf)}).json()['id']
        assert owner.put(f'/uploads/{first_upload}/0',content=pdf).status_code == 200
        first_id = owner.post(f'/uploads/{first_upload}/process').json()['id']
        status = {}
        for _ in range(40):
            status = owner.get('/documents/'+first_id).json()
            if status['status'] != 'processing':
                break
            time.sleep(.1)
        assert status['status'] == 'done', status

        second_upload = owner.post('/uploads',json={'filename':'guia-de-novo.pdf','mode':'guia','size_bytes':len(pdf)}).json()['id']
        assert owner.put(f'/uploads/{second_upload}/0',content=pdf).status_code == 200
        second_id = owner.post(f'/uploads/{second_upload}/process').json()['id']

    assert len(calls) == 1  # the pipeline never ran a second time
    assert second_id == first_id  # the second upload points at the same document
    result = owner.get('/documents/'+second_id).json()
    assert result['status'] == 'done'
    assert result['artifacts'] == status['artifacts']
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM extractions WHERE filename IN ('guia.pdf','guia-de-novo.pdf')").fetchone()['n']
    assert n == 1  # no new history row was created for the duplicate


def test_duplicate_detection_is_scoped_to_team_and_mode(owner):
    app.state.limiter._limiter.storage.reset()
    pdf = b'%PDF idempotent scope check'

    async def stream(*args):
        yield 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(b'PKscoped').decode(),'excel_filename':'r.xlsx','provider':'gemini-guia','rows_extracted':1,'ai_usage':[]})+'\n\n'

    with patch('main.stream_guia_extraction',stream):
        upload = owner.post('/uploads',json={'filename':'a.pdf','mode':'guia','size_bytes':len(pdf)}).json()['id']
        assert owner.put(f'/uploads/{upload}/0',content=pdf).status_code == 200
        original_id = owner.post(f'/uploads/{upload}/process').json()['id']
        for _ in range(40):
            if owner.get('/documents/'+original_id).json()['status'] != 'processing':
                break
            time.sleep(.1)

    other_team = owner.post('/teams',json={'name':'Dedupe isolation'}).json()['id']
    with patch('main.stream_guia_extraction',stream):
        upload = owner.post('/uploads',json={'filename':'a.pdf','mode':'guia','size_bytes':len(pdf)},headers={'x-team-id':other_team}).json()['id']
        assert owner.put(f'/uploads/{upload}/0',content=pdf,headers={'x-team-id':other_team}).status_code == 200
        other_team_id = owner.post(f'/uploads/{upload}/process',headers={'x-team-id':other_team}).json()['id']
    assert other_team_id != original_id  # another team's identical file is not the same document


def test_reuploads_rejoin_running_job_even_when_queue_is_full(owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from documents import content_hash, find_duplicate

    app.state.limiter._limiter.storage.reset()
    pdf = b'%PDF refresh while processing'
    release = Event()
    started = Event()
    calls = []
    client = TestClient(app, headers=dict(owner.headers), cookies=owner.cookies)

    async def stream(*args):
        calls.append(args)
        started.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        yield 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(b'PKrefresh').decode(),'excel_filename':'r.xlsx','provider':'pdfplumber'})+'\n\n'

    def upload_file(filename, content=pdf):
        result = client.post('/uploads', json={'filename':filename,'mode':'guia','size_bytes':len(content)})
        assert result.status_code == 200, result.text
        upload_id = result.json()['id']
        assert client.put(f'/uploads/{upload_id}/0', content=content).status_code == 200
        return upload_id

    with patch('main.initialize'), patch('main.pool.close'), client, patch('main.stream_guia_extraction', stream):
        try:
            original_upload = upload_file('original.pdf')
            original_id = client.post(f'/uploads/{original_upload}/process').json()['id']
            assert started.wait(5)
            repeated_uploads = [upload_file('renamed.pdf'), upload_file('original.pdf')]
            with patch('jobs.jobs', {object(), object()}), ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda upload_id: client.post(f'/uploads/{upload_id}/process'), repeated_uploads))
                assert all(response.status_code == 200 for response in responses)
                assert [response.json()['id'] for response in responses] == [original_id, original_id]
                changed_upload = upload_file('original.pdf', b'%PDF changed content')
                assert client.post(f'/uploads/{changed_upload}/process').status_code == 429
                assert client.delete(f'/uploads/{changed_upload}').status_code == 200
            assert client.get('/documents/'+original_id).json()['status'] == 'processing'
            with pool.connection() as conn:
                assert conn.execute('SELECT count(*) AS n FROM upload_parts WHERE upload_id=ANY(%s::uuid[])', (repeated_uploads,)).fetchone()['n'] == 0
                assert conn.execute('SELECT count(*) AS n FROM extractions WHERE content_hash=%s', (content_hash(pdf),)).fetchone()['n'] == 1
                assert find_duplicate(conn, owner.headers['x-team-id'], 'cartao', content_hash(pdf), include_processing=True) is None
        finally:
            release.set()
        for _ in range(50):
            status = client.get('/documents/'+original_id).json()
            if status['status'] != 'processing':
                break
            time.sleep(.1)
        assert status['status'] == 'done', status
    assert len(calls) == 1


def test_a_document_waiting_its_turn_is_reported_as_queued(owner):
    """Only one document is processed at a time, so the ones behind it must say
    they are waiting - the row reads 'processing' from the moment it is created,
    which used to make a queued document look like one being worked on."""

    first, second = b'%PDF first in line', b'%PDF second in line'
    app.state.limiter._limiter.storage.reset()
    client = TestClient(app, headers=dict(owner.headers), cookies=owner.cookies)
    with patch("main.initialize"), patch("main.pool.close"), client:
        uploads = []
        for name, content in (('primeiro.pdf', first), ('segundo.pdf', second)):
            upload = client.post('/uploads', json={'filename': name, 'mode': 'cartao', 'size_bytes': len(content)}).json()['id']
            assert client.put(f'/uploads/{upload}/0', content=content).status_code == 200
            uploads.append(upload)

        # consume_job runs the stream in its own event loop on another thread,
        # so the gate has to be a threading primitive, not an asyncio one.
        release = Event()
        started = Event()

        async def stream(*args):
            started.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            yield 'data: ' + json.dumps({'type': 'done', 'excel_b64': base64.b64encode(b'PKdata').decode(), 'excel_filename': 'r.xlsx', 'provider': 'pdfplumber'}) + '\n\n'

        with patch('main.stream_timesheet_extraction', stream):
            running = client.post(f'/uploads/{uploads[0]}/process').json()['id']
            assert started.wait(5)
            waiting = client.post(f'/uploads/{uploads[1]}/process').json()['id']
            assert running != waiting

            for _ in range(40):
                if client.get('/documents/' + waiting).json()['progress'].get('phase') == 'queued':
                    break
                time.sleep(.05)

            status = client.get('/documents/' + waiting).json()
            assert status['progress']['phase'] == 'queued', status
            assert status['progress']['message'] == 'Aguardando processamento...'

            listed = {d['id']: d['status'] for d in client.get('/documents', params={'q': '.pdf'}).json()['documents']}
            assert listed[waiting] == 'queued', listed
            assert listed[running] == 'processing', listed

            release.set()
            for _ in range(60):
                if client.get('/documents/' + waiting).json()['status'] == 'done':
                    break
                time.sleep(.1)

        # Once it gets the slot it is no longer queued anywhere.
        assert client.get('/documents/' + waiting).json()['status'] == 'done'
        listed = {d['id']: d['status'] for d in client.get('/documents', params={'q': '.pdf'}).json()['documents']}
        assert 'queued' not in listed.values()

import base64
import json
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import storage
from database import pool
from main import app


@pytest.mark.parametrize('route,factory,mode', [('/extract/stream','stream_timesheet_extraction','cartao'),('/extract/guia','stream_guia_extraction','guia'),('/contracheque','stream_contracheque_extraction','contracheque'),('/contracheque/horas-extras','stream_contracheque_extra_hours_extraction','horas_extras'),('/extract/frequencia','stream_frequency_cycle_extraction','frequencia')])
def test_persist_and_isolate_all_modes(owner, route, factory, mode):
    app.state.limiter._limiter.storage.reset()
    content = b'PKgenerated spreadsheet'
    pdf = b'%PDF test original'

    async def stream(*args):
        yield 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(content).decode(),'excel_filename':'result.xlsx','provider':'pdfplumber','rows_extracted':3})+'\n\n'

    with patch('main.'+factory,stream):
        response = owner.post(route,files={'file':('private.pdf',pdf,'application/pdf')})
    event = json.loads(response.text.split('data: ')[1])
    assert event['type'] == 'done'
    rows = owner.get('/documents',params={'q':'private.pdf'}).json()['documents']
    row = next(r for r in rows if r['id']==event['extraction_id'])
    assert row['mode'] == mode
    assert row['status'] == 'done'
    assert row['cost_brl'] == 0
    for artifact in row['artifacts']:
        result = owner.get('/documents/files/'+artifact['id'])
        assert result.status_code == 200
        assert result.content == (pdf if artifact['kind']=='original' else content)
        assert 'attachment' in result.headers['content-disposition']
    outsider = TestClient(app,headers={'x-autus-request':'1'})
    assert outsider.get('/documents/files/'+row['artifacts'][0]['id']).status_code == 401
    original_team = owner.headers['x-team-id']
    other_team = owner.post('/teams',json={'name':'Another team'}).json()['id']
    assert owner.get('/documents/files/'+row['artifacts'][0]['id'],headers={'x-team-id':other_team}).status_code == 404
    assert owner.get('/documents',headers={'x-team-id':other_team}).json()['documents'] == []
    assert owner.headers['x-team-id'] == original_team


def test_failed_generation_discards_the_run(owner):
    app.state.limiter._limiter.storage.reset()

    async def broken(*args):
        yield 'data: {"type":"done","excel_b64":"not-base64","excel_filename":"bad.xlsx"}\n\n'

    with patch('main.stream_timesheet_extraction',broken):
        response = owner.post('/extract/stream',files={'file':('broken.pdf',b'%PDF broken','application/pdf')})
    assert '"type": "error"' in response.text
    # A run that produced nothing keeps nothing: no history row, no stored bytes.
    assert owner.get('/documents',params={'q':'broken.pdf'}).json()['documents'] == []
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM extractions WHERE filename='broken.pdf'").fetchone()['n'] == 0


def _done_event(ai_usage):
    return 'data: '+json.dumps({'type':'done','excel_b64':base64.b64encode(b'PKspreadsheet').decode(),'excel_filename':'result.xlsx','provider':'gemini','rows_extracted':4,'ai_usage':ai_usage})+'\n\n'


def _call(**overrides):
    return {'provider':'gemini','model':'gemini-3.1-pro-preview','kind':'extract','metered':True,'prompt_tokens':1000,'cached_tokens':0,'output_tokens':500,'thought_tokens':100,'total_tokens':1600,**overrides}


def test_gemini_document_costs_the_sum_of_its_calls(owner, monkeypatch):
    app.state.limiter._limiter.storage.reset()
    monkeypatch.setattr('services.fx.usd_brl',lambda: 5.0)

    async def stream(*args):
        yield _done_event([_call(),_call(kind='normalize',prompt_tokens=2000,output_tokens=0,thought_tokens=0,total_tokens=2000)])

    with patch('main.stream_timesheet_extraction',stream):
        response = owner.post('/extract/stream',files={'file':('metered.pdf',b'%PDF metered','application/pdf')})
    event = json.loads(response.text.split('data: ')[-1])
    assert 'ai_usage' not in event

    with pool.connection() as conn:
        rows = conn.execute('SELECT * FROM ai_usage WHERE extraction_id=%s ORDER BY prompt_tokens',(UUID(event['extraction_id']),)).fetchall()
    assert [row['kind'] for row in rows] == ['extract','normalize']
    assert [row['prompt_tokens'] for row in rows] == [1000,2000]
    assert [row['output_tokens'] for row in rows] == [500,0]
    # 1000 input + 500 output tokens at $2/$12 per 1M, then 2000 input tokens.
    assert float(rows[0]['cost_usd']) == pytest.approx(0.008)
    assert float(rows[1]['cost_usd']) == pytest.approx(0.004)
    assert [float(row['usd_brl_rate']) for row in rows] == [5.0,5.0]

    document = owner.get('/documents',params={'q':'metered.pdf'}).json()['documents'][0]
    assert float(document['cost_brl']) == pytest.approx(0.012*5.0)


def test_unpriceable_call_leaves_the_cost_unknown(owner, monkeypatch):
    app.state.limiter._limiter.storage.reset()
    monkeypatch.setattr('services.fx.usd_brl',lambda: 5.0)

    async def stream(*args):
        yield _done_event([_call(),_call(metered=False,prompt_tokens=0,output_tokens=0,thought_tokens=0,total_tokens=0)])

    with patch('main.stream_timesheet_extraction',stream):
        response = owner.post('/extract/stream',files={'file':('unmetered.pdf',b'%PDF unmetered','application/pdf')})
    event = json.loads(response.text.split('data: ')[-1])

    with pool.connection() as conn:
        rows = conn.execute('SELECT metered,cost_usd FROM ai_usage WHERE extraction_id=%s ORDER BY metered',(UUID(event['extraction_id']),)).fetchall()
    assert [row['metered'] for row in rows] == [False,True]
    assert rows[0]['cost_usd'] is None

    document = owner.get('/documents',params={'q':'unmetered.pdf'}).json()['documents'][0]
    assert document['cost_brl'] is None


def test_failed_run_leaves_nothing_behind(owner, monkeypatch):
    app.state.limiter._limiter.storage.reset()
    removed = []
    original = storage.delete
    monkeypatch.setattr(storage,'delete',lambda key: removed.append(key) or original(key))

    async def broken(*args):
        yield 'data: '+json.dumps({'type':'error','message':'falhou'})+'\n\n'

    with patch('main.stream_timesheet_extraction',broken):
        owner.post('/extract/stream',files={'file':('apagado.pdf',b'%PDF apagado','application/pdf')})

    assert any('raw_files' in key for key in removed)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM extractions WHERE filename='apagado.pdf'").fetchone()['n'] == 0
        assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE filename='apagado.pdf'").fetchone()['n'] == 0


def test_failed_runs_are_excluded_from_history_and_the_monthly_summary(owner):
    app.state.limiter._limiter.storage.reset()
    before = owner.get('/documents').json()['summary']

    async def broken(*args):
        yield 'data: '+json.dumps({'type':'error','message':'falhou'})+'\n\n'

    with patch('main.stream_timesheet_extraction',broken):
        owner.post('/extract/stream',files={'file':('descartado.pdf',b'%PDF descartado','application/pdf')})

    history = owner.get('/documents',params={'q':'descartado.pdf'}).json()
    assert history['documents'] == []
    assert owner.get('/documents').json()['summary']['documents'] == before['documents']
    assert owner.get('/documents').json()['summary']['unknown_costs'] == before['unknown_costs']

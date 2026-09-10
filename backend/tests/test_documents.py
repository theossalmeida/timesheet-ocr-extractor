import base64
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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


def test_failed_generation_retains_original(owner):
    app.state.limiter._limiter.storage.reset()

    async def broken(*args):
        yield 'data: {"type":"done","excel_b64":"not-base64","excel_filename":"bad.xlsx"}\n\n'

    with patch('main.stream_timesheet_extraction',broken):
        response = owner.post('/extract/stream',files={'file':('broken.pdf',b'%PDF broken','application/pdf')})
    assert '"type": "error"' in response.text
    row = owner.get('/documents',params={'q':'broken.pdf'}).json()['documents'][0]
    assert row['status'] == 'interrupted'
    assert [a['kind'] for a in row['artifacts']] == ['original']

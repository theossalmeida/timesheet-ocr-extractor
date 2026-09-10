import asyncio
import base64
import json
import logging
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from database import pool
from security import team, user

router = APIRouter()
logger = logging.getLogger(__name__)
processing_lock = asyncio.Lock()
EXCEL_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def safe_filename(value):
    return value.replace('\\', '/').split('/')[-1].replace('\r', '').replace('\n', '').replace('\x00', '')[:200] or 'documento.pdf'


def create_extraction(request, filename, mode, content):
    selected = team(request)
    extraction_id = uuid4()
    filename = safe_filename(filename or 'documento.pdf')
    with pool.connection() as conn:
        conn.execute("INSERT INTO extractions(id,team_id,user_id,filename,mode,status) VALUES (%s,%s,%s,%s,%s,'processing')", (extraction_id,selected['team_id'],user(request)['id'],filename,mode))
        conn.execute("INSERT INTO artifacts(id,extraction_id,kind,filename,mime_type,size_bytes,content) VALUES (%s,%s,'original',%s,'application/pdf',%s,%s)", (uuid4(),extraction_id,filename,len(content),content))
    return extraction_id


def finish_extraction(extraction_id, event):
    artifacts = []
    with pool.connection() as conn:
        for kind in ('excel','csv'):
            if event.get(kind+'_b64'):
                content = base64.b64decode(event[kind+'_b64'], validate=True)
                filename = safe_filename(event[kind+'_filename'])
                mime = EXCEL_MIME if kind == 'excel' else event.get('csv_mime','text/csv')
                artifact_id = uuid4()
                conn.execute("INSERT INTO artifacts(id,extraction_id,kind,filename,mime_type,size_bytes,content) VALUES (%s,%s,%s,%s,%s,%s,%s)", (artifact_id,extraction_id,kind,filename,mime,len(content),content))
                artifacts.append({'id':str(artifact_id),'kind':kind,'filename':filename})
        if not artifacts:
            raise ValueError('No generated artifacts')
        provider = event.get('provider','unknown')
        cost = None if any(name in provider.lower() for name in ('gemini','mistral','unknown')) else 0
        conn.execute("UPDATE extractions SET status='done', provider=%s,row_count=%s,cost_brl=%s,completed_at=now() WHERE id=%s", (provider,event.get('rows_extracted',event.get('months_extracted',0)),cost,extraction_id))
    return artifacts


def fail_extraction(extraction_id, status='failed'):
    with pool.connection() as conn:
        conn.execute("UPDATE extractions SET status=%s,error='Não foi possível concluir o processamento. Tente novamente.',completed_at=now() WHERE id=%s AND status='processing'", (status,extraction_id))


async def stored_stream(request, pdf_bytes, filename, mode, factory):
    extraction_id = await asyncio.to_thread(create_extraction,request,filename,mode,pdf_bytes)
    completed = False
    try:
        while processing_lock.locked():
            yield 'data: '+json.dumps({'type':'progress','chunk':0,'total':1,'message':'Aguardando outro documento terminar.'})+'\n\n'
            await asyncio.sleep(2)
        async with processing_lock:
            async for chunk in factory():
                for line in chunk.splitlines():
                    if not line.startswith('data: '):
                        continue
                    event = json.loads(line[6:])
                    if event.get('type') == 'done':
                        artifacts = await asyncio.to_thread(finish_extraction,extraction_id,event)
                        completed = True
                        event['extraction_id'] = str(extraction_id)
                        event['artifacts'] = artifacts
                        if request.headers.get('x-autus-artifacts') == '1':
                            event.pop('excel_b64', None)
                            event.pop('csv_b64', None)
                        chunk = 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                    elif event.get('type') == 'error':
                        await asyncio.to_thread(fail_extraction,extraction_id)
                        completed = True
                yield chunk
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Extraction failed: %s',extraction_id)
        yield 'data: '+json.dumps({'type':'error','message':'Não foi possível processar ou salvar o documento. Tente novamente.'})+'\n\n'
    finally:
        if not completed:
            await asyncio.shield(asyncio.to_thread(fail_extraction,extraction_id,'interrupted'))


@router.get('/documents')
def history(request: Request, q: str = Query(default='',max_length=200), offset: int = Query(default=0,ge=0), limit: int = Query(default=30,ge=1,le=100)):
    selected = team(request)
    with pool.connection() as conn:
        rows = conn.execute("SELECT e.id,e.filename,e.mode,e.status,e.provider,e.row_count,e.cost_brl,e.error,e.created_at,u.name AS user_name FROM extractions e JOIN users u ON u.id=e.user_id WHERE e.team_id=%s AND (e.filename ILIKE %s OR e.mode ILIKE %s) ORDER BY e.created_at DESC,e.id LIMIT %s OFFSET %s", (selected['team_id'],'%'+q+'%','%'+q+'%',limit+1,offset)).fetchall()
        visible = rows[:limit]
        artifacts = conn.execute("SELECT id,extraction_id,kind,filename,size_bytes FROM artifacts WHERE extraction_id=ANY(%s)", ([r['id'] for r in visible],)).fetchall() if visible else []
        for row in visible:
            row['artifacts'] = [a for a in artifacts if a['extraction_id']==row['id']]
        summary = conn.execute("SELECT count(*) AS documents,COALESCE(sum(cost_brl),0) AS known_cost_brl,count(*) FILTER (WHERE cost_brl IS NULL) AS unknown_costs FROM extractions WHERE team_id=%s AND created_at>=date_trunc('month',now() AT TIME ZONE 'America/Sao_Paulo') AT TIME ZONE 'America/Sao_Paulo'", (selected['team_id'],)).fetchone()
    return {'documents':visible,'has_more':len(rows)>limit,'summary':summary}


@router.get('/documents/files/{artifact_id}')
def download(artifact_id: UUID, request: Request):
    selected = team(request)
    with pool.connection() as conn:
        artifact = conn.execute("SELECT a.filename,a.mime_type,a.content FROM artifacts a JOIN extractions e ON e.id=a.extraction_id WHERE a.id=%s AND e.team_id=%s", (artifact_id,selected['team_id'])).fetchone()
    if not artifact:
        raise HTTPException(404,'Arquivo não encontrado.')
    return Response(bytes(artifact['content']),media_type=artifact['mime_type'],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(artifact['filename'],safe=''),'Cache-Control':'no-store'})

import asyncio
import json
import logging
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from database import pool
from documents import fail_extraction, finish_extraction, processing_lock, safe_filename
from security import team, throttle, user

router = APIRouter()
logger = logging.getLogger(__name__)
CHUNK_SIZE = 8 * 1024 * 1024
jobs = set()
job_start_lock = asyncio.Lock()
progress = {}


class UploadStart(BaseModel):
    filename: str = Field(min_length=1,max_length=200)
    mode: Literal['cartao','guia','contracheque','horas_extras','frequencia']
    size_bytes: int = Field(gt=0,le=200*1024*1024)


def owned_upload(conn, upload_id, request):
    current = user(request)
    upload = conn.execute("SELECT u.* FROM uploads u JOIN memberships m ON m.team_id=u.team_id AND m.user_id=u.user_id WHERE u.id=%s AND u.team_id=%s AND u.user_id=%s AND u.expires_at>now() FOR UPDATE OF u", (upload_id,UUID(request.headers['x-team-id']),current['id'])).fetchone()
    if not upload:
        raise HTTPException(404,'Upload não encontrado ou expirado.')
    return upload


@router.post('/uploads')
def start_upload(data: UploadStart, request: Request):
    selected = team(request)
    current = user(request)
    throttle('upload:'+str(current['id']),30)
    if data.mode == 'cartao' and data.size_bytes > 50*1024*1024:
        raise HTTPException(413,'Cartões de ponto devem ter até 50 MB.')
    upload_id = uuid4()
    with pool.connection() as conn:
        conn.execute("DELETE FROM uploads WHERE expires_at<now()")
        conn.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (current['id'],))
        count = conn.execute("SELECT count(*) AS n FROM uploads WHERE user_id=%s AND extraction_id IS NULL", (current['id'],)).fetchone()['n']
        if count >= 3:
            raise HTTPException(429,'Há uploads pendentes. Aguarde ou remova um upload incompleto.')
        conn.execute("INSERT INTO uploads(id,team_id,user_id,filename,mode,size_bytes) VALUES (%s,%s,%s,%s,%s,%s)", (upload_id,selected['team_id'],current['id'],safe_filename(data.filename),data.mode,data.size_bytes))
    return {'id':upload_id,'chunk_size':CHUNK_SIZE}


def store_part(upload_id, part, content, request):
    with pool.connection() as conn:
        upload = owned_upload(conn,upload_id,request)
        expected = min(CHUNK_SIZE,upload['size_bytes']-part*CHUNK_SIZE)
        if upload['extraction_id'] or part<0 or expected<=0 or len(content)!=expected:
            raise HTTPException(400,'Parte do upload inválida.')
        if part == 0 and not content.startswith(b'%PDF'):
            raise HTTPException(400,'Arquivo inválido. Apenas PDFs são aceitos.')
        conn.execute("INSERT INTO upload_parts(upload_id,part,content) VALUES (%s,%s,%s) ON CONFLICT(upload_id,part) DO UPDATE SET content=EXCLUDED.content", (upload_id,part,content))
    return {'ok':True}


@router.put('/uploads/{upload_id}/{part}')
async def upload_part(upload_id: UUID, part: int, request: Request):
    await asyncio.to_thread(team,request)
    data = await request.body()
    return await asyncio.to_thread(store_part,upload_id,part,data,request)


@router.delete('/uploads/{upload_id}')
def discard_upload(upload_id: UUID, request: Request):
    with pool.connection() as conn:
        owned_upload(conn,upload_id,request)
        conn.execute("DELETE FROM uploads WHERE id=%s", (upload_id,))
    return {'ok':True}


def prepare_job(upload_id, request):
    with pool.connection() as conn:
        upload = owned_upload(conn,upload_id,request)
        if upload['extraction_id']:
            return upload['extraction_id'],None,None
        parts = conn.execute("SELECT part,content FROM upload_parts WHERE upload_id=%s ORDER BY part", (upload_id,)).fetchall()
        if [p['part'] for p in parts] != list(range((upload['size_bytes']+CHUNK_SIZE-1)//CHUNK_SIZE)):
            raise HTTPException(400,'O upload está incompleto.')
        content = b''.join(bytes(p['content']) for p in parts)
        if len(content)!=upload['size_bytes']:
            raise HTTPException(400,'Tamanho do upload inválido.')
        extraction_id = uuid4()
        conn.execute("INSERT INTO extractions(id,team_id,user_id,filename,mode,status) VALUES (%s,%s,%s,%s,%s,'processing')", (extraction_id,upload['team_id'],upload['user_id'],upload['filename'],upload['mode']))
        conn.execute("INSERT INTO artifacts(id,extraction_id,kind,filename,mime_type,size_bytes,content) VALUES (%s,%s,'original',%s,'application/pdf',%s,%s)", (uuid4(),extraction_id,upload['filename'],len(content),content))
        conn.execute("UPDATE uploads SET extraction_id=%s WHERE id=%s", (extraction_id,upload_id))
        conn.execute("DELETE FROM upload_parts WHERE upload_id=%s", (upload_id,))
    return extraction_id,upload,content


async def consume_job(extraction_id, upload, content):
    import main
    factories = {'cartao':main.stream_timesheet_extraction,'guia':main.stream_guia_extraction,'contracheque':main.stream_contracheque_extraction,'horas_extras':main.stream_contracheque_extra_hours_extraction,'frequencia':main.stream_frequency_cycle_extraction}
    completed = False
    try:
        async for chunk in factories[upload['mode']](content,upload['filename'].rsplit('.',1)[0]):
            for line in chunk.splitlines():
                if not line.startswith('data: '):
                    continue
                event = json.loads(line[6:])
                if event.get('type') == 'progress':
                    progress[str(extraction_id)] = {k:event[k] for k in ('message','chunk','total') if k in event}
                elif event.get('type') == 'done':
                    await asyncio.to_thread(finish_extraction,extraction_id,event)
                    completed = True
                elif event.get('type') == 'error':
                    await asyncio.to_thread(fail_extraction,extraction_id)
                    completed = True
    except Exception:
        logger.exception('Background extraction failed: %s',extraction_id)
    finally:
        if not completed:
            await asyncio.shield(asyncio.to_thread(fail_extraction,extraction_id))
        progress.pop(str(extraction_id),None)


async def run_job(extraction_id, upload, content):
    async with processing_lock:
        await asyncio.to_thread(lambda: asyncio.run(consume_job(extraction_id,upload,content)))


@router.post('/uploads/{upload_id}/process')
async def process_upload(upload_id: UUID, request: Request):
    async with job_start_lock:
        if len(jobs)>=2:
            raise HTTPException(429,'O servidor está processando outros documentos. Tente novamente em instantes.')
        extraction_id,upload,content = await asyncio.to_thread(prepare_job,upload_id,request)
        if upload:
            task = asyncio.create_task(run_job(extraction_id,upload,content))
            jobs.add(task)
            task.add_done_callback(jobs.discard)
        return {'id':extraction_id}


@router.get('/documents/{extraction_id}')
def job_status(extraction_id: UUID, request: Request):
    selected = team(request)
    with pool.connection() as conn:
        row = conn.execute("SELECT id,status,error FROM extractions WHERE id=%s AND team_id=%s", (extraction_id,selected['team_id'])).fetchone()
        if not row:
            raise HTTPException(404,'Documento não encontrado.')
        row['artifacts'] = conn.execute("SELECT id,kind,filename FROM artifacts WHERE extraction_id=%s AND kind!='original'", (extraction_id,)).fetchall()
    row['progress'] = progress.get(str(extraction_id),{'message':'Aguardando processamento…','chunk':0,'total':1})
    return row


async def drain_jobs():
    if jobs:
        await asyncio.gather(*list(jobs),return_exceptions=True)

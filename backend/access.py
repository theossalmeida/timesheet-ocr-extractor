import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from config import settings
from security import COOKIE_NAME, authenticate


class AccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        public = request.url.path in {'/health', '/auth/login', '/auth/register'}
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if request.headers.get('x-autus-request') != '1' or (origin and origin != settings.APP_ORIGIN):
                return await JSONResponse({'error':'Origem da solicitação inválida.'}, 403)(scope, receive, send)
        if request.method != 'OPTIONS' and not public:
            current = await asyncio.to_thread(authenticate, request.cookies.get(COOKIE_NAME, ''))
            if not current:
                return await JSONResponse({'error':'Entre na sua conta para continuar.'}, 401)(scope, receive, send)
            scope.setdefault('state', {})['user'] = current
        size = 0
        limit = 201 * 1024 * 1024 if request.url.path.startswith(('/extract', '/contracheque', '/preview')) else 16384
        started = False

        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > limit:
                from fastapi import HTTPException
                raise HTTPException(413, 'Arquivo ou solicitação muito grande.')
            return message

        async def secured_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                message['headers'] += [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer')]
            await send(message)

        await self.app(scope, bounded_receive, secured_send)

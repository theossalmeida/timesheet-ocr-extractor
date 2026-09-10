import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from main import app
from tests.make_fixtures import make_native_pdf

pytestmark = pytest.mark.skipif(os.environ.get('AUTUS_BROWSER_TEST')!='1',reason='Set AUTUS_BROWSER_TEST=1 after building the frontend and installing Playwright Chromium')


def test_autus_browser_flow(owner):
    from playwright.sync_api import sync_playwright, expect
    frontend = Path(__file__).resolve().parents[2]/'frontend'
    server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8000,lifespan='off',log_level='warning'))
    thread = threading.Thread(target=server.run,daemon=True)
    thread.start()
    log = open('/tmp/autus-browser-server.log','w')
    node = subprocess.Popen(['node','node_modules/next/dist/bin/next','start','--hostname','127.0.0.1','--port','3000'],cwd=frontend,stdout=log,stderr=log)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1',3000),timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.goto('http://localhost:3000')
            page.get_by_label('E-mail',exact=True).fill('owner@example.com')
            page.get_by_label('Senha',exact=True).fill('a long secure password')
            page.get_by_role('button',name='Entrar',exact=True).click()
            expect(page.get_by_role('heading',name='1 · Tipo de documento')).to_be_visible()
            page.locator('input[type=file]').set_input_files({'name':'browser-ponto.pdf','mimeType':'application/pdf','buffer':make_native_pdf()})
            expect(page.get_by_role('heading',name='Planilha gerada')).to_be_visible(timeout=45000)
            with page.expect_download() as download:
                page.get_by_role('button',name='Baixar Excel',exact=True).click()
            assert download.value.suggested_filename.endswith('.xlsx')
            page.reload()
            expect(page.get_by_text('browser-ponto.pdf',exact=True)).to_be_visible()
            page.get_by_role('button',name='Equipe',exact=True).click()
            expect(page.get_by_role('heading',name='Membros',exact=True)).to_be_visible()
            page.get_by_label('E-mail',exact=True).fill('browser-member@example.com')
            page.get_by_role('button',name='Criar convite',exact=True).click()
            expect(page.get_by_label('Link do convite')).to_be_visible()
            link = page.get_by_label('Link do convite').input_value()
            page.get_by_role('button',name='Sair',exact=True).click()
            page.goto(link)
            page.get_by_label('Nome',exact=True).fill('Browser Member')
            page.get_by_label('E-mail',exact=True).fill('browser-member@example.com')
            page.get_by_label('Senha · pelo menos 8 caracteres',exact=True).fill('browser member password')
            page.get_by_role('button',name='Criar conta',exact=True).click()
            expect(page.get_by_role('heading',name='1 · Tipo de documento')).to_be_visible()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.get_by_role('button',name='Equipe',exact=True).click()
            expect(page.get_by_role('button',name='Criar convite',exact=True)).to_have_count(0)
            assert not errors,errors
            browser.close()
    finally:
        node.terminate()
        node.wait(timeout=15)
        server.should_exit=True
        thread.join(timeout=15)
        log.close()

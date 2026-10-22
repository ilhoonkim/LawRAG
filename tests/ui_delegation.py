"""Synthetic end-to-end: hierarchy sources, numeric citations and HTML navigation."""
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
import uvicorn
from playwright.sync_api import sync_playwright, expect

from app.config import Settings
from app.main import create_app
from test_delegation import DelegationAPI, collect
from test_enhanced_retrieval import source
from test_workflow import FakeProvider


class CaptureProvider(FakeProvider):
    def answer(self, question, evidence, history, **kwargs):
        visible = [e for e in evidence if not e.get('relation') or e.get('include_in_sources')]
        assert len(visible) == 3
        return '**서류 근거**\n\n' + '\n\n'.join(f'{r["law_name"]} {r["number"]} [{i}]' for i,r in enumerate(visible,1))


def main():
    root = Path(tempfile.mkdtemp(prefix='lawrag-delegation-ui-'))
    app = create_app(Settings(data_dir=root), CaptureProvider(), DelegationAPI())
    versions = collect(app.state.management)
    app.state.store.search_enhanced = lambda *a,**kw:[source(app.state.store,versions['300'],'제7조')]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_EXECUTABLE') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1480,'height':1000})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}/')
            page.get_by_label('질문',exact=True).fill('검사 서류는?')
            page.get_by_role('button',name='질문 전송',exact=True).click()
            expect(page.locator('.evidence-chips button')).to_have_count(3)
            expect(page.locator('.citation-link')).to_have_count(3)
            expect(page.get_by_role('complementary',name='지식 출처')).to_have_count(0)
            page.get_by_role('button',name='합성안전법 근거 1개 보기',exact=True).click()
            sources=page.get_by_role('complementary',name='지식 출처')
            expect(sources).to_contain_text('상하위 연결 근거')
            expect(sources).not_to_contain_text('관련도 점수:')
            page.screenshot(path=str(root/'linked-sources.png'),full_page=True)
            sources.locator('.source-card').click()
            frame=page.frame_locator('#source-dialog iframe')
            expect(frame.locator('h1')).to_have_text('합성안전법')
            frame.locator('section:target .delegation-links').get_by_role('link',name='합성안전법 시행규칙 제7조(서류)',exact=True).click()
            expect(frame.locator('h1')).to_have_text('합성안전법 시행규칙')
            expect(frame.locator('section:target')).to_have_attribute('id','합성안전법 시행규칙-제7조')
            frame.locator('section:target pre').get_by_role('link',name='제2조(검사)',exact=True).click()
            expect(frame.locator('h1')).to_have_text('합성안전법 시행령')
            expect(page.locator('#source-dialog .dialog-head strong')).to_have_text('합성안전법 시행령 제2조(검사)')
            page.screenshot(path=str(root/'cross-document.png'),full_page=True)
            assert not errors,errors
            browser.close()
        print('Delegation source/citation/HTML navigation passed:',root)
    finally:
        server.should_exit=True
        thread.join(timeout=10)


if __name__=='__main__':
    main()

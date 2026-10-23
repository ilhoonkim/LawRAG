"""Optional Chromium check: cumulative stream display, model selection, and history.

Run after npm run build: .venv/bin/python tests/ui_nse_stream.py
Uses the existing fixture server and synthetic NDJSON; never contacts company MQ.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
base_url = f'http://127.0.0.1:{port}'
with tempfile.TemporaryDirectory(prefix='lawrag-nse-ui-') as temp:
    with open(Path(temp)/'server.log', 'w') as log:
        process = subprocess.Popen([sys.executable, str(ROOT/'tests/ui_server.py'), str(port)],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(100):
                    try:
                        if client.get(base_url+'/api/status').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('UI fixture server not ready')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=os.getenv('CHROMIUM_EXECUTABLE') or shutil.which('chromium'),
                    headless=True, args=['--no-sandbox','--no-proxy-server'])
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/api/models', lambda route: route.fulfill(json={
                    'models':['gpt-4.1','LOCAL_MODEL'], 'default':'gpt-4.1', 'warning':''}))
                page.add_init_script("""(() => {
                    const original = window.fetch.bind(window);
                    window.fetch = (url, options) => {
                        if (url !== '/api/chat/stream') return original(url, options);
                        window.lastChatRequest = JSON.parse(options.body);
                        return Promise.resolve(new Response(new ReadableStream({ start(controller) {
                            window.pushAnswer = event => controller.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\\n'));
                            window.endAnswer = () => controller.close();
                        }}), {headers: {'Content-Type':'application/x-ndjson'}}));
                    };
                })();""")
                page.goto(base_url+'/#chat')
                selector = page.get_by_label('답변 모델', exact=True)
                expect(selector.locator('option[value="LOCAL_MODEL"]')).to_have_count(1)
                selector.select_option('LOCAL_MODEL')
                page.get_by_label('질문', exact=True).fill('선박 검사 신청 방법')
                page.get_by_role('button', name='질문 전송', exact=True).click()
                page.wait_for_function('window.pushAnswer !== undefined')
                assert page.evaluate('window.lastChatRequest.model') == 'LOCAL_MODEL'
                page.evaluate("window.pushAnswer({type:'stage', stage:'answer'})")
                page.evaluate("window.pushAnswer({type:'answer_text', text:'검사를'})")
                expect(page.get_by_label('생성 중인 답변')).to_have_text('검사를')
                page.evaluate("window.pushAnswer({type:'answer_text', text:'검사를 신청하세요. [1]'})")
                expect(page.get_by_label('생성 중인 답변')).to_have_text('검사를 신청하세요. [1]')
                history = page.evaluate("JSON.parse(localStorage.getItem('lawrag.chats'))")
                assert all(m['role'] != 'assistant' for m in history[0]['messages'])
                page.evaluate("""() => {
                    window.pushAnswer({type:'result', result:{answer:'검사를 신청하세요. [1] 참조 [C5]. 미확보 [C99]. 코드 `[C5]`.', as_of:'2026-10-23',
                      review_trace:{policy:'rejection_recheck_v1',question:'선박 검사 신청 방법',
                        decisions:[{number:'제1조',phase:'recheck',relevant:true,reason:'신청 방법을 규정함'}]},
                      linked_sources:{C5:{law_name:'시험법',number:'별표 1',title:'검사 기준',
                        html_url:'/documents/linked-test.html#annex-1',relation:'references_annex'}},
                      evidence:[{law_name:'시험법',number:'제1조',title:'검사',score:.8,body:'검사를 신청한다.'}]}});
                    window.endAnswer();
                }""")
                expect(page.get_by_label('생성 중인 답변')).to_have_count(0)
                expect(page.get_by_role('button', name='시험법 근거 1개 보기')).to_be_visible()
                history = page.evaluate("JSON.parse(localStorage.getItem('lawrag.chats'))")
                assert history[0]['messages'][-1]['content'].startswith('검사를 신청하세요. [1]')
                assert history[0]['messages'][-1]['model'] == 'LOCAL_MODEL'
                assert history[0]['messages'][-1]['linked_sources']['C5']['number'] == '별표 1'
                page.get_by_role('button',name='근거 1: 시험법 제1조(검사) 보기').click()
                expect(page.get_by_role('complementary',name='지식 출처')).to_contain_text('[1] 제1조')
                page.get_by_role('button',name='지식 출처 닫기').click()
                page.route('**/documents/linked-test.html', lambda route: route.fulfill(
                    content_type='text/html',body='<h1 id="annex-1">Annex fixture</h1>'))
                annex = page.get_by_role('button',name='참조 자료: 시험법 별표 1(검사 기준) 보기')
                expect(annex).to_have_text('별표 1')
                expect(page.locator('.message.assistant .citation-link')).to_have_count(2)
                expect(page.locator('.message.assistant code')).to_have_text('[C5]')
                expect(page.locator('.message.assistant')).to_contain_text('[C99]')
                annex.click()
                expect(page.locator('#source-dialog iframe')).to_have_attribute('src','/documents/linked-test.html#annex-1')
                page.keyboard.press('Escape')
                page.reload()
                page.locator('.history-title').first.click()
                expect(page.get_by_role('button',name='참조 자료: 시험법 별표 1(검사 기준) 보기')).to_be_visible()
                with page.expect_download() as download_info:
                    page.get_by_role('button',name='근거 검토 기록 다운로드').click()
                download = download_info.value
                assert download.suggested_filename == 'lawrag_review.json'
                saved = json.loads(Path(download.path()).read_text())
                assert saved['question'] == '선박 검사 신청 방법'
                assert saved['decisions'][0]['reason'] == '신청 방법을 규정함'

                # A failed stream must never persist its partial answer as an assistant message.
                page.get_by_role('button', name='＋ 새 대화', exact=True).click()
                page.evaluate('window.pushAnswer = undefined')
                page.get_by_label('질문', exact=True).fill('스트리밍 오류 시험')
                page.get_by_role('button', name='질문 전송', exact=True).click()
                page.wait_for_function('window.pushAnswer !== undefined')
                page.evaluate("window.pushAnswer({type:'answer_text',text:'미완성 답변'})")
                expect(page.get_by_label('생성 중인 답변')).to_have_text('미완성 답변')
                page.evaluate("window.pushAnswer({type:'error',detail:'합성 MQ 연결 오류'}); window.endAnswer()")
                expect(page.get_by_text('합성 MQ 연결 오류', exact=True)).to_be_visible()
                expect(page.get_by_label('생성 중인 답변')).to_have_count(0)
                history = page.evaluate("JSON.parse(localStorage.getItem('lawrag.chats'))")
                assert all(m['role'] != 'assistant' for m in history[0]['messages'])

                selector.select_option('gpt-4.1')
                page.evaluate('window.pushAnswer = undefined')
                page.get_by_label('질문', exact=True).fill('GPT 연결 시험')
                page.get_by_role('button', name='질문 전송', exact=True).click()
                page.wait_for_function('window.pushAnswer !== undefined')
                assert page.evaluate('window.lastChatRequest.model') == 'gpt-4.1'
                page.evaluate("window.pushAnswer({type:'result',result:{answer:'GPT 완료 응답',evidence:[]}}); window.endAnswer()")
                expect(page.get_by_text('GPT 완료 응답', exact=True)).to_be_visible()
                assert not errors, errors
                browser.close()
                print('NSE/GPT model selection, cumulative display, final sources, and failure history: PASS')
        finally:
            process.terminate()
            process.wait(timeout=10)

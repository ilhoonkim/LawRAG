"""Optional React browser regression check. Requires playwright and Chromium."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import httpx

ROOT = Path(__file__).resolve().parent.parent
artifacts = Path(tempfile.mkdtemp(prefix='lawrag-ui-artifacts-'))
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
base_url = f'http://127.0.0.1:{port}'
log = (artifacts / 'server.log').open('w', encoding='utf-8')
process = subprocess.Popen([sys.executable, str(ROOT / 'tests' / 'ui_server.py'), str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
try:
    ready = False
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('UI fixture server exited; see ' + str(artifacts))
            try:
                ready = client.get(base_url + '/api/status').status_code == 200
                if ready:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
    if not ready:
        raise RuntimeError('UI fixture server did not become ready')
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as p:
     browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_EXECUTABLE') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
     page=browser.new_page(viewport={'width':1480,'height':1000})
     errors=[];posts=[]
     page.on('pageerror',lambda e:errors.append(str(e)))
     page.on('request',lambda r:posts.append(r.url) if r.method=='POST' else None)
     page.goto(base_url + '/#laws')
     expect(page.get_by_role('heading',name='법령 개정 연혁')).to_be_visible()
     page.evaluate('document.fonts.ready')
     assert page.evaluate('(font) => document.fonts.check(font)', '12px "Noto Sans KR Variable"')
     expect(page.get_by_role('link',name='채팅',exact=True).locator('svg')).to_have_count(1)
     expect(page.get_by_role('navigation',name='법령 카테고리').get_by_role('button')).to_have_count(25)
     expect(page.get_by_role('navigation',name='행정규칙 카테고리').get_by_role('button')).to_have_count(2)
     expect(page.locator('.category-panel summary').first).to_have_text('법령')
     expect(page.get_by_role('button',name='부동산 지정 목록 · 26',exact=True)).to_have_count(0)
     expect(page.get_by_role('button',name='카테고리 등록',exact=True)).to_have_count(0)
     page.get_by_role('button',name='수집하기',exact=True).click()
     dialog=page.get_by_role('dialog',name='법령 수집',exact=True)
     expect(dialog.get_by_text('임베딩 없이 수집',exact=True)).to_have_count(0)
     dialog.get_by_label('수집할 법령명',exact=True).fill('선박안전법')
     dialog.get_by_role('button',name='검색',exact=True).click()
     dialog.get_by_role('button',name='선박안전법 · 버전 조회',exact=True).click()
     expect(dialog.get_by_text('9건 선택',exact=True)).to_be_visible()
     expect(dialog.get_by_label('선박안전법 2026-01-01 수집',exact=True)).to_be_disabled()
     assert not any('/collections' in u for u in posts)
     dialog.get_by_label('조회된 미수집 버전 모두 선택').uncheck()
     dialog.get_by_label('선박안전법 2020-01-01 수집',exact=True).check()
     dialog.get_by_role('button',name='수집 시작',exact=True).click()
     expect(dialog.get_by_label('선박안전법 2020-01-01 수집',exact=True)).to_be_disabled(timeout=10000)
     dialog.get_by_role('navigation',name='수집할 버전 페이지').get_by_role('button',name='2',exact=True).click()
     expect(dialog.get_by_text('3건 선택',exact=True)).to_be_visible()
     dialog.get_by_label('조회된 미수집 버전 모두 선택').uncheck()
     dialog.get_by_label('선박안전법 2000-01-01 수집',exact=True).check()
     dialog.get_by_role('button',name='수집 시작',exact=True).click()
     expect(dialog.get_by_text('수집 실패',exact=True)).to_be_visible(timeout=12000)
     dialog.get_by_role('button',name='법령 수집 닫기').click()
     expect(page.locator('.revisions-table tbody tr')).to_have_count(2,timeout=12000)
     expect(page.get_by_role('navigation',name='법령 카테고리').get_by_role('button',name='선박안전법',exact=True)).to_be_visible()
     page.locator('.revisions-table .row-name').nth(1).click()
     expect(page.locator('.articles-table tbody tr')).to_have_count(3)
     page.get_by_role('button',name='제3조',exact=True).click()
     frame=page.frame_locator('#source-dialog iframe')
     expect(frame.locator('section:target')).to_have_attribute('id','선박안전법-제3조')
     expect(frame.locator('.document-meta')).to_contain_text('2020.01.01')
     expect(frame.locator('section:target')).to_have_css('background-color','rgb(255, 246, 206)')
     expect(frame.locator('.toc a[aria-current="location"]')).to_have_text('제3조(적용범위)')
     page.screenshot(path=str(artifacts / 'document-viewer.png'),full_page=True)
     frame.get_by_role('link',name='제30조의2(검사)',exact=True).click()
     expect(frame.locator('section:target')).to_have_attribute('id','선박안전법-제30조의2')
     expect(frame.locator('.toc a[aria-current="location"]')).to_have_text('제30조의2(검사)')
     page.locator('#source-dialog .dialog-head button').click()
     page.screenshot(path=str(artifacts / 'management.png'),full_page=True)
     page.get_by_role('button',name='수집 이력',exact=True).click()
     history=page.get_by_role('dialog',name='수집 이력',exact=True)
     expect(history.get_by_text('수집 실패',exact=True)).to_be_visible()
     expect(history.get_by_text('수집 완료',exact=True)).to_be_visible()
     expect(history.get_by_text('법령 API 요청 실패',exact=True)).to_be_visible()
     page.screenshot(path=str(artifacts / 'history.png'),full_page=True)
     history.get_by_role('button',name='확인',exact=True).click()
     page.get_by_role('link',name='채팅',exact=True).click()
     page.get_by_label('질문',exact=True).fill('2020년 6월 1일 기준 선박안전법에 적용되지 않는 것은?')
     page.get_by_role('button',name='질문 전송',exact=True).click()
     expect(page.locator('.evidence-chips button').first).to_contain_text('제3조')
     expect(page.get_by_text('기준일 2020-06-01 · 수집된 개정 버전 기준')).to_be_visible()
     page.get_by_role('link',name='법령관리',exact=True).click()
     page.get_by_role('link',name='채팅',exact=True).click()
     expect(page.locator('.evidence-chips button').first).to_be_visible()
     page.reload()
     page.locator('#history button').first.click()
     expect(page.locator('.evidence-chips button').first).to_be_visible()
     page.set_viewport_size({'width':390,'height':844})
     page.get_by_role('link',name='법령관리',exact=True).click()
     expect(page.get_by_role('button',name='수집하기',exact=True)).to_be_visible()
     assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
     page.screenshot(path=str(artifacts / 'mobile.png'),full_page=True)
     assert not errors,errors
     print('PASS React: categories, explicit search/version/collect steps, missing-only selection, paging, success/failure history, article anchor popup, historical chat, page switching/history persistence, mobile width, zero JS errors')
     browser.close()

    print('Screenshots:', artifacts)
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    log.close()

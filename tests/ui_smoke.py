"""Optional React browser regression check. Requires playwright and Chromium."""
import os
import json
import re
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
     expect(dialog.get_by_text('2건 선택',exact=True)).to_be_visible()
     dialog.get_by_label('조회된 미수집 버전 모두 선택').uncheck()
     dialog.get_by_label('선박안전법 2000-01-01 수집',exact=True).check()
     dialog.get_by_role('button',name='수집 시작',exact=True).click()
     expect(dialog.get_by_text('수집 실패',exact=True)).to_be_visible(timeout=12000)
     expect(dialog.locator('.collection-failure')).to_have_text('법령 API 요청 실패')
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
     expect(page.get_by_role('complementary',name='지식 출처')).to_have_count(0)
     page.get_by_label('답변 모델',exact=True).select_option('gpt-5.4')
     page.get_by_label('질문',exact=True).fill('2020년 6월 1일 기준 선박안전법에 적용되지 않는 것은?')
     page.get_by_role('button',name='질문 전송',exact=True).click()
     expect(page.locator('.pipeline-progress .current')).to_contain_text('질문 재조정 중')
     expect(page.locator('.pipeline-progress li')).to_have_count(5)
     expect(page.locator('.pipeline-progress .current')).to_contain_text('법조항 후보군 검토 중')
     expect(page.locator('.pipeline-progress .current')).to_contain_text('근거 법조항 기반 답변 생성 중')
     expect(page.locator('.evidence-chips button')).to_have_count(1)
     expect(page.locator('.evidence-chips button').first).to_contain_text('선박안전법')
     expect(page.get_by_role('complementary',name='지식 출처')).to_have_count(0)
     expect(page.locator('.answer-model')).to_contain_text('gpt-5.4')
     bubble = page.locator('.message.user').bounding_box()
     answer = page.locator('.message.assistant').bounding_box()
     assert abs(bubble['x'] + bubble['width'] - answer['x'] - answer['width']) < 2
     page.screenshot(path=str(artifacts / 'chat-grouped.png'),full_page=True)
     # Citations open the exact source, retaining the answer's original numbering.
     citation=page.locator('.message.assistant .citation-link').first
     expect(citation).to_have_attribute('title','선박안전법 제3조(적용범위)')
     citation.focus()
     page.keyboard.press('Enter')
     expect(page.locator('.source-card[aria-current="true"]')).to_contain_text('[1] 제3조')
     expect(page.locator('.source-card[aria-current="true"]')).to_be_focused()
     page.get_by_label('지식 출처 닫기',exact=True).click()
     expect(citation).to_be_focused()
     page.locator('.evidence-chips button').first.click()
     expect(page.get_by_role('complementary',name='지식 출처')).to_be_visible()
     expect(page.locator('.source-card').first).to_contain_text('제3조')
     page.screenshot(path=str(artifacts / 'chat-sources.png'),full_page=True)
     page.locator('.source-card').first.click()
     expect(page.frame_locator('#source-dialog iframe').locator('section:target')).to_have_attribute('id','선박안전법-제3조')
     page.locator('#source-dialog .dialog-head button').click()
     page.get_by_label('지식 출처 닫기',exact=True).click()
     expect(page.get_by_role('complementary',name='지식 출처')).to_have_count(0)
     expect(page.get_by_text('기준일 2020-06-01 · 수집된 개정 버전 기준')).to_be_visible()
     page.get_by_role('link',name='법령관리',exact=True).click()
     page.get_by_role('link',name='채팅',exact=True).click()
     expect(page.locator('.evidence-chips button').first).to_be_visible()
     page.reload()
     page.locator('#history button').first.click()
     expect(page.locator('.evidence-chips button').first).to_be_visible()
     expect(page.get_by_label('검색 결합 방식',exact=True)).to_have_count(0)
     page.get_by_role('button',name='검색 설정',exact=True).click()
     options=page.get_by_role('dialog',name='검색 설정',exact=True)
     options.get_by_label('검색 결합 방식',exact=True).select_option('rrf')
     options.get_by_label('근거 임계치',exact=True).fill('0.48')
     page.screenshot(path=str(artifacts / 'chat-settings.png'),full_page=True)
     options.get_by_role('button',name='적용',exact=True).click()
     expect(options).to_have_count(0)
     page.get_by_label('질문',exact=True).fill('2020년 6월 1일 기준 선박안전법 적용범위 추가 검색 테스트')
     page.get_by_role('button',name='질문 전송',exact=True).click()
     expect(page.locator('.pipeline-progress .current')).to_contain_text('근거 충분성 확인 중')
     expect(page.locator('.pipeline-progress')).to_contain_text('부족한 근거 추가 검색 중')
     expect(page.locator('.message.assistant').last).to_contain_text('합성 테스트 추가 검색 답변')
     # Group sorting must preserve original answer citation numbers.
     sample = [
       {'law_name':'합성 B법','number':'제1조','score':.95,'title':'최상위','effective':'20200101','version':'b'},
       {'law_name':'합성 A법','number':'제2조','score':.9,'title':'상위','effective':'20200101','version':'a'},
       {'law_name':'연결 전용 C법','number':'제5조','score':.95,'title':'연결 자료','effective':'20200101','version':'c','relation':'references_article','referenced_by':'제1조'},
       {'law_name':'합성 A법','number':'제3조','score':.7,'title':'하위','effective':'20200101','version':'a'}
     ]
     markdown_answer = '''## 거래 유형별 준비 서류

**"명시된 서류"**를 기준으로 정리합니다. [1][2][4]

- **계약서 사본** [2]
  - 조건에 해당할 때 제출합니다.
- 단독신고사유서 [4]

| 서류 | 적용 조건 |
| --- | --- |
| 계약서 | 단독신고 [2] |
| 지급 증빙 | 해당하는 경우 [4] |

과거 답변의 연결 전용 인용: [3]

> 제출 조건을 함께 확인하세요.

코드 속 번호는 그대로 유지: `[1]`

```text
**코드 예시** [2]
```

미확인 번호 [99], [공식 원문](https://www.law.go.kr/), [위험 링크](javascript:alert(1)).

[[1]](https://www.law.go.kr/)

<script>window.markdownExecuted = true</script>

<img src="invalid-image" onerror="window.markdownExecuted = true">

![이미지](https://invalid.example/remote.png)
'''
     page.route('**/api/chat/stream', lambda route: route.fulfill(status=200, content_type='application/x-ndjson',
         body=json.dumps({'type':'result','result':{'answer':markdown_answer,'evidence':sample,'as_of':'2026-10-21'}},ensure_ascii=False)+'\n'))
     page.get_by_label('질문',exact=True).fill('합성 여러 법령 출처 **테스트**')
     page.get_by_role('button',name='질문 전송',exact=True).click()
     chips=page.locator('.message.assistant').last.locator('.evidence-chips button')
     expect(chips).to_have_count(2)
     expect(chips.first).to_contain_text('합성 B법')
     rendered=page.locator('.message.assistant').last.locator('.markdown-body')
     expect(rendered.get_by_role('heading',name='거래 유형별 준비 서류',level=2)).to_be_visible()
     expect(rendered.locator('strong').first).to_have_text('"명시된 서류"')
     expect(rendered.locator('strong').last).to_have_text('계약서 사본')
     expect(rendered.locator('ul ul li')).to_have_text('조건에 해당할 때 제출합니다.')
     expect(rendered.locator('table tbody tr')).to_have_count(2)
     expect(rendered.locator('blockquote')).to_contain_text('제출 조건')
     expect(rendered.locator('code').first).to_have_text('[1]')
     expect(rendered.locator('pre code')).to_contain_text('**코드 예시** [2]')
     expect(rendered.locator('code .citation-link, a .citation-link')).to_have_count(0)
     expect(rendered.locator('.citation-link')).to_have_count(7)
     expect(rendered).not_to_contain_text('[3]')
     expect(chips.filter(has_text='연결 전용 C법')).to_have_count(0)
     expect(rendered).to_contain_text('[99]')
     expect(rendered.locator('a').filter(has_text='위험 링크')).not_to_have_attribute('href', re.compile('javascript:', re.I))
     expect(rendered.locator('img, script')).to_have_count(0)
     assert page.evaluate('window.markdownExecuted === undefined')
     expect(page.locator('.message.user').last).to_contain_text('**테스트**')
     expect(page.locator('.message.user').last.locator('strong')).to_have_count(0)
     # A legacy linked-only [3] is hidden; [4] must retain its original mapping.
     rendered.get_by_role('button',name='근거 4: 합성 A법 제3조(하위) 보기',exact=True).first.click()
     expect(page.locator('.source-card[aria-current="true"]')).to_contain_text('[4] 제3조')
     expect(page.locator('.source-law-name')).to_have_text('합성 A법')
     page.get_by_label('지식 출처 닫기',exact=True).click()
     rendered.scroll_into_view_if_needed()
     page.screenshot(path=str(artifacts / 'chat-markdown.png'),full_page=True)
     chips.nth(1).click()
     expect(page.locator('.source-card')).to_have_count(2)
     expect(page.locator('.source-card').first).to_contain_text('[2] 제2조')
     expect(page.locator('.source-card').last).to_contain_text('[4] 제3조')
     expect(page.locator('.source-card').first).to_contain_text('90.0%')
     expect(page.locator('.source-card').last).to_contain_text('70.0%')
     expect(page.locator('.source-card').first.locator('.source-relevance')).to_have_text('관련도 점수: 90.0%')
     expect(page.locator('.source-card').first.locator('.source-relevance b')).to_have_css('color','rgb(36, 107, 255)')
     expect(page.locator('.source-card').first.locator('.source-effective')).to_have_text('시행일: 2020.01.01')
     expect(page.locator('.sources')).not_to_contain_text('내용 적합도')
     expect(page.locator('.sources')).not_to_contain_text('연결 조문 점수')
     page.screenshot(path=str(artifacts / 'chat-relevance.png'),full_page=True)
     page.get_by_label('지식 출처 닫기',exact=True).click()
     page.set_viewport_size({'width':390,'height':844})
     page.get_by_role('button',name='검색 설정',exact=True).click()
     expect(page.get_by_role('dialog',name='검색 설정',exact=True)).to_be_visible()
     assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
     page.keyboard.press('Escape')
     expect(page.get_by_role('dialog',name='검색 설정',exact=True)).to_have_count(0)
     expect(rendered.locator('table')).to_be_visible()
     assert rendered.locator('.markdown-table-wrap').evaluate('(el) => el.scrollWidth >= el.clientWidth')
     page.locator('.evidence-chips button').first.click()
     expect(page.get_by_role('complementary',name='지식 출처')).to_be_visible()
     assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
     page.screenshot(path=str(artifacts / 'chat-mobile.png'),full_page=True)
     page.get_by_label('지식 출처 닫기',exact=True).click()
     page.get_by_role('link',name='법령관리',exact=True).click()
     expect(page.get_by_role('button',name='수집하기',exact=True)).to_be_visible()
     assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
     page.screenshot(path=str(artifacts / 'mobile.png'),full_page=True)
     assert not errors,errors
     print('PASS React: categories, collection, history, article popup, chat stages, model/settings, grouped sources, Markdown/CJK emphasis/tables, safe content rendering, clickable citations and focus, mobile layout, zero JS errors')
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

"""별표 링크 및 보관 HTML 재생성 회귀 검사. 실제 API 호출은 하지 않습니다."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote

import pytest

from app.laws import parse_law, render_html
from app.rebuild_html import rebuild_all
from app.store import Store
import law_article_links as standalone
from test_admrul_parser import prose
from test_workflow import RAW, FakeProvider


def annex_xml():
    # 모든 번호에 같은 처리가 적용되고, 별표 1과 별표 1의2가 혼동되지 않아야 합니다.
    nodes = ''.join(
        f'<별표단위><별표구분>별표</별표구분><별표번호>{num}</별표번호>'
        f'<별표가지번호>{branch}</별표가지번호><별표제목>기준 {num}-{branch}</별표제목>'
        '<별표내용>테스트 별표 본문. 제3조에 따른다.</별표내용></별표단위>'
        for num, branch in [(1, 0), (2, 0), (10, 0), (123, 0), (1, 2)]
    )
    nodes += ('<별표단위><별표번호>7</별표번호><별표제목>파일 전용</별표제목>'
              '<별표PDF파일링크>/sample.pdf</별표PDF파일링크></별표단위>')
    body = ('별표1, 별표 2, 별표10, 별표 123 및 별표 1의2를 따른다. '
            '별표 1의9와 별표 99는 없다. 별표 7을 확인한다. '
            '「다른법」 별표 2는 별도다. 같은 법 별표 10은 확인이 필요하다.')
    return RAW.replace('테스트용 선박에는 적용하지 아니한다.'.encode(), body.encode()).replace(
        '</법령>'.encode(), ('<별표>' + nodes + '</별표></법령>').encode())


def unnumbered_annex_xml(number='000000'):
    # 사용자 화면의 '선박법 시행령 제13조 ... 별표와 같다' 형태를 합성 재현합니다.
    return f'''<법령><기본정보><법령ID>TEST-UNNUMBERED</법령ID>
      <법령명_한글>선박법 시행령</법령명_한글><시행일자>20191008</시행일자></기본정보>
      <조문><조문단위><조문번호>13</조문번호><조문제목>과태료의 부과기준</조문제목>
      <조문내용>제13조(과태료의 부과기준) 법 제35조제1항 및 제2항에 따른 과태료의 부과기준은 별표와 같다.</조문내용>
      </조문단위></조문><별표><별표단위><별표구분>별표</별표구분>
      <별표번호>{number}</별표번호><별표가지번호>00</별표가지번호>
      <별표제목>과태료의 부과기준(제13조 관련)</별표제목>
      <별표내용>■ 선박법 시행령 [별표] &lt;개정 2019. 10. 8.&gt;</별표내용>
      <별표PDF파일링크>/example.pdf</별표PDF파일링크>
      </별표단위></별표></법령>'''.encode()


class DocumentLinks(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.section = None
        self.ids = set()
        self.links = []
        self.text = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if tag == 'section':
            self.section = attrs['id']
        if tag == 'a':
            self.links.append((self.section, attrs['href']))

    def handle_endtag(self, tag):
        if tag == 'section':
            self.section = None

    def handle_data(self, text):
        self.text.append(text)


@pytest.mark.parametrize('parse,render', [
    (parse_law, render_html), (standalone.parse_law_xml, standalone.render_html)
])
@pytest.mark.parametrize('number', ['0', '000000'])
def test_unnumbered_annex_display_and_link_preserve_old_anchor(parse, render, number):
    page = render(parse(unnumbered_annex_xml(number)))
    parsed = DocumentLinks(page)
    refs = [unquote(url) for section, url in parsed.links if section == '선박법 시행령-제13조']
    assert refs == ['#선박법 시행령-별표0']
    assert '선박법 시행령-별표0' in parsed.ids
    assert '별표 0' not in ''.join(parsed.text)
    assert '별표(과태료의 부과기준(제13조 관련))</a>와 같다.' in page
    assert not any(url.startswith('#') for section, url in parsed.links if section == '선박법 시행령-별표0')
    assert ('선박법 시행령-별표0', 'https://www.law.go.kr/example.pdf') in parsed.links


@pytest.mark.parametrize('parse,render', [
    (parse_law, render_html), (standalone.parse_law_xml, standalone.render_html)
])
def test_bare_annex_boundaries_foreign_references_and_missing_targets(parse, render):
    law = parse(unnumbered_annex_xml())
    law.articles[0].body = ('[별표] 참조. 별표에 따른다. 별표를 적용한다. 별표\n'
                            '「다른법」 별표와 같다. 같은 법 별표를 적용한다. '
                            '별표 1과 별표 1의2는 없다. 특별표 별표준 별표목록')
    parsed = DocumentLinks(render(law))
    refs = [unquote(url) for section, url in parsed.links if section == '선박법 시행령-제13조']
    assert refs == ['#선박법 시행령-별표0'] * 4
    # 번호 있는 별표가 하나뿐이어도 번호 없는 참조의 대상으로 추정하지 않습니다.
    numbered = parse(unnumbered_annex_xml('1'))
    assert not [url for section, url in DocumentLinks(render(numbered)).links
                if section == '선박법 시행령-제13조']
    law.annexes[0].title = '별표 0'  # 제목이 없어서 파서가 번호를 대신 넣은 경우
    law.annexes[0].body = ''
    page = render(law)
    assert '<h2>별표</h2>' in page and '별표 0' not in ''.join(DocumentLinks(page).text)
    assert '별표 본문이 확보되지 않았습니다.' in page


@pytest.mark.parametrize('parse,render', [
    (parse_law, render_html), (standalone.parse_law_xml, standalone.render_html)
])
def test_all_annex_numbers_branches_files_and_external_refs(parse, render):
    law = parse(annex_xml())
    law.annexes[0].title = '<script>제목</script>'
    page = render(law)
    parsed = DocumentLinks(page)
    refs = [unquote(url[1:]) for section, url in parsed.links
            if section == '선박안전법-제3조' and url.startswith('#')]
    assert refs == ['선박안전법-별표' + n for n in ['1', '2', '10', '123', '1의2', '7']]
    assert all(unquote(url[1:]) in parsed.ids for _, url in parsed.links if url.startswith('#'))
    assert ('선박안전법-별표7', 'https://www.law.go.kr/sample.pdf') in parsed.links
    assert '별표 본문이 확보되지 않았습니다.' in page
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert '「다른법」 별표 2는' in page and '같은 법 별표 10은' in page
    assert '별표 1의9와 별표 99는' in page
    # 별표 본문의 조문 참조도 같은 HTML 내 대상에 연결됩니다.
    assert any(section == '선박안전법-별표2' and unquote(url) == '#선박안전법-제3조'
               for section, url in parsed.links)


def test_standalone_runs_outside_repository_without_dependencies(tmp_path):
    source = tmp_path / 'sample.xml'
    source.write_bytes(annex_xml())
    script = Path(standalone.__file__).resolve()
    result = subprocess.run([sys.executable, '-I', str(script), str(source)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '별표 123(기준 123-0)</a>' in source.with_suffix('.html').read_text(encoding='utf-8')


def test_rebuild_every_version_and_rule_without_changing_db_or_raw(tmp_path, monkeypatch):
    store = Store(tmp_path)
    first = store.ingest(annex_xml().replace(b'20260101', b'20200101'), FakeProvider(), archive_only=True)
    second = store.ingest(annex_xml(), FakeProvider(), archive_only=True)
    rule = store.ingest(prose('1. 테스트 지침 본문'), FakeProvider(), kind='admrul', archive_only=True)
    paths = [tmp_path / 'html' / (row['version'] + '.html') for row in [first, second, rule]]
    for path in paths:
        path.write_text('<html data-viewer-version="2">old</html>', encoding='utf-8')
    paths[1].unlink()  # DB에 수집 버전이 있다면 유실된 HTML도 복구합니다.
    before_db = store.db.read_bytes()
    before_raw = {p.name: p.read_bytes() for p in (tmp_path / 'raw').iterdir()}
    def forbidden(*args, **kwargs):
        raise AssertionError('HTML rebuild must not call API, embedding, or Store initialization')
    monkeypatch.setattr('app.laws.LawAPI.request', forbidden)
    monkeypatch.setattr('app.provider.Provider.post', forbidden)
    monkeypatch.setattr(Store, '__init__', forbidden)
    report = rebuild_all(tmp_path, notify=lambda _: None)
    assert (report['total'], report['completed'], report['failed']) == (3, 3, 0)
    assert store.db.read_bytes() == before_db
    assert {p.name: p.read_bytes() for p in (tmp_path / 'raw').iterdir()} == before_raw
    assert '[시행 2020.01.01]' in paths[0].read_text(encoding='utf-8')
    assert '[시행 2026.01.01]' in paths[1].read_text(encoding='utf-8')
    assert all('data-viewer-version="4"' in p.read_text(encoding='utf-8') for p in paths)
    assert not list((tmp_path / 'html').glob('*.tmp'))


def test_rebuild_preserves_failed_files_and_continues(tmp_path):
    store = Store(tmp_path)
    rows = [store.ingest(annex_xml().replace(b'20260101', day), FakeProvider(), archive_only=True)
            for day in [b'20200101', b'20210101', b'20220101']]
    paths = [tmp_path / 'html' / (row['version'] + '.html') for row in rows]
    for path in paths:
        path.write_text('previous HTML', encoding='utf-8')
    (tmp_path / 'raw' / (rows[0]['version'] + '.xml')).unlink()
    (tmp_path / 'raw' / (rows[1]['version'] + '.xml')).write_bytes(annex_xml())
    report = rebuild_all(tmp_path, notify=lambda _: None)
    assert (report['completed'], report['failed']) == (1, 2)
    assert paths[0].read_text() == paths[1].read_text() == 'previous HTML'
    assert 'data-viewer-version="4"' in paths[2].read_text(encoding='utf-8')


def test_rebuild_replace_failure_keeps_previous_html(tmp_path, monkeypatch):
    store = Store(tmp_path)
    row = store.ingest(annex_xml(), FakeProvider(), archive_only=True)
    path = tmp_path / 'html' / (row['version'] + '.html')
    path.write_text('previous HTML', encoding='utf-8')
    def locked(*args, **kwargs):
        raise PermissionError('locked')
    monkeypatch.setattr(Path, 'replace', locked)
    report = rebuild_all(tmp_path, notify=lambda _: None)
    assert report['failed'] == 1 and path.read_text() == 'previous HTML'
    assert not list((tmp_path / 'html').glob('*.tmp'))


def test_rebuild_unnumbered_annex_without_reindexing(tmp_path):
    store = Store(tmp_path)
    raw = unnumbered_annex_xml()
    row = store.ingest(raw, FakeProvider(), archive_only=True)
    path = tmp_path / 'html' / (row['version'] + '.html')
    path.write_text('<html data-viewer-version="3">별표 0</html>', encoding='utf-8')
    before = store.db.read_bytes()
    report = rebuild_all(tmp_path, notify=lambda _: None)
    assert (report['completed'], report['failed']) == (1, 0)
    assert store.db.read_bytes() == before
    assert (tmp_path / 'raw' / (row['version'] + '.xml')).read_bytes() == raw
    parsed = DocumentLinks(path.read_text(encoding='utf-8'))
    assert ('선박법 시행령-제13조', '#선박법 시행령-별표0') in [
        (section, unquote(url)) for section, url in parsed.links]
    assert '별표 0' not in ''.join(parsed.text)

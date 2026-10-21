from datetime import date
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.store import Store
from app.laws import parse_law, render_html
from app.references import article_links, annex_references
from test_workflow import RAW, FakeProvider


def referenced_raw(with_annex=True):
    raw=RAW.replace('테스트용 선박에는 적용하지 아니한다.'.encode(),'별표 2에 따른 선박에는 제11조를 적용하지 아니한다.'.encode())
    extra='<조문단위><조문번호>11</조문번호><조문제목>허가구역 내 토지거래에 대한 허가</조문제목><조문내용>제11조(허가구역 내 토지거래에 대한 허가) 합성 테스트 허가 본문.</조문내용></조문단위>'
    raw=raw.replace('</조문>'.encode(),(extra+'</조문>').encode())
    if with_annex:
        annex='<별표><별표단위><별표구분>별표</별표구분><별표번호>2</별표번호><별표제목>합성 테스트 기준</별표제목><별표내용>별표 2 내용\n테스트 선박 A는 제외한다.</별표내용></별표단위></별표>'
        raw=raw.replace('</법령>'.encode(),(annex+'</법령>').encode())
    return raw


def test_reference_expansion_and_title(tmp_path):
    store=Store(tmp_path);provider=FakeProvider();store.ingest(referenced_raw(),provider)
    evidence=store.search('선박안전법 적용범위',provider,limit=1,as_of=date(2026,10,21))
    expanded,missing=store.expand_references(evidence)
    assert not missing
    assert {e['number'] for e in expanded}=={'제3조','제11조','별표 2'}
    assert any(r['title']=='허가구역 내 토지거래에 대한 허가' for r in expanded[0]['reference_titles'])
    assert len({e['version'] for e in expanded})==1
    html=render_html(parse_law(referenced_raw()))
    assert '제11조(허가구역 내 토지거래에 대한 허가)</a>' in html
    assert 'id="선박안전법-별표2"' in html
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM article_references').fetchone()[0]==2


def test_old_annex_never_mixed_with_new(tmp_path):
    store=Store(tmp_path);provider=FakeProvider()
    store.ingest(referenced_raw().replace(b'20260101',b'20200101').replace('테스트 선박 A'.encode(),'과거 선박 B'.encode()),provider)
    store.ingest(referenced_raw(),provider)
    evidence=store.search('선박안전법 적용범위',provider,limit=1,as_of=date(2020,6,1))
    expanded,missing=store.expand_references(evidence)
    assert '과거 선박 B' in next(e for e in expanded if e['number']=='별표 2')['body']


def test_missing_annex_blocks_generation(tmp_path):
    provider=FakeProvider();app=create_app(Settings(data_dir=tmp_path),provider)
    app.state.store.ingest(referenced_raw(False),provider)
    r=TestClient(app).post('/api/chat',json={'question':'선박안전법 적용범위','limit':1,'as_of':'2026-10-21'}).json()
    assert not r['generated'] and provider.calls==0
    assert r['missing_references'][0]['number']=='별표 2'
    assert '본문이 확보되지 않았습니다' in r['answer']


def test_external_references_are_not_bound_to_own_law():
    assert article_links('「다른법」 제11조를 적용한다.', '현재법')==[('제11조','다른법')]
    assert article_links('같은 법 제11조를 적용한다.', '현재법')==[('제11조','같은 법(대상 확인 필요)')]
    assert annex_references('별표 1부터 별표 3까지', '현재법')==[('별표 1','현재법'),('별표 2','현재법'),('별표 3','현재법')]


def test_branch_reference_and_html_escaping():
    assert article_links('제11조의2를 따른다.', '현재법')==[('제11조의2','현재법')]
    law=parse_law(referenced_raw());law.articles[-1].title='<script>alert(1)</script>'
    html=render_html(law)
    assert '<script>' not in html


def test_self_and_other_law_same_number_are_not_mislinked():
    law=parse_law(referenced_raw())
    law.articles[1].body='이 법 제11조를 적용한다. 「다른법」 제11조는 별도로 확인한다.'
    html=render_html(law)
    assert html.count('제11조(허가구역 내 토지거래에 대한 허가)</a>')==1
    assert '「다른법」 제11조는' in html
    assert article_links('「다른법」과 달리 이 법 제11조를 적용한다.',law.name)==[('제11조',law.name)]
    assert article_links('법 제11조에 따라','선박안전법 시행령')==[('제11조','법(대상 확인 필요)')]

"""사용자가 제시한 선박안전법 시행령 제3·4조의 인용 구조를 재현합니다."""
from urllib.parse import unquote

from app.laws import parse_law, render_html
from app.rebuild_html import rebuild_all
from app.references import article_links, annex_references
from app.store import Store
from test_annex_html import DocumentLinks
from test_enhanced_retrieval import xml
from test_workflow import FakeProvider


def test_range_and_enumeration_keep_law_target_and_stop_at_boundaries():
    own = '선박안전법 시행령'
    refs = article_links('법 제4조부터 제11조까지, 법 제13조부터 제17조까지, 법 제88조 및 제89조에 따른다.', own)
    assert set(refs)=={(f'제{n}조','법(대상 확인 필요)') for n in [*range(4,12),*range(13,18),88,89]}
    refs = article_links('법 제3조제3항 및 제4조제2항을 따른다. 제11조에 따른다.\n제17조를 적용한다.',own)
    assert set(refs)=={('제3조','법(대상 확인 필요)'),('제4조','법(대상 확인 필요)'),('제11조',own),('제17조',own)}
    assert article_links('법 제3조 및 영 제4조 및 제5조에 따른다.',own)==[
        ('제3조','법(대상 확인 필요)'),('제4조','영(대상 확인 필요)'),('제5조','영(대상 확인 필요)')]
    assert set(annex_references('법 별표 1부터 별표 3까지',own))=={(f'별표 {n}','법(대상 확인 필요)') for n in range(1,4)}


def test_repeated_ranges_resolve_at_each_occurrence_and_include_external_same_number():
    refs = article_links('법 제3조부터 제5조까지, 영 제3조부터 제5조까지', '시행규칙', source_number='제3조')
    assert set(refs)=={(f'제{n}조',target+'(대상 확인 필요)') for n in range(3,6) for target in ('법','영')}


def test_screenshot_links_after_family_completion_and_no_wrong_local_range_links(tmp_path):
    store = Store(tmp_path)
    provider = FakeProvider()
    declaration = '① 「선박안전법」 (이하 "법"이라 한다) 제3조제1항제3호에서 정하는 선박이다.'
    range_body = ('제3조(외국선박에 대한 법의 적용범위) 법 제3조제2항에 따라 외국선박에 대하여 '
                  '적용되는 규정은 법 제4조부터 제11조까지, 법 제13조부터 제17조까지, 법 제18조제9항, '
                  '법 제83조부터 제86조까지, 법 제88조 및 법 제89조로 한다.')
    paragraph_body = ('① 법 제3조제3항에 따라 법의 전부 또는 일부를 적용하지 아니한다.\n'
                      '1. 법 제3조제3항제1호의 선박: 협정의 내용에 따른다.\n'
                      '2. 법 제3조제3항제2호의 선박: 법 제17조제2항을 적용하지 아니한다.')
    decree_raw = xml('선박안전법 시행령', [('2','적용제외 선박',declaration),('3','외국선박에 대한 법의 적용범위',range_body),
        ('4','협정체결에 따른 적용배제 등',paragraph_body),('11','대행업무의 취소 등','시행령의 다른 조문'),
        ('17','항만국통제에 따른 조치 등','시행령의 다른 조문')], '20241112')
    decree = store.ingest(decree_raw,provider,archive_only=True)
    path = tmp_path/'html'/(decree['version']+'.html')
    initial = path.read_text(encoding='utf-8')
    assert '상하위 문서 링크 준비 중' in initial
    # 상대 법이 아직 없을 때 자기 시행령 제11·17조로 잘못 붙이면 안 됩니다.
    assert not [url for section,url in DocumentLinks(initial).links if section=='선박안전법 시행령-제3조']
    statute = store.ingest(xml('선박안전법',[(str(n),'법률 조문 '+str(n),'법률 본문')
        for n in [3,4,11,13,17,18,83,86,88,89]],'20230628'),provider,archive_only=True)
    before = store.db.read_bytes()
    assert rebuild_all(tmp_path,notify=lambda _:None)['failed']==0
    assert store.db.read_bytes()==before
    completed = path.read_text(encoding='utf-8')
    assert '상하위 문서 링크 준비 중' not in completed
    assert '참조 연결 기준일:' in completed
    parsed = DocumentLinks(completed)
    for section in ('선박안전법 시행령-제3조','선박안전법 시행령-제4조'):
        urls = [url for src,url in parsed.links if src==section]
        assert urls and all(url.startswith(statute['version']+'.html#') for url in urls)
    assert '제3조(법률 조문 3)</a>제3항제1호' in completed
    assert any(unquote(url).endswith('#선박안전법-제11조') for _,url in parsed.links)

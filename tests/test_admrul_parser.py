"""Synthetic administrative-rule response variants, not authoritative laws."""
from urllib.parse import unquote

import pytest

from app.laws import parse_admrul, render_html
from app.management import CollectionInput, Management
from app.store import Store
from test_workflow import FakeProvider


def document(body):
    return ('<행정규칙><기본정보><행정규칙명>합성 업무처리지침</행정규칙명>'
            '<행정규칙ID>TEST-RULE</행정규칙ID><시행일자>20200101</시행일자>'
            '<발령일자>20191201</발령일자></기본정보>' + body + '</행정규칙>').encode()


def prose(value):
    return document('<조문내용><![CDATA[' + value + ']]></조문내용>')


def test_structured_rule_preserves_paragraphs_branch_and_markup():
    raw = document('''<조문><조문단위><조문번호>11</조문번호><조문가지번호>2</조문가지번호>
        <조문제목>발급 절차</조문제목><조문내용>제11조의2(발급 절차)</조문내용>
        <항><항내용>① 신청서를 제출한다.<br/>다음 사항을 확인한다.</항내용>
        <호><호내용>1. 신청인 확인</호내용></호></항></조문단위></조문>''')
    article = parse_admrul(raw).articles[0]
    assert article.number == '제11조의2' and article.title == '발급 절차'
    assert '① 신청서를 제출한다.\n다음 사항을 확인한다.\n1. 신청인 확인' in article.body
    assert '<br' not in article.body


def test_single_cdata_document_splits_headings_but_not_citations():
    law = parse_admrul(prose('''제1장 총칙
제1조(목적) 이 지침의 목적을 정한다.
제11조를 적용하지 아니한다.
제11조(허가)에 따른 사항도 확인한다.
제11조(허가) 및 제12조에 관한 사항.
제2조의2(신청) 신청 내용을 확인한다.
부칙 <제1호>
제1조(시행일) 이 지침은 공포한 날부터 시행한다.'''))
    numbered = [a for a in law.articles if a.number.startswith('제')]
    assert [a.number for a in numbered] == ['제1조','제2조의2']
    assert '제11조를 적용하지 아니한다.' in numbered[0].body
    assert '제11조(허가) 및 제12조' in numbered[0].body
    assert numbered[0].chapter == '제1장 총칙'
    assert law.articles[-1].title == '부칙 본문'
    assert '공포한 날' in law.articles[-1].body


def test_html_preserves_rows_cells_and_text_without_executing_markup():
    raw = prose('''<div>제1조(처리 절차)</div><p>첫째<br>둘째</p>
<table><tr><th>항목</th><th>값</th></tr><tr><td>발급</td><td>확인</td></tr></table>
<script>alert('secret')</script><style>.hidden{}</style><p>비교값 1 &lt; 3</p>
<p>제2조(종료) 처리 완료</p>''')
    law = parse_admrul(raw)
    assert [a.number for a in law.articles] == ['제1조','제2조']
    assert '첫째\n둘째' in law.articles[0].body
    assert '항목\t값\n발급\t확인' in law.articles[0].body
    assert '1 < 3' in law.articles[0].body
    output = render_html(law)
    assert '<script' not in output and 'secret' not in output
    assert '1 &lt; 3' in output


def test_actual_xml_html_children_and_separate_content_nodes():
    law = parse_admrul(document('''<조문내용><p>제1조(목적) 안내한다.</p><p>본문을 보존한다.</p></조문내용>
<조문내용>제2조(절차) 다음 절차를 따른다.</조문내용>
<부칙내용>제1조(시행일) 공포일 시행.</부칙내용>'''))
    assert [a.number for a in law.articles[:2]] == ['제1조','제2조']
    assert '본문을 보존한다.' in law.articles[0].body
    assert law.articles[-1].title == '부칙 본문'


@pytest.mark.parametrize('tag', ['조문내용','행정규칙내용','본문내용','본문','조문'])
def test_unnumbered_rule_keeps_original_text_and_never_invents_articles(tag):
    original = '1. 목적\n등기사항증명서의 발급 절차를 정한다.\n가. 접수\n나. 확인\n2. 처리\n기록을 확인한다.'
    law = parse_admrul(document(f'<{tag}>{original}</{tag}>'))
    assert law.articles[0].number.startswith('본문 구간 ')
    assert law.articles[0].title == '1. 목적'
    assert ''.join(a.body for a in law.articles) == original
    assert all(not a.number.startswith('제') for a in law.articles)


def test_large_prose_is_bounded_and_lossless():
    original = ('접수 내용을 확인하고 처리한다.\n' * 700).strip()
    law = parse_admrul(prose(original))
    assert len(law.articles) > 1
    assert all(len(a.body) <= 2000 for a in law.articles)
    assert ''.join(a.body for a in law.articles) == original
    assert len({a.section_id for a in law.articles}) == len(law.articles)


def test_hyphen_numbers_and_duplicate_numbers_have_safe_locations():
    law = parse_admrul(prose('제2-1조(신청) 신청한다.\n제2-2조의2(확인) 확인한다.'))
    assert [a.number for a in law.articles] == ['제2-1조','제2-2조의2']
    law = parse_admrul(prose('제1조(원 규칙) 첫 규정.\n제1조(인용 규칙) 둘째 규정.'))
    assert all(a.number.startswith('본문 구간 ') for a in law.articles)
    assert '첫 규정.' in law.articles[0].body and '둘째 규정.' in law.articles[0].body


@pytest.mark.parametrize('body', [
    '<첨부파일링크>https://example.test/a.hwp</첨부파일링크>',
    '<조문내용><![CDATA[<img src="a.png">]]></조문내용>',
    '<조문단위><조문번호>1</조문번호><조문제목>목적</조문제목></조문단위>',
])
def test_missing_text_does_not_become_successful_collection(body):
    with pytest.raises(ValueError,match='추출 가능한 본문 텍스트'):
        parse_admrul(document(body))


def test_collect_prose_creates_embeddings_history_and_html(tmp_path):
    raw = prose('1. 신청\n신청 접수 및 확인 절차를 정한다.')
    class API:
        def fetch(self,mst,kind='law',effective=''):
            assert kind == 'admrul' and effective == '20200101'
            return raw
    manager = Management(Store(tmp_path),FakeProvider(),API())
    job = manager.queue(CollectionInput(items=[{'mst':'123','kind':'admrul','name':'합성 업무처리지침','effective':'20200101'}]))
    manager.run(job['ids'])
    event = manager.history(1)['items'][0]
    assert event['status'] == 'completed'
    listing = manager.articles(event['version'],1)
    assert listing['total'] == 1 and listing['items'][0]['number'] == '본문 구간 1'
    assert '#합성 업무처리지침-본문 구간 1' in unquote(listing['items'][0]['html_url'])
    with manager.store.connect() as db:
        assert db.execute('SELECT count(*) FROM version_articles WHERE vector IS NOT NULL AND title_vector IS NOT NULL').fetchone()[0] == 1
    assert (tmp_path/'raw'/f'{event["version"]}.xml').read_bytes() == raw


def test_failed_parse_retains_raw_xml_for_diagnosis(tmp_path):
    raw = document('<조문내용/>')
    class API:
        def fetch(self,*args,**kwargs):
            return raw
    manager = Management(Store(tmp_path),FakeProvider(),API())
    job = manager.queue(CollectionInput(items=[{'mst':'123','kind':'admrul','name':'합성 업무처리지침'}]))
    manager.run(job['ids'])
    event = manager.history(1)['items'][0]
    assert event['status'] == 'failed' and '원문 XML: raw/' in event['reason']
    assert next((tmp_path/'raw').glob('*.xml')).read_bytes() == raw
    assert manager.revisions('합성 업무처리지침','admrul',1,'collected')['total'] == 0

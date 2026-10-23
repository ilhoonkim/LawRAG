"""Explicit annex dependencies survive budget selection without truncating tables."""
from datetime import date
from xml.sax.saxutils import escape

import pytest

from app.chat_pipeline import budgeted, trim_token_context, run_chat
from app.context_selection import evidence_units
from app.config import Settings
from app.main import ChatRequest
from app.provider import Provider
from app.store import Store
from app.vllm_context import ContextBudgetExceeded
from test_chat_pipeline import row
from test_enhanced_retrieval import source, put
from test_references import referenced_raw
from test_workflow import FakeProvider


def annex(parent, n=14, body='수치\t수량\n10\t2\n주: 단서와 예외'):
    result = {**row(n,parent['score'],body),'number':f'별표 {n}', 'title':'상세 기준',
              'relation':'references_annex','kind':'annex','reference_edges':[],
              'html_url':f'/documents/v1.html#annex-{n}'}
    add_parent(result,parent)
    return result


def add_parent(table,parent):
    table['reference_edges'].append({'relation':'references_annex',
        'from_version':parent['version'],'from_law':parent['law_name'],'from_article':parent['number'],
        'to_version':table['version'],'to_law':table['law_name'],'to_article':table['number']})


def test_char_budget_keeps_top_article_and_table_before_other_articles():
    high,low = row(1,.9,'a'*50),row(2,.8,'b'*80)
    table = annex(high,body='t'*200)
    incidental = {**row(3,.99,'c'*100),'relation':'reverse_reference'}
    selected, missing = budgeted([low,incidental,table,high],280)
    assert [r['number'] for r in selected] == ['제1조','별표 14']
    assert selected[1]['body'] == 't'*200
    assert {r['number'] for r in missing} == {'제2조','제3조'}


def test_token_budget_removes_lowest_bundle_instead_of_highest_table():
    high,low = row(1,.9),row(2,.8)
    high_table,low_table = annex(high,14),annex(low,15)
    evidence,missing,omitted = [low_table,high,low,high_table],[],[]
    trim_token_context(evidence,missing,omitted,ContextBudgetExceeded(40093,32768,2048),'answer')
    assert {r['number'] for r in evidence} == {'제1조','별표 14'}
    assert {r['number'] for r in omitted} == {'제2조','별표 15'}


def test_shared_table_stays_with_retained_parent_without_duplicates():
    high,low = row(1,.9),row(2,.8)
    table = annex(low)
    add_parent(table,high)
    units = evidence_units([low,table,high])
    assert [[r['number'] for r in unit] for unit in units] == [['제1조','별표 14'],['제2조']]
    evidence = [low,table,high]
    trim_token_context(evidence,[],[],ContextBudgetExceeded(40093,32768,2048),'answer')
    assert {r['number'] for r in evidence} == {'제1조','별표 14'}


def test_wrong_version_reference_cannot_bind_annex_to_current_article():
    high,low = row(1,.9),row(2,.8)
    table = annex({**high,'version':'old'})
    units = evidence_units([high,low,table])
    assert [[r['number'] for r in unit] for unit in units] == [['제1조'],['제2조'],['별표 14']]


def test_cross_law_annex_uses_exact_edge_versions():
    high,low = row(1,.9),row(2,.8)
    table = {**annex(high),'version':'external','law_name':'다른기준','reference_edges':[]}
    add_parent(table,high)
    assert evidence_units([low,table,high])[0] == [high,table]


def test_oversized_last_bundle_falls_back_to_complete_article_and_records_table():
    high = row(1,.9,'a'*50)
    table = annex(high,body='t'*1000)
    selected,missing = budgeted([high,table],200)
    assert selected == [high] and missing[0]['number']=='별표 14'
    evidence,omitted = [high,table],[]
    trim_token_context(evidence,[],omitted,ContextBudgetExceeded(40093,32768,2048),'answer')
    assert evidence == [high] and omitted[0]['number']=='별표 14'
    assert table['body'] == 't'*1000


def test_graph_limit_prioritizes_explicit_annex_over_word_overlap(tmp_path):
    store = Store(tmp_path)
    saved = store.ingest(referenced_raw(),FakeProvider())
    root = source(store,saved['version'],'제3조')
    expanded,missing = store.expand_context([root],cutoff=date(2026,10,21),
                                          question='허가구역 내 토지거래에 대한 허가',limit=1)
    assert [r['number'] for r in expanded] == ['제3조','별표 2']
    assert '테스트 선박 A는 제외한다.' in expanded[1]['body']
    assert any(r['number']=='제11조' for r in missing)


@pytest.mark.parametrize('stage',['assess','answer'])
def test_actual_answer_receives_retained_table_and_link_metadata(stage):
    high,low = row(1,.9,'별표 14의 상세 기준을 따른다.'),row(2,.8)
    table = annex(high)
    class SourceStore:
        def temporal_versions(self,cutoff):
            return {'versions':[],'missing':[],'note':''}
        def search_enhanced(self,*args,**kwargs):
            return [high,low]
        def expand_context(self,roots,**kwargs):
            return roots+[table],[]
    class Model(FakeProvider):
        def rewrite_question(self,q):
            return q
        def assess_evidence(self,q,evidence,*args):
            if stage=='assess' and len(evidence)>2:
                raise ContextBudgetExceeded(40093,32768,2048)
            return {'sufficient':True,'missing_issues':[],'missing_facts':[],'search_queries':[]}
        def answer(self,q,evidence,*args,**kwargs):
            if stage=='answer' and len(evidence)>2:
                raise ContextBudgetExceeded(40093,32768,2048)
            assert {r['number'] for r in evidence} == {'제1조','별표 14'}
            assert next(r for r in evidence if r['number']=='별표 14')['body'] == table['body']
            return '상세 기준은 별표와 같다. [1][C1]'
    result = run_chat(SourceStore(),Model(),Settings(),ChatRequest(question='대상과 상세 기준은?'))
    assert [r['number'] for r in result['evidence']] == ['제1조']
    assert result['linked_sources']['C1']['number'] == '별표 14'
    trace = result['review_trace']
    assert [r['number'] for r in trace['token_budget_omissions']] == ['제2조']
    summary = next(r for r in trace['answer_context'] if r['number']=='별표 14')
    assert summary['body_chars'] == len(table['body']) and summary['image_placeholders']==0
    assert 'body' not in summary


@pytest.mark.parametrize('has_text',[True,False])
@pytest.mark.parametrize('stream',[True,False])
def test_fixed_relevance_policy_sends_available_annex_body_to_actual_answer_prompt(tmp_path,has_text,stream):
    """Real ingestion/search/graph/prompt; only the external model response is mocked."""
    store = Store(tmp_path)
    table_text = '구간 | 질량(kg) | 수량(개)\n50 이상 100 미만 | 12 | 2\n주: 예비용 1개 별도.\n계산식: M = 2 × E.'
    xml_annex = ('<별표><별표단위><별표번호>14</별표번호><별표제목>합성 수량표</별표제목>'
                 f'<별표내용>{escape(table_text) if has_text else ""}</별표내용>'
                 '<별표PDF파일링크>/example.pdf</별표PDF파일링크></별표단위></별표>')
    put(store,'합성기준',[('1','설비 수량','설비 수량은 별표 14에 따른다.')],annex=xml_annex)
    model = Provider(Settings(chat_backend='company',company_chat_model='test-company-model'))
    requests = []
    def capture(messages):
        requests.append(messages)
        return '합성 모델 응답 [1]' + ('[C1]' if has_text else '')
    model.post = lambda path,body: {'choices':[{'message':{'content':capture(body['messages'])}}]}
    if stream:
        model._on_answer_text = lambda chunk: None
        model.stream_completion = lambda messages,on_text: capture(messages)
    class Model(FakeProvider):
        def answer(self,*args,**kwargs):
            return model.answer(*args,**kwargs)
    result = run_chat(store,Model(),Settings(data_dir=tmp_path),
                      ChatRequest(question='합성기준 설비 수량은?',mode='keyword'))
    assert result['generated'] and all(e['score'] >= .85 for e in result['evidence'])
    assert len(requests)==1
    system,context = requests[0][0]['content'],requests[0][-1]['content']
    assert '수치·산식·수량·구간·단서' in system
    assert '제목·링크만 있거나' in system
    if has_text:
        assert table_text in context  # 수치·단위·경계·주석·산식이 잘리지 않습니다.
        assert '연결 참조 C1: 합성기준 별표 14' in context
        assert result['linked_sources']['C1']['number']=='별표 14'
        assert any(r['number']=='별표 14' for r in result['review_trace']['answer_context'])
    else:
        assert table_text not in context and '연결 참조 C1:' not in context
        assert not result['linked_sources']
        assert any(r['number']=='별표 14' for r in result['missing_references'])

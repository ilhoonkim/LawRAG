"""Synthetic recall/graph/gap-search cases; not authoritative legal content."""
from datetime import date
import json
from xml.sax.saxutils import escape
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.store import Store
from app.main import create_app, ChatRequest
from app.provider import Provider
from app.laws import SetupError
from app.retrieval import fragments, fuse
from app.chat_pipeline import run_chat, budgeted
from test_workflow import FakeProvider


def xml(name, articles, effective='20200101', annex=''):
    items = ''.join(f'<조문단위><조문번호>{n}</조문번호><조문제목>{escape(title)}</조문제목><조문내용>{escape(body)}</조문내용></조문단위>' for n,title,body in articles)
    return (f'<법령><기본정보><법령ID>{escape(name)}</법령ID><법령명_한글>{escape(name)}</법령명_한글><시행일자>{effective}</시행일자></기본정보><조문>{items}</조문>{annex}</법령>').encode()


def put(store, name, articles, effective='20200101', annex=''):
    return store.ingest(xml(name,articles,effective,annex),FakeProvider(),archive_only=True)


def source(store, version, number):
    with store.connect() as db:
        r = dict(db.execute('SELECT * FROM version_articles WHERE version=? AND number=?',(version,number)).fetchone())
        name = db.execute('SELECT name FROM law_versions WHERE version=?',(version,)).fetchone()[0]
    return {k:v for k,v in r.items() if k not in ('vector','title_vector','model')} | {'law_name':name,'score':.8}


def test_original_rewrite_union_and_law_names_never_narrow_scope(tmp_path):
    store=Store(tmp_path)
    put(store,'합성 주택법',[('1','권리','보증금 반환 보호 요건을 정한다.')])
    put(store,'합성 예외법',[('1','예외','소액임차인 우선변제 서울 기준을 정한다.')])
    result=store.search_enhanced('합성 주택법 보증금 반환',FakeProvider(),variants=['소액임차인 우선변제 서울'],mode='keyword')
    assert {r['law_name'] for r in result} == {'합성 주택법','합성 예외법'}
    assert result[0]['law_name_score'] in (0,1)
    assert all(r['body'] for r in result)


def test_title_and_law_name_alone_cannot_qualify_but_generic_heading_can(tmp_path):
    store=Store(tmp_path)
    put(store,'합성 보증금법',[('1','보증금 반환 보호','주차장 건축 구조를 정한다.'),
                               ('2','일반사항','보증금 반환 보호 기준을 정한다.')])
    rows=store.search_enhanced('합성 보증금법 보증금 반환 보호',FakeProvider(),mode='keyword')
    assert [r['number'] for r in rows] == ['제2조']
    assert rows[0]['title_score'] == 0 and rows[0]['body_score'] >= .9


def test_explicit_identifier_route_is_only_from_original_question(tmp_path):
    store=Store(tmp_path)
    put(store,'합성법',[('1','일반','다른 내용'),('2','신청','신청서 제출')])
    exact=store.search_enhanced('합성법 제1조',FakeProvider(),mode='keyword')
    assert exact[0]['number']=='제1조' and exact[0]['exact_identifier']
    rewritten=store.search_enhanced('원본에 없는 질문',FakeProvider(),mode='keyword',variants=['합성법 제1조'])
    assert not any(r['exact_identifier'] for r in rewritten)


def test_subitem_hit_restores_all_of_condition_and_proviso(tmp_path):
    store=Store(tmp_path)
    body='① 다음 각 호 모두 충족해야 한다.\n1. 신청서를 제출한다.\n2. 영수증 사본을 제출한다.\n다만, 미성년자는 대리인이 신청해야 한다.\n② 별도 조건을 정한다.'
    put(store,'합성법',[('1','접수',body)])
    result=store.search_enhanced('영수증 사본',FakeProvider(),mode='keyword')
    assert result[0]['matched_units'][0]['location']=='제1항제2호'
    assert result[0]['body']==body
    assert '모두' in result[0]['body'] and '다만' in result[0]['body']
    units=fragments(body)
    assert all(body[u['start']:u['end']].strip()==u['text'] for u in units)


def test_rrf_same_candidate_pool_deduplicates_and_shares_embedding_call(tmp_path):
    store=Store(tmp_path)
    put(store,'합성법',[('1','적용범위','적용 범위 보호'),('2','검사','검사 서류 제출')])
    class Spy(FakeProvider):
        def __init__(self): self.queries=[]
        def embed(self,texts): self.queries.append(texts); return super().embed(texts)
    provider=Spy()
    comparison=store.search_enhanced('적용 범위',provider,variants=['검사 서류'],compare=True,threshold=.2)
    assert provider.queries==[['적용 범위','검사 서류']]
    assert {r['id'] for r in comparison['weighted']}=={r['id'] for r in comparison['rrf']}
    assert comparison['threshold_basis']=='weighted_score'
    assert [r['score'] for r in comparison['rrf']]==sorted([r['score'] for r in comparison['rrf']],reverse=True)
    assert fuse([['a','a','b'],['b','c']])['b']==pytest.approx(1/62+1/61)


def graph_fixture(store):
    law=put(store,'합성보호법',[
        ('2','정의','이 법에서 임차인이란 보호 대상자를 말한다.'),
        ('8','보호','임차인 보증금 보호 범위는 대통령령으로 정한다. 제2조를 따른다.'),
        ('20','제재','제8조를 위반한 자는 제재한다.')])
    put(store,'합성보호법 시행령',[('3','금액','법 제8조에 따른 과거 금액과 별표 1의 기준을 따른다.')],effective='20200101')
    decree=put(store,'합성보호법 시행령',[('3','금액','법 제8조에 따른 현재 금액과 별표 1의 기준을 따른다.')],effective='20250101',annex='<별표><별표단위><별표번호>1</별표번호><별표제목>지역 기준</별표제목><별표내용>지역\t단위\n서울\t합성값\n주: 합성 테스트</별표내용></별표단위></별표>')
    return law,decree


def test_reverse_penalty_and_delegated_decree_are_date_aware(tmp_path):
    store=Store(tmp_path);law,decree=graph_fixture(store)
    root=source(store,law['version'],'제8조')
    current,missing=store.expand_context([root],cutoff=date(2026,10,21),question='서울 보호 금액 위반')
    assert {e['number'] for e in current}>={'제8조','제2조','제20조','제3조'}
    linked=next(e for e in current if e['law_name'].endswith('시행령'))
    assert linked['version']==decree['version'] and '현재' in linked['body']
    assert linked['relation']=='reverse_reference'
    assert '법 제8조' in linked['reference_edges'][0]['excerpt']
    old,_=store.expand_context([root],cutoff=date(2021,1,1),question='보호 금액')
    assert '과거' in next(e for e in old if e['law_name'].endswith('시행령'))['body']
    assert not any(e['version']==decree['version'] for e in old)


def test_forward_external_annex_and_alias_keep_real_target_version(tmp_path):
    store=Store(tmp_path);law,decree=graph_fixture(store)
    root=source(store,decree['version'],'제3조')
    expanded,missing=store.expand_context([root],cutoff=date(2026,10,21),question='서울 금액')
    assert not missing
    actual=next(e for e in expanded if e['law_name']=='합성보호법')
    assert actual['number']=='제8조' and actual['version']==law['version']
    annex=next(e for e in expanded if e['number']=='별표 1')
    assert '지역\t단위' in annex['body'] and '주:' in annex['body']
    assert annex['version']==decree['version']
    assert any(t['version']==law['version'] for t in expanded[0]['reference_titles'])


def test_missing_current_target_does_not_fall_back_to_old_version(tmp_path):
    store=Store(tmp_path);law,decree=graph_fixture(store)
    with store.connect() as db:
        db.execute('INSERT INTO revision_catalog VALUES(?,?,?,?,?)',('law','999','합성보호법','20260101','20251201'))
    expanded,missing=store.expand_context([source(store,decree['version'],'제3조')],cutoff=date(2026,10,21))
    assert not any(e['law_name']=='합성보호법' for e in expanded)
    assert any(m['law_name']=='합성보호법' for m in missing)


def test_unclear_reference_stays_missing_and_expansion_is_bounded(tmp_path):
    store=Store(tmp_path)
    root=put(store,'합성 지침',[('1','신청','법 제8조에 따른다. 제2조부터 제5조까지 준용한다.'),
        ('2','조건','조건2'),('3','조건','조건3'),('4','조건','조건4'),('5','조건','조건5')])
    rows,missing=store.expand_context([source(store,root['version'],'제1조')],cutoff=date(2026,10,21),limit=2)
    assert len(rows)==3
    assert any(m['law_name'].startswith('법') for m in missing)
    assert any('제한' in m['reason'] for m in missing)


def assessment(queries=(),issues=(),facts=(),sufficient=False):
    return {'sufficient':sufficient,'missing_issues':list(issues),'missing_facts':list(facts),'search_queries':list(queries)}


class GapProvider(FakeProvider):
    def __init__(self, assessments): self.assessments=iter(assessments); self.received=[]
    def rewrite_question(self, question): return '보증금 보호'
    def assess_evidence(self,question,evidence,missing_references,as_of):
        self.received.append((question,[e['body'] for e in evidence],as_of))
        return next(self.assessments)
    def answer(self,question,evidence,history,missing_references=None,assessment=None):
        self.final=evidence
        self.assessment=assessment
        return '합성 답변 [1]'


def test_gap_queries_find_new_content_without_rewriting_original_or_date(tmp_path):
    provider=GapProvider([assessment(['서울 금액'],['지역 금액 누락']),assessment(sufficient=True)])
    app=create_app(Settings(data_dir=tmp_path),provider,object())
    put(app.state.store,'합성 보호법',[('1','보호','보증금 보호 범위를 정한다.')])
    put(app.state.store,'합성 금액법',[('1','금액','서울 금액은 별도로 정한다.')])
    question='2021-01-01 기준 보증금 보호'
    stages=[]
    result=run_chat(app.state.store,provider,Settings(data_dir=tmp_path),ChatRequest(question=question,mode='keyword'),stages.append)
    assert result['additional_search_rounds']==1 and result['additional_queries']==['서울 금액']
    assert result['original_question']==question and result['as_of']=='2021-01-01'
    assert len(provider.received)==2 and all(q==question and d=='2021-01-01' for q,_,d in provider.received)
    assert not any('서울' in b for b in provider.received[0][1])
    assert any('서울' in b for b in provider.received[1][1])
    assert stages==['rewrite','search','review','assess','additional_search','review','assess','answer']


def test_user_facts_only_dont_trigger_search_and_remaining_gaps_are_preserved(tmp_path):
    provider=GapProvider([assessment(['계약일 조회'],facts=['계약일'])])
    app=create_app(Settings(data_dir=tmp_path),provider,object())
    put(app.state.store,'합성법',[('1','보호','보증금 보호')])
    result=run_chat(app.state.store,provider,Settings(),ChatRequest(question='보증금 보호',mode='keyword'))
    assert result['additional_search_rounds']==0 and provider.assessment['missing_facts']==['계약일']


def test_linked_article_independently_found_in_extra_search_gets_its_own_score(tmp_path):
    provider=GapProvider([assessment(['서울 금액'],['지역 금액 확인']),assessment(sufficient=True)])
    store=Store(tmp_path)
    put(store,'합성 보호법',[('1','보호','보증금 보호 범위는 제2조에 따른다.'),
                             ('2','금액','서울 금액은 별도로 정한다.')])
    result=run_chat(store,provider,Settings(),ChatRequest(question='보증금 보호',mode='keyword'))
    assert result['additional_search_rounds']==1
    hit=next(e for e in result['evidence'] if e['number']=='제2조')
    assert not hit.get('relation') and hit['matched_query']=='서울 금액'


def test_extra_search_stops_after_one_round_even_when_still_insufficient(tmp_path):
    provider=GapProvider([assessment(['서울 금액'],['금액 없음']),assessment(['세번째 검색'],['여전히 금액 없음'])])
    app=create_app(Settings(data_dir=tmp_path),provider,object())
    put(app.state.store,'합성법',[('1','보호','보증금 보호')])
    result=run_chat(app.state.store,provider,Settings(),ChatRequest(question='보증금 보호',mode='keyword'))
    assert result['additional_search_rounds']==1 and not result['assessment']['sufficient']
    assert provider.assessment['missing_issues']==['여전히 금액 없음']


def test_duplicate_query_not_retried_and_budget_never_truncates_proviso(tmp_path):
    provider=GapProvider([assessment(['보증금  보호?'],['부족'])])
    app=create_app(Settings(data_dir=tmp_path),provider,object())
    put(app.state.store,'합성법',[('1','보호','보증금 보호')])
    assert run_chat(app.state.store,provider,Settings(),ChatRequest(question='보증금 보호',mode='keyword'))['additional_search_rounds']==0
    large={'version':'v','law_name':'법','number':'제1조','body':'x'*100+'다만, 예외','score':1}
    rows,missing=budgeted([large],50)
    assert rows==[] and missing[0]['number']=='제1조'
    assert large['body'].endswith('다만, 예외')


@pytest.mark.parametrize('payload',[{}, {'sufficient':'yes'}, assessment(['x']*3), assessment(facts=[None])])
def test_sufficiency_requires_structured_output(monkeypatch,payload):
    provider=Provider(Settings(api_key='test-only'))
    monkeypatch.setattr(provider,'post',lambda *a:{'choices':[{'message':{'content':json.dumps(payload)}}]})
    with pytest.raises(SetupError,match='충분성'):
        provider.assess_evidence('질문',[],[],'2026-10-21')


def test_api_exposes_both_fusions_and_no_reranker(tmp_path):
    app=create_app(Settings(data_dir=tmp_path),FakeProvider(),object())
    put(app.state.store,'합성법',[('1','적용범위','적용 범위 보호')])
    response=TestClient(app).post('/api/search/compare',json={'question':'적용 범위','mode':'keyword'})
    assert response.status_code==200
    assert response.json()['weighted'] and response.json()['rrf']


def test_coherent_item_match_ranks_above_scattered_words(tmp_path):
    store=Store(tmp_path)
    put(store,'합성법',[('1','일반','① 제출 절차\n1. 영수증 사본\n2. '+ '무관한 내용 '*100),
                         ('2','일반','① 영수증\n'+ '무관한 내용 '*100 +'\n② 사본')])
    result=store.search_enhanced('영수증 사본',FakeProvider(),mode='keyword')
    assert result[0]['number']=='제1조'
    assert result[0]['body_keyword_score']>result[1]['body_keyword_score']


def test_reverse_provenance_and_declared_alias_use_actual_source(tmp_path):
    from app.graph import aliases, resolve
    declared=aliases('「합성보호법」(이하 "법"이라 한다)')
    assert resolve('법','별도 지침',declared)=='합성보호법'
    conflicting=aliases('「A법」(이하 "법"이라 한다) 「B법」(이하 "법"이라 한다)')
    assert '확인 필요' in resolve('법','A법 시행령',conflicting)
    store=Store(tmp_path);law,decree=graph_fixture(store)
    expanded,_=store.expand_context([source(store,law['version'],'제8조')],cutoff=date(2026,10,21))
    reverse=next(e for e in expanded if e['law_name'].endswith('시행령'))
    edge=reverse['reference_edges'][0]
    assert edge['from_version']==decree['version'] and edge['to_version']==law['version']
    assert edge['from_article']=='제3조' and edge['to_article']=='제8조'


def test_edge_excerpt_distinguishes_same_number_in_two_laws():
    from app.graph import links
    row={'law_name':'합성법','number':'제1조','body':'「A법」 제 8 조의2를 준용한다.\n「B법」 제 8 조에 따른다.\n「A법」 제8조를 준용한다.'}
    found=links(row,{})
    assert any(number=='제8조' and law=='B법' and '「B법」 제 8 조' in excerpt for number,law,_,excerpt in found)
    assert any(number=='제8조의2' and law=='A법' for number,law,_,_ in found)


def test_rejected_source_cannot_reappear_in_gap_search(tmp_path):
    class RejectingProvider(GapProvider):
        def filter_evidence(self,question,candidates):
            return [c for c in candidates if c['title']!='제외 근거']
    provider=RejectingProvider([assessment(['별도 금액'],['금액 누락']),assessment(sufficient=True)])
    app=create_app(Settings(data_dir=tmp_path),provider,object())
    put(app.state.store,'합성법',[('1','보호','보증금 보호'),('2','제외 근거','보증금 보호 별도 금액')])
    result=run_chat(app.state.store,provider,Settings(),ChatRequest(question='보증금 보호',mode='keyword'))
    assert result['additional_search_rounds']==1
    assert all(e['number']!='제2조' for e in result['evidence'])
    assert all(e['number']!='제2조' for e in provider.final)

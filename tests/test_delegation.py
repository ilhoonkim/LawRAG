"""Synthetic fixtures only: these do not assert the live DRF response or legal content."""
from datetime import date
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import delegation
from app.backfill_delegation import backfill
from app.chat_pipeline import run_chat
from app.config import Settings
from app.laws import LawAPI, SetupError
from app.main import ChatRequest, create_app
from app.management import Management
from app.provider import Provider
from app.store import Store
from test_enhanced_retrieval import source
from test_hierarchy_collection import HierarchyAPI, ROWS, request
from test_workflow import FakeProvider


def payload():
    basic = {'삼단비교존재여부':'Y', '삼단비교기준':'L'}
    for prefix, row in zip(('법령','시행령','시행규칙'), ROWS):
        basic[prefix+'명'] = row['name']
        basic[prefix+'ID'] = row['law_id']
        value = row['promulgated']
        basic[prefix+'요약정보'] = f'[합성 제1호, {value[:4]}. {int(value[4:6])}. {int(value[6:])}., 일부개정]'
    def article(n, title, body):
        return {'조번호':str(n).zfill(4), '조가지번호':'00', '조제목':title, '조내용':body}
    root = article(2, '검사', '① 검사 신청서를 제출한다.')
    root['시행령조문'] = article(2, '검사', '① 검사 신청서를 제출한다.')
    root['시행규칙조문'] = [article(7, '서류', '① 영 제2조제2항에 따른 서류를 제출한다.')]
    root['위임행정규칙목록'] = {'위임행정규칙': {'위임행정규칙명':'무시할 지침'}}
    return {'LspttnThdCmpLawXService': {'기본정보':basic, '위임조문삼단비교':{'법률조문':[root]}}}


class DelegationAPI(HierarchyAPI):
    def __init__(self):
        super().__init__()
        self.calls, self.response, self.fail_delegation = [], payload(), False
    def fetch(self, mst, **kw):
        raw = super().fetch(mst, **kw)
        row = next(r for r in self.rows if r['id']==mst)
        return raw.replace(f'<법령ID>{row["name"]}</법령ID>'.encode(), f'<법령ID>{row["law_id"]}</법령ID>'.encode())
    def delegation(self, mst):
        self.calls.append(mst)
        if self.fail_delegation:
            raise SetupError('위임 비교 API 권한 확인 필요')
        return json.dumps(self.response, ensure_ascii=False).encode()


@pytest.fixture
def manager(tmp_path):
    return Management(Store(tmp_path), FakeProvider(), DelegationAPI())


def collect(manager):
    plan = manager.plan_collection(request())
    queued = manager.queue_plan(plan['plan_id'])
    manager.run(queued['ids'], plan_id=plan['plan_id'])
    with manager.store.connect() as db:
        versions = {r['mst']:r['version'] for r in db.execute('SELECT mst,version FROM law_versions')}
    return versions


def test_adapter_requests_delegation_not_citation(monkeypatch):
    api = LawAPI('test')
    calls = []
    monkeypatch.setattr(api, 'request', lambda endpoint, **params: calls.append((endpoint,params)) or b'{}')
    api.delegation('100')
    assert calls == [('lawService.do', {'target':'thdCmp','MST':'100','knd':'2','type':'JSON'})]
    with pytest.raises(ValueError):
        api.delegation('bad')


def test_parser_objects_arrays_branch_numbers_and_no_cartesian_product():
    data = payload()
    root = data['LspttnThdCmpLawXService']['위임조문삼단비교']['법률조문'][0]
    root['조가지번호'] = '02'
    parsed = delegation.parse(json.dumps(data))
    assert len(parsed['edges']) == 2
    assert {r['parent']['number'] for r in parsed['edges']} == {'제2조의2'}
    assert {r['child']['level'] for r in parsed['edges']} == {'시행령','시행규칙'}
    assert parsed['edges'][0]['parent']['promulgated'] == '20221201'
    assert parsed['edges'][0]['child']['promulgated'] == '20231201'


@pytest.mark.parametrize('raw', [b'<html>permission error</html>', b'{}', b'[]',
    json.dumps({'LspttnThdCmpLawXService': {'기본정보':{}, '위임조문삼단비교':{}}}).encode()])
def test_bad_api_response_not_treated_as_no_relations(raw):
    with pytest.raises(SetupError):
        delegation.parse(raw)


def test_explicit_no_comparison_is_valid():
    data = {'LspttnThdCmpLawXService': {'기본정보':{'삼단비교존재여부':'N'}}}
    assert delegation.parse(json.dumps(data))['edges'] == []


def test_collect_persists_direction_provenance_once_per_family(manager):
    versions = collect(manager)
    assert manager.api.calls == ['100']
    with manager.store.connect() as db:
        edges = [dict(r) for r in db.execute('SELECT * FROM delegation_edges')]
        assert db.execute('SELECT count(*) FROM delegation_unresolved').fetchone()[0] == 0
        assert db.execute('SELECT payload FROM delegation_snapshots').fetchone()[0]
    assert len(edges) == 3
    assert any(r['from_version']==versions['200'] and r['to_version']==versions['300'] and r['relation']=='hierarchy_citation' for r in edges)
    assert all(r['from_version'] != versions['300'] for r in edges)
    assert delegation.status(manager.store, versions['300'])['status'] == 'completed'
    assert all(r['delegation']['status']=='completed' for r in manager.history(1)['items'])


def test_missing_or_different_content_not_bound_then_rebind_without_ai(manager):
    manager.api.fail.add('200')
    versions = collect(manager)
    assert delegation.status(manager.store, versions['300'])['status'] == 'partial'
    manager.api.fail.clear()
    # Intake companion without touching the already saved API response.
    manager.store.ingest(manager.api.fetch('200', effective='20240101'), manager.provider, mst='200', archive_only=True)
    delegation.bind(manager.store, '100')
    assert delegation.status(manager.store, versions['300'])['status'] == 'completed'
    assert manager.api.calls == ['100']
    manager.api.response['LspttnThdCmpLawXService']['위임조문삼단비교']['법률조문'][0]['조내용'] += '개정된 조건'
    delegation.refresh(manager.store, manager.api, versions['100'])
    assert delegation.status(manager.store, versions['100'])['status'] == 'partial'
    assert delegation.active_edges(manager.store, date(2026,10,22)) == []


def test_old_request_does_not_bind_new_response_to_old_body(manager):
    data = manager.api.response['LspttnThdCmpLawXService']['기본정보']
    data['법령요약정보'] = '[합성 제2호, 2026. 1. 1., 일부개정]'
    versions = collect(manager)
    assert delegation.status(manager.store, versions['100'])['status'] == 'partial'
    assert delegation.active_edges(manager.store, date(2026,10,22)) == []


def test_missing_dates_or_ambiguous_effective_versions_rejected(manager):
    versions = collect(manager)
    with manager.store.connect() as db:
        # Same promulgation/content, distinct effective version: cannot select by name/article alone.
        db.execute('''INSERT INTO law_versions SELECT 'ambiguous',law_id,name,kind,'101',promulgated,'20230201',collected_at,html,source_url
            FROM law_versions WHERE version=?''', (versions['100'],))
        db.execute('''INSERT INTO version_articles SELECT 'ambiguous',position,number,title,body,section_id,chapter,vector,model,title_vector
            FROM version_articles WHERE version=?''', (versions['100'],))
    delegation.bind(manager.store, '100')
    assert delegation.status(manager.store, versions['100'])['status'] == 'partial'
    data = manager.api.response['LspttnThdCmpLawXService']['기본정보']
    data['법령요약정보'] = ''
    delegation.refresh(manager.store, manager.api, versions['100'])
    assert delegation.status(manager.store, versions['100'])['status'] == 'partial'


def test_failures_separate_from_body_and_retry_no_reembedding(manager):
    manager.api.fail_delegation = True
    versions = collect(manager)
    assert all(r['status']=='completed' for r in manager.history(1)['items'])
    assert delegation.status(manager.store, versions['100'])['status'] == 'failed'
    manager.api.fail_delegation = False
    manager.provider = object()
    manager.api.fetch = lambda *a, **k: pytest.fail('no body downloads')
    report = backfill(manager.store, manager.api, notify=lambda _:None)
    assert report['ok'] and manager.api.calls == ['100','100']
    assert backfill(Store(manager.store.directory), manager.api, notify=lambda _:None)['ok']
    assert manager.api.calls == ['100','100']


def test_two_way_expansion_no_siblings_and_date_safe(manager):
    versions = collect(manager)
    roots = [source(manager.store, versions['300'], '제7조')]
    rows, _ = delegation.expand(manager.store, roots, date(2026,10,22))
    assert {r['version'] for r in rows} == {versions['100'],versions['200']}
    assert all(r['include_in_sources'] for r in rows)
    lower, _ = delegation.expand(manager.store, [source(manager.store, versions['100'], '제2조')], date(2026,10,22))
    assert {r['version'] for r in lower} == {versions['200'],versions['300']}
    assert delegation.expand(manager.store, roots, date(2025,1,1))[0] == []
    limited, missing = delegation.expand(manager.store, roots, date(2026,10,22), limit=1)
    assert len(limited)==1 and missing
    with manager.store.connect() as db:
        db.execute('INSERT INTO revision_catalog VALUES(?,?,?,?,?)', ('law','201',ROWS[1]['name'],'20261001','20260901'))
    assert all(e['from_version'] != versions['200'] and e['to_version'] != versions['200'] for e in delegation.active_edges(manager.store,date(2026,10,22)))


def test_no_decree_rule_edge_without_explicit_reference(manager):
    collect(manager)
    with manager.store.connect() as db:
        db.execute("UPDATE version_articles SET body='독립된 규정이다.' WHERE version IN (SELECT version FROM law_versions WHERE mst='300') AND number='제7조'")
    manager.api.response['LspttnThdCmpLawXService']['위임조문삼단비교']['법률조문'][0]['시행규칙조문'][0]['조내용']='독립된 규정이다.'
    with manager.store.connect() as db:
        version = db.execute("SELECT version FROM law_versions WHERE mst='300'").fetchone()[0]
    delegation.refresh(manager.store,manager.api,version)
    with manager.store.connect() as db:
        assert db.execute("SELECT count(*) FROM delegation_edges WHERE relation='hierarchy_citation'").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM delegation_edges').fetchone()[0]==2


def test_html_alias_and_delegation_cross_document_links(manager):
    versions = collect(manager)
    app = create_app(Settings(data_dir=manager.store.directory), FakeProvider(), manager.api)
    manager.api.fetch = lambda *a,**k: pytest.fail('view must not download')
    with TestClient(app) as client:
        response = client.get('/documents/'+versions['300']+'.html?as_of=2026-10-22')
        assert response.status_code == 200
        target = '/documents/'+versions['200']+'.html?as_of=2026-10-22#'+quote(ROWS[1]['name']+'-제2조',safe='')
        assert target in response.text
        assert '상위 위임조문 비교' in response.text
        assert '/documents/'+versions['100']+'.html?as_of=2026-10-22#' in response.text
        base = client.get('/documents/'+versions['100']+'.html?as_of=2026-10-22')
        assert '하위 위임조문 비교' in base.text
        assert '/documents/'+versions['300']+'.html?as_of=2026-10-22#' in base.text


def test_chat_sends_upper_lower_sources_to_llm_and_returns_numbered_sources(manager, monkeypatch):
    versions = collect(manager)
    root = source(manager.store,versions['300'],'제7조')
    monkeypatch.setattr(manager.store, 'search_enhanced', lambda *a,**kw:[root])
    class Capture(FakeProvider):
        def answer(self, question, evidence, history, **kwargs):
            self.context = evidence
            return '서류 근거 [1][2][3]'
    provider = Capture()
    result = run_chat(manager.store,provider,Settings(),ChatRequest(question='검사 서류는?',as_of=date(2026,10,22)))
    assert {r['version'] for r in result['evidence']} >= {versions['100'],versions['200'],versions['300']}
    assert result['hierarchy_source_count'] >= 2
    assert all(r in provider.context for r in result['evidence'])
    actual = Provider(Settings(api_key='test'))
    calls = []
    monkeypatch.setattr(actual,'post',lambda path,payload:calls.append(payload) or {'choices':[{'message':{'content':'답변'}}]})
    actual.answer('질문',provider.context,[])
    text = calls[-1]['messages'][-1]['content']
    for i, row in enumerate(result['evidence'],1):
        assert f'[{i}] {row["law_name"]} {row["number"]}' in text
    assert 'thdCmp' in text


def test_summary_dates_distinguish_effective_and_promulgated():
    value = '[시행 2023. 6. 28.] [법률 제19134호, 2022. 12. 27., 일부개정]'
    assert delegation.summary_effective(value)=='20230628'
    assert delegation.summary_date(value)=='20221227'
    assert delegation.summary_date('[시행 2023. 6. 28.]')==''
    assert delegation.summary_date('2023. 1. 1. 또는 2024. 1. 1.')==''


def test_unresolved_target_is_forwarded_to_sufficiency_check(manager):
    manager.api.fail.add('200')
    versions = collect(manager)
    missing = delegation.missing_context(manager.store,[source(manager.store,versions['100'],'제2조')])
    assert any(r['law_name']=='합성안전법 시행령' and r['number']=='제2조' for r in missing)
    assert all(r['from_article']=='제2조' for r in missing)


def test_same_promulgation_and_unchanged_body_does_not_prove_historical_effective_version(manager):
    versions = collect(manager)
    with manager.store.connect() as db:
        # One promulgation can have multiple effective versions with an unchanged article.
        # The later version is known but missing: the old stored body must not stand in for it.
        db.execute('INSERT INTO revision_catalog VALUES(?,?,?,?,?)', ('law','101',ROWS[0]['name'],'20260101',ROWS[0]['promulgated']))
    delegation.bind(manager.store,'100')
    assert delegation.status(manager.store,versions['100'])['status']=='partial'
    with manager.store.connect() as db:
        assert db.execute('SELECT count(*) FROM delegation_edges').fetchone()[0]==0

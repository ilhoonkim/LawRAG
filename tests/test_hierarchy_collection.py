"""Synthetic API fixtures: hierarchy intake, retry and date-bound cross-law links."""
from copy import deepcopy
from datetime import date
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hierarchy import parse_hierarchy
from app.laws import LawAPI, SetupError
from app.main import create_app
from app.management import CollectionInput, Management
from app.store import Store
from test_enhanced_retrieval import xml, source
from test_workflow import FakeProvider


ROWS = [
    dict(id='100', law_id='1', name='합성안전법', kind='law', effective='20230101', promulgated='20221201'),
    dict(id='200', law_id='2', name='합성안전법 시행령', kind='law', effective='20240101', promulgated='20231201'),
    dict(id='300', law_id='3', name='합성안전법 시행규칙', kind='law', effective='20260101', promulgated='20251201'),
    # 이름이 본법+'시행규칙'이 아닌 분기와 행정규칙 제외를 함께 확인합니다.
    dict(id='400', law_id='4', name='합성 검사 수수료 규칙', kind='law', effective='20210101', promulgated='20201201'),
]


def tree_payload():
    def node(row, **children):
        return {'기본정보': {'법령명': row['name'], '법령ID': row['law_id'], '법령일련번호': row['id'],
                '시행일자': row['effective'], '공포일자': row['promulgated']}, **children}
    return {'법령체계도': {'상하위법': {'법률': node(ROWS[0], 시행령=node(ROWS[1], 시행규칙=node(ROWS[2])),
        시행규칙=[node(ROWS[3])], 행정규칙={'고시': {'기본정보': {'행정규칙명': '수집하면 안 되는 고시'}}})}}}


def tree():
    return parse_hierarchy(json.dumps(tree_payload(), ensure_ascii=False).encode())


class HierarchyAPI:
    def __init__(self):
        self.rows, self.tree = deepcopy(ROWS), tree()
        self.searched, self.fetched = [], []
        self.fail = set()

    def hierarchy(self, mst):
        return self.tree

    def delegation(self, mst):
        # 이 fixture는 체계도 테스트용으로, 비교 자료가 없다는 정상 응답을 명시합니다.
        # 실제 위임 응답·조문 매핑은 test_delegation의 별도 합성 fixture에서 검사합니다.
        return json.dumps({'LspttnThdCmpLawXService': {'기본정보': {'삼단비교존재여부': 'N'}}}).encode()

    def search(self, query, page=1, kind='law', revisions=False, display=100):
        self.searched.append(query)
        rows = [r for r in self.rows if r['name'] == query]
        return {'laws': rows[(page-1)*display:page*display], 'total': len(rows)}

    def fetch(self, mst, kind='law', effective=''):
        self.fetched.append(mst)
        if mst in self.fail:
            raise SetupError('본문 다운로드 실패')
        row = next(r for r in self.rows if r['id'] == mst and r['effective'] == effective)
        body = '① 검사 신청서를 제출한다.'
        if mst == '300':
            body = '「합성안전법」(이하 “법”이라 한다) 및 「합성안전법 시행령」(이하 “영”이라 한다).\n① 법 제2조제1항 및 영 제2조제2항을 따른다.'
        return xml(row['name'], [('2', '검사', body), ('7', '서류', '① 영 제2조제2항에 따른 서류를 제출한다.')], row['effective']).replace(
            b'</' + '기본정보'.encode() + b'>', ('<공포일자>'+row['promulgated']+'</공포일자></기본정보>').encode())


def request(mst='300', name='합성안전법 시행규칙', effective='20260101'):
    return CollectionInput(items=[dict(mst=mst, name=name, effective=effective)])


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr('app.hierarchy.today_in_korea', lambda: date(2026,10,22))
    monkeypatch.setattr('app.management.today_in_korea', lambda: date(2026,10,22))
    return Management(Store(tmp_path), FakeProvider(), HierarchyAPI())


def test_official_tree_preserves_branching_and_only_three_legal_types():
    result = tree()
    assert len(result['nodes']) == 4
    assert result['edges'] == [{'parent': '1', 'child': '2'}, {'parent': '2', 'child': '3'}, {'parent': '1', 'child': '4'}]
    assert {n['level'] for n in result['nodes']} == {'법률', '시행령', '시행규칙'}
    assert result['nodes'][0]['effective'] == '20230101'


def test_api_adapter_requests_selected_mst_json(monkeypatch):
    api = LawAPI('not-a-real-key')
    calls = []
    def fake_request(endpoint, **params):
        calls.append((endpoint, params))
        return json.dumps(tree_payload()).encode()
    monkeypatch.setattr(api, 'request', fake_request)
    assert len(api.hierarchy('300')['nodes']) == 4
    assert calls == [('lawService.do', {'target': 'lsStmd', 'MST': '300', 'type': 'JSON'})]


@pytest.mark.parametrize('raw', [b'<html>permission denied</html>', b'{}', '{"법령체계도":{}}'.encode()])
def test_invalid_or_empty_tree_is_not_no_relatives(raw):
    with pytest.raises(SetupError, match='체계도'):
        parse_hierarchy(raw)


def test_plan_prefers_snapshot_versions_and_skips_companion_history(manager):
    plan = manager.plan_collection(request())
    assert {r['mst'] for r in plan['items']} == {'100','200','300','400'}
    # 선택 법령의 종료일만 연혁에서 확인합니다. 완전한 동반 버전 정보는 그대로 사용.
    assert manager.api.searched == ['합성안전법 시행규칙']
    assert plan['new_count'] == 4 and not manager.api.fetched
    with manager.store.connect() as db:
        assert db.execute('SELECT count(*) FROM law_hierarchy_snapshots').fetchone()[0] == 4
        assert db.execute('SELECT count(*) FROM revision_catalog').fetchone()[0] == 4
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0] == 0


@pytest.mark.parametrize('bad_field,bad_value', [('effective', '20990101'), ('mst', ''), ('promulgated', '')])
def test_incomplete_or_future_companion_uses_effective_history(manager, bad_field, bad_value):
    manager.api.tree['nodes'][0][bad_field] = bad_value
    plan = manager.plan_collection(request())
    parent = next(r for r in plan['items'] if r['mst'] == '100')
    assert parent['effective'] == '20230101'  # earlier than rule, still effective
    assert manager.api.searched == ['합성안전법 시행규칙', '합성안전법']


def test_historical_selection_never_uses_post_period_tree_version(manager):
    manager.api.rows.append(dict(ROWS[2], id='299', effective='20200101', promulgated='20191201'))
    manager.api.rows.append(dict(ROWS[1], id='201', effective='20260101', promulgated='20251201'))
    manager.api.tree['nodes'][1].update(mst='201', effective='20260101', promulgated='20251201')
    plan = manager.plan_collection(request('299', effective='20200101'))
    assert plan['families'][0]['as_of'] == '20251231'
    assert '200' in {r['mst'] for r in plan['items']}
    assert '201' not in {r['mst'] for r in plan['items']}


def test_partial_failure_and_retry_only_fetch_missing_companion(manager):
    plan = manager.plan_collection(request())
    manager.api.fail.add('200')
    batch = manager.queue_plan(plan['plan_id'])
    assert batch['queued'] == 4
    manager.run(batch['ids'])
    history = manager.history(1, batch['batch'])
    assert {r['status'] for r in history['items']} == {'completed','failed'}
    assert all(r['hierarchy_status'] == 'incomplete' and r['hierarchy_missing'] == 1 for r in history['items'])
    manager.api.fail.clear()
    manager.api.fetched.clear()
    retry = manager.plan_collection(request())
    assert retry['new_count'] == 1
    batch = manager.queue_plan(retry['plan_id'])
    manager.run(batch['ids'])
    assert manager.api.fetched == ['200']
    assert manager.history(1, batch['batch'])['items'][0]['hierarchy_status'] == 'completed'


def test_missing_companion_warns_instead_of_claiming_complete(manager):
    manager.api.tree['nodes'][0]['mst'] = ''
    manager.api.rows = [r for r in manager.api.rows if r['id'] != '100']
    plan = manager.plan_collection(request())
    assert plan['warnings'] and len(plan['items']) == 3
    result = manager.queue_plan(plan['plan_id'])
    manager.run(result['ids'])
    assert manager.history(1)['items'][0]['hierarchy_status'] == 'incomplete'


def test_expired_plan_requires_rechecking(manager):
    plan = manager.plan_collection(request())
    with manager.store.connect() as db:
        db.execute("UPDATE collection_plans SET created_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(ValueError, match='만료'):
        manager.queue_plan(plan['plan_id'])
    assert manager.history(1)['total'] == 0


def test_plan_endpoint_collection_and_rule_law_decree_links(manager):
    app = create_app(Settings(data_dir=manager.store.directory), FakeProvider(), manager.api)
    with TestClient(app) as client:
        preview = client.post('/api/management/collection-plan', json=request().model_dump())
        assert preview.status_code == 200
        queued = client.post('/api/management/collections', json={'plan_id': preview.json()['plan_id']})
        assert queued.status_code == 202 and queued.json()['queued'] == 4
        with manager.store.connect() as db:
            versions = {r['mst']: r['version'] for r in db.execute('SELECT * FROM law_versions')}
        rule_url = '/documents/' + versions['300'] + '.html?as_of=2026-10-22'
        response = client.get(rule_url)
        assert response.status_code == 200
        for mst, name in [('100', '합성안전법'), ('200', '합성안전법 시행령')]:
            url = '/documents/' + versions[mst] + '.html?as_of=2026-10-22#' + quote(name+'-제2조', safe='')
            assert url in response.text
            target = client.get(url)
            assert target.status_code == 200 and f'id="{name}-제2조"' in target.text
        # 영은 같은 규칙의 제2조로 잘못 연결하면 안 됩니다.
        assert '영 <a href="/documents/'+versions['200'] in response.text
        assert '제2조제2항' not in response.text  # clickable title plus preserved paragraph suffix
        assert '제2조(검사)</a>제2항' in response.text
        evidence = source(manager.store, versions['300'], '제7조')
        expanded, _ = manager.store.expand_context([evidence], cutoff=date(2026,10,22), limit=10)
        assert any(e['law_name'] == '합성안전법 시행령' and e['number'] == '제2조' for e in expanded)


def test_explicit_alias_and_missing_latest_version_do_not_fall_back(manager):
    result = manager.queue_plan(manager.plan_collection(request())['plan_id'])
    manager.run(result['ids'])
    with manager.store.connect() as db:
        rule = db.execute("SELECT version FROM law_versions WHERE mst='300'").fetchone()[0]
        decree = db.execute("SELECT version FROM law_versions WHERE mst='200'").fetchone()[0]
        db.execute("INSERT INTO revision_catalog VALUES('law','201','합성안전법 시행령','20260701','20260601')")
    app = create_app(Settings(data_dir=manager.store.directory), FakeProvider(), manager.api)
    manager.api.fetch = lambda *args, **kwargs: pytest.fail('document viewing must not call API')
    with TestClient(app) as client:
        old = client.get('/documents/'+rule+'.html?as_of=2026-03-01')
        current = client.get('/documents/'+rule+'.html?as_of=2026-10-22')
        assert '/documents/'+decree+'.html?as_of=2026-03-01' in old.text
        assert '/documents/'+decree+'.html' not in current.text
        assert '영 제2조제2항' in current.text
        assert client.get('/documents/'+rule+'.html?as_of=2020-01-01').status_code == 400


def test_official_ancestor_without_suffix_or_declaration_resolves_yeong(manager):
    # '합성 검사 수수료 규칙'처럼 접미사로 본법을 유추할 수 없는 경우에도 공식
    # 부모가 시행령이면 영을 찾습니다. 임의의 형제 시행령으로는 연결하지 않습니다.
    manager.api.tree['edges'][-1] = {'parent': '2', 'child': '4'}
    result = manager.queue_plan(manager.plan_collection(request())['plan_id'])
    manager.run(result['ids'])
    with manager.store.connect() as db:
        version = db.execute("SELECT version FROM law_versions WHERE mst='400'").fetchone()[0]
    evidence = source(manager.store, version, '제7조')
    expanded, _ = manager.store.expand_context([evidence], cutoff=date(2026,10,22), limit=10)
    assert any(e['law_name'] == '합성안전법 시행령' for e in expanded)

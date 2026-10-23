"""Web users and old environment settings cannot change search policy."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.retrieval import select_candidates
from app.store import Store
from test_enhanced_retrieval import put
from test_workflow import FakeProvider


@pytest.mark.parametrize('endpoint', ['/api/search', '/api/chat', '/api/chat/stream'])
@pytest.mark.parametrize('overrides', [{}, {'threshold': 0, 'fusion': 'weighted'},
                                     {'threshold': 1, 'fusion': 'weighted'},
                                     {'rrf_threshold': 0, 'relevance_threshold': 0}])
def test_fixed_policy_reaches_retrieval_and_returned_scores(tmp_path, monkeypatch, endpoint, overrides):
    class Provider(FakeProvider):
        def answer(self, *args, **kwargs):
            return '합성 근거 답변 [1]'

    app = create_app(Settings(data_dir=tmp_path), Provider(), object())
    store = app.state.store
    put(store, '합성법', [('1', '적용 범위 보호 요건', '적용 범위 보호 요건'),
                         ('2', '일반', '적용 범위 보호를 안내한다.')])
    question = '적용 범위 보호 요건'
    # 낮은 임계치로는 약한 후보도 나오므로 0.3 고정 여부를 실제 검색으로 구별합니다.
    loose = store.search_enhanced(question, Provider(), threshold=0, mode='keyword')
    assert any(0 < e['score'] < .3 for e in loose)
    calls = []
    original = store.search_enhanced

    def search(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'search_enhanced', search)
    with TestClient(app) as client:
        response = client.post(endpoint, json={'question': question, 'mode': 'keyword', **overrides})
    assert response.status_code == 200
    if endpoint.endswith('/stream'):
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        result = next(e['result'] for e in events if e['type'] == 'result')
        assert not any(e['type'] == 'error' for e in events)
    else:
        result = response.json()
    assert calls and all(c['threshold'] == .3 and c['fusion'] == 'rrf' and c['rrf_threshold'] == .85
                         for c in calls)
    assert [e['number'] for e in result['evidence']] == ['제1조']
    assert all(.3 <= e['weighted_score'] < 1 and e['score'] == e['rrf_score']
               and e['score'] >= .85 and e['fusion'] == 'rrf' for e in result['evidence'])
    if endpoint != '/api/search':
        assert result['review_trace']['relevance_threshold'] == .85
        assert result['review_trace']['relevance_threshold_basis'] == 'rrf_score'


def test_status_and_schema_publish_fixed_policy(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), FakeProvider(), object())) as client:
        status = client.get('/api/status').json()
        assert status['threshold'] == .3 and status['search_fusion'] == 'rrf'
        assert status['relevance_threshold'] == .85 and status['relevance_threshold_basis'] == 'rrf_score'
        schemas = client.get('/openapi.json').json()['components']['schemas']
    for name in ('SearchRequest', 'ChatRequest'):
        assert not {'threshold', 'fusion', 'rrf_threshold', 'relevance_threshold'} & schemas[name]['properties'].keys()


def test_stale_environment_cannot_override_or_break_startup(monkeypatch):
    monkeypatch.setenv('SEARCH_THRESHOLD', 'invalid-old-value')
    monkeypatch.setenv('SEARCH_FUSION', 'weighted')
    monkeypatch.setenv('SEARCH_RELEVANCE_THRESHOLD', '0')
    result = subprocess.run([sys.executable, '-c',
        'from app.config import Settings, SEARCH_FUSION, SEARCH_THRESHOLD, SEARCH_RELEVANCE_THRESHOLD; '
        'Settings(); print(SEARCH_FUSION, SEARCH_THRESHOLD, SEARCH_RELEVANCE_THRESHOLD)'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'rrf 0.3 0.85'


def test_relevance_boundary_and_keyword_reservation_cannot_bypass_cutoff():
    rows = [{'version':'v', 'number':str(i), 'score':score, 'body_focus_score':focus,
             'exact_identifier':exact} for i, (score, focus, exact) in enumerate([
                 (.849999, 1, True), (.85, 0, False), (.9, 0, False), (.7, 1, False)])]
    assert [r['score'] for r in select_candidates(rows,15,min_score=.85)] == [.9,.85]


def test_real_rrf_cutoff_is_applied_before_limit_and_reported(tmp_path):
    store = Store(tmp_path)
    put(store, '합성보호법', [(str(i), '보호 요건', '보호 요건을 확인한다.') for i in range(1,26)])
    trace = {}
    rows = store.search_enhanced('합성보호법 보호 요건', FakeProvider(), mode='keyword',
        fusion='rrf',threshold=.3,rrf_threshold=.85,limit=15,diagnostics=trace)
    assert 0 < len(rows) < 15
    assert all(r['score'] >= .85 for r in rows)
    rejected = [r for r in trace['candidates'] if r['stage']=='below_relevance_threshold']
    assert rejected and all(r['admission_score'] >= .3 and r['rrf_score'] < .85 for r in rejected)
    assert {r['id'] for r in rows} == {r['id'] for r in trace['candidates'] if r['stage']=='selected'}
    assert trace['relevance_threshold'] == .85 and trace['relevance_threshold_basis'] == 'rrf_score'

"""Web users and old environment settings cannot change search policy."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_enhanced_retrieval import put
from test_workflow import FakeProvider


@pytest.mark.parametrize('endpoint', ['/api/search', '/api/chat', '/api/chat/stream'])
@pytest.mark.parametrize('overrides', [{}, {'threshold': 0, 'fusion': 'weighted'},
                                     {'threshold': 1, 'fusion': 'weighted'}])
def test_fixed_policy_reaches_retrieval_and_returned_scores(tmp_path, monkeypatch, endpoint, overrides):
    class Provider(FakeProvider):
        def answer(self, *args, **kwargs):
            return '합성 근거 답변 [1]'

    app = create_app(Settings(data_dir=tmp_path), Provider(), object())
    store = app.state.store
    put(store, '합성법', [('1', '일반', '적용 범위 보호 요건'),
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
    assert calls and all(c['threshold'] == .3 and c['fusion'] == 'rrf' for c in calls)
    assert [e['number'] for e in result['evidence']] == ['제1조']
    assert all(.3 <= e['weighted_score'] < 1 and e['score'] == e['rrf_score']
               and e['fusion'] == 'rrf' for e in result['evidence'])


def test_status_and_schema_publish_fixed_policy(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), FakeProvider(), object())) as client:
        status = client.get('/api/status').json()
        assert status['threshold'] == .3 and status['search_fusion'] == 'rrf'
        schemas = client.get('/openapi.json').json()['components']['schemas']
    for name in ('SearchRequest', 'ChatRequest'):
        assert not {'threshold', 'fusion'} & schemas[name]['properties'].keys()


def test_stale_environment_cannot_override_or_break_startup(monkeypatch):
    monkeypatch.setenv('SEARCH_THRESHOLD', 'invalid-old-value')
    monkeypatch.setenv('SEARCH_FUSION', 'weighted')
    result = subprocess.run([sys.executable, '-c',
        'from app.config import Settings, SEARCH_FUSION, SEARCH_THRESHOLD; '
        'Settings(); print(SEARCH_FUSION, SEARCH_THRESHOLD)'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'rrf 0.3'

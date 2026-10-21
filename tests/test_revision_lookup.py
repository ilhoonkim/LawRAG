"""Regression: revisions are remote history, not one current result per title."""
import pytest
from app.laws import LawAPI, SetupError
from app.management import Management
from app.store import Store
from test_workflow import FakeProvider


def row(i, name='선박안전법', effective='20200101'):
    return dict(id=str(i), name=name, kind='law', effective=effective, promulgated='20190101', status='연혁')


def test_api_combines_scopes_all_pages_and_deduplicates(monkeypatch):
    api = LawAPI('test-oc')
    calls = []
    def request(endpoint, **params):
        calls.append(params)
        assert endpoint == 'lawSearch.do' and params['search'] == 1
        scope = params.get('nw')
        rows = ([row(i) for i in range(1, 102)] if scope == 1 else
                [row(102, effective='20260101')] if scope == 2 else
                [row(102, effective='20260101'), row(103, effective='20990101')])
        page, display = params['page'], params['display']
        xml = ''.join(f'<law><법령일련번호>{r["id"]}</법령일련번호><법령명한글>{r["name"]}</법령명한글><시행일자>{r["effective"]}</시행일자><공포일자>{r["promulgated"]}</공포일자></law>' for r in rows[(page-1)*display:page*display])
        return f'<LawSearch><totalCnt>{len(rows)}</totalCnt>{xml}</LawSearch>'.encode()
    monkeypatch.setattr(api, 'request', request)
    result = api.search('선박안전법', revisions=True, display=10)
    assert result['total'] == 103 and len(result['laws']) == 10
    assert result['laws'][0]['id'] == '103'
    assert {(r['nw'],r['page']) for r in calls} == {(1,1),(1,2),(2,1),(3,1)}
    pages = [api.search('선박안전법', page=p, revisions=True, display=10) for p in range(1,12)]
    assert len({r['id'] for page in pages for r in page['laws']}) == 103
    assert len(calls) == 4  # cache, not repeated upstream traffic when paging


def test_management_filters_exact_title_before_counting_and_paging(tmp_path):
    rows = [row(i, name='선박안전법 시행령') for i in range(1,101)]
    rows += [row(i, name='선박 안전법') for i in range(101,114)]
    class API:
        def search(self, query, page=1, kind='law', revisions=False, display=10):
            assert revisions
            return {'laws':rows[(page-1)*display:page*display], 'total':len(rows)}
    manager = Management(Store(tmp_path), FakeProvider(), API())
    first = manager.revisions('선박안전법','law',1,'api')
    second = manager.revisions('선박안전법','law',2,'api')
    assert first['total'] == second['total'] == 13
    assert len(first['items']) == 10 and len(second['items']) == 3
    assert all(i['collection_status'] == 'uncollected' for i in first['items']+second['items'])
    assert manager.history(1)['total'] == 0


@pytest.mark.parametrize('missing', [False, True])
def test_incomplete_history_must_not_be_presented_as_complete(monkeypatch, missing):
    api = LawAPI('test-oc')
    def fake(query, page, kind, display, scope=None):
        return {'laws':[] if missing and page>1 else [row(1)], 'total':201}
    monkeypatch.setattr(api, '_search_page', fake)
    with pytest.raises(ValueError,match='누락되거나 반복'):
        api.search('선박안전법', revisions=True)
    assert not api._revision_cache


def test_search_api_error_is_not_empty_history(monkeypatch):
    api = LawAPI('test-oc')
    monkeypatch.setattr(api, 'request', lambda *a, **kw: b'<error>invalid credentials</error>')
    with pytest.raises(SetupError, match='API 검색 응답'):
        api.search('선박안전법', revisions=True)

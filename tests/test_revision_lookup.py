"""Regression: revisions are remote history, not one current result per title."""
import pytest
from app.laws import LawAPI, SetupError
from app.management import Management
from app.store import Store
from test_workflow import FakeProvider


def row(i, name='선박안전법', effective='20200101'):
    return dict(id=str(i), name=name, kind='law', effective=effective, promulgated='20190101', status='연혁')


def test_effective_history_all_pages_dates_and_future_filter(monkeypatch):
    from datetime import date
    monkeypatch.setattr('app.laws.today_in_korea', lambda: date(2026, 10, 21))
    api = LawAPI('test-oc')
    calls = []
    # More than two upstream pages; one MST has two eligible effective dates.
    rows = [row(i) for i in range(1, 226)]
    rows += [row(225, effective='20261021'), row(280405, effective='20280101'), row(1)]
    def request(endpoint, **params):
        calls.append(params)
        assert endpoint == 'lawSearch.do' and params['search'] == 1
        assert params['target'] == 'eflaw' and params['nw'] == '1,3'
        page, display = params['page'], params['display']
        xml = ''.join(f'<law><법령일련번호>{r["id"]}</법령일련번호><법령명한글>{r["name"]}</법령명한글><시행일자>{r["effective"]}</시행일자><공포일자>{r["promulgated"]}</공포일자></law>' for r in rows[(page-1)*display:page*display])
        return f'<LawSearch><totalCnt>{len(rows)}</totalCnt>{xml}</LawSearch>'.encode()
    monkeypatch.setattr(api, 'request', request)
    result = api.search('선박안전법', revisions=True, display=10)
    assert result['total'] == 226 and len(result['laws']) == 10
    assert result['laws'][0]['effective'] == '20261021'
    assert [r['page'] for r in calls] == [1, 2, 3]
    pages = [api.search('선박안전법', page=p, revisions=True, display=10) for p in range(1, 24)]
    identities = {(r['id'], r['effective']) for page in pages for r in page['laws']}
    assert len(identities) == 226
    assert ('225','20200101') in identities and ('225','20261021') in identities
    assert not any(mst == '280405' for mst, effective in identities)
    assert len(calls) == 3  # cache, not repeated upstream traffic when paging


def test_body_request_uses_selected_effective_date(monkeypatch):
    api = LawAPI('test-oc')
    calls = []
    monkeypatch.setattr(api, 'request', lambda endpoint, **params: calls.append((endpoint, params)))
    api.fetch('280405', effective='20260101')
    assert calls == [('lawService.do', {'target':'eflaw','MST':'280405','efYd':'20260101'})]
    with pytest.raises(ValueError):
        api.fetch('280405', effective='bad-date')


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


def test_same_mst_dates_are_independent_in_catalog_collection_and_status(tmp_path):
    from app.management import CollectionInput
    from test_management import version
    class API:
        calls = []
        def search(self, *args, **kwargs):
            return {'laws':[row(123, effective=d) for d in ['20200101','20210101']], 'total':2}
        def fetch(self, mst, kind='law', effective=''):
            self.calls.append((mst,effective))
            return version(effective,'20190101')
    api = API()
    manager = Management(Store(tmp_path), FakeProvider(), api)
    assert manager.revisions('선박안전법','law',1,'api')['total'] == 2
    def request(effective):
        return CollectionInput(items=[{'mst':'123','name':'선박안전법','effective':effective}])
    first = manager.queue(request('20200101'))
    # Both dates may be queued simultaneously, but exact duplicates may not.
    second = manager.queue(request('20210101'))
    assert first['queued'] == second['queued'] == 1
    assert manager.queue(request('20210101'))['queued'] == 0
    manager.run(first['ids'])
    status = manager.revisions('선박안전법','law',1,'api')['items']
    assert {r['effective']:r['collection_status'] for r in status} == {'20200101':'completed','20210101':'queued'}
    assert next(r for r in status if r['effective']=='20210101')['version'] is None
    manager.run(second['ids'])
    assert api.calls == [('123','20200101'),('123','20210101')]
    assert manager.queue(request('20210101'))['queued'] == 0
    with manager.store.connect() as db:
        assert db.execute('SELECT count(*) FROM revision_catalog').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0] == 2


def test_legacy_revision_catalog_migration_preserves_rows(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'laws.sqlite3') as db:
        db.execute('CREATE TABLE revision_catalog(kind TEXT,mst TEXT,name TEXT,effective TEXT,promulgated TEXT,PRIMARY KEY(kind,mst))')
        db.execute("INSERT INTO revision_catalog VALUES('law','123','선박안전법','20200101','20190101')")
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("INSERT INTO revision_catalog VALUES('law','123','선박안전법','20210101','20190101')")
    with Store(tmp_path).connect() as db:
        assert [r[0] for r in db.execute('SELECT effective FROM revision_catalog ORDER BY effective')] == ['20200101','20210101']

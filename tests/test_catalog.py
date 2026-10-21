"""Specified catalog intake: isolated fake APIs, no real laws or external calls."""
import pytest
from fastapi.testclient import TestClient
from app import catalog
from app.config import Settings
from app.laws import LawAPI, SetupError
from app.main import create_app
from app.management import Management
from app.provider import Provider
from app.store import Store
from test_workflow import RAW, FakeProvider


class CatalogAPI:
    def __init__(self):
        self.items = list(catalog.CATALOG['items'])
        self.fetched = []

    def search(self, query, page=1, kind='law', revisions=False, display=100):
        index = next(i for i, item in enumerate(self.items) if item['name'] == query)
        rows = [dict(id=str(index+100), name=query, kind=kind, effective='20260101', promulgated='20251201'),
                dict(id=str(index+200), name=query, kind=kind, effective='20990101', promulgated='20980101'),
                dict(id=str(index+300), name=query+' 시행령', kind=kind, effective='20260102', promulgated='20251202')]
        # Missing API support for the second directive must remain an explicit failure.
        if index == 19:
            rows = []
        return {'laws': rows[(page-1)*display:page*display], 'total': len(rows)}

    def fetch(self, mst, kind='law'):
        self.fetched.append(mst)
        item = self.items[int(mst)-100]
        if kind == 'law':
            return RAW.replace('선박안전법'.encode(),item['name'].encode()).replace(b'TEST001',mst.encode())
        return f'<행정규칙><행정규칙명>{item["name"]}</행정규칙명><행정규칙ID>{mst}</행정규칙ID><시행일자>20260101</시행일자><조문단위><조문번호>1</조문번호><조문제목>목적</조문제목><조문내용>제1조(목적) 합성 테스트 자료입니다.</조문내용></조문단위></행정규칙>'.encode()


def test_catalog_seed_is_unique_idempotent_and_not_collection(tmp_path):
    store = Store(tmp_path)
    manager = Management(store, FakeProvider(), CatalogAPI())
    items = catalog.snapshot(manager)['items']
    assert len(items) == len({i['name'] for i in items}) == 26
    assert sum(i['kind'] == 'admrul' for i in items) == 2
    assert all(i['status'] == 'uncollected' and i['collected_versions'] == 0 for i in items)
    manager.add_category('추가법령', 'law')
    assert len(Management(store, FakeProvider(), CatalogAPI()).categories()) == 27
    assert manager.history(1)['total'] == 0
    assert '임대주택법' in {i['name'] for i in items}
    assert '공인중개사의 업무 및 부동산 거래신고에 관한 법률' in {i['name'] for i in items}


def test_batch_embeds_exact_names_skips_future_and_existing_and_retries_failure(tmp_path):
    api = CatalogAPI()
    store = Store(tmp_path)
    manager = Management(store, FakeProvider(), api)
    queued = catalog.queue_catalog(manager, False)
    assert queued['queued'] == 26
    assert catalog.queue_catalog(manager, False)['queued'] == 0
    manager.run(queued['ids'], resolve_catalog=True)
    items = catalog.snapshot(manager)['items']
    assert sum(i['status'] == 'completed' for i in items) == 25
    assert items[19]['status'] == 'failed' and '정확한 명칭' in items[19]['reason']
    assert all(int(mst) < 200 for mst in api.fetched)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0] == 25
        assert db.execute('SELECT count(*) FROM version_articles WHERE vector IS NULL OR title_vector IS NULL').fetchone()[0] == 0
    repeat = catalog.queue_catalog(manager, False)
    manager.run(repeat['ids'], resolve_catalog=True)
    assert len(api.fetched) == 25
    items = catalog.snapshot(manager)['items']
    assert sum(i['status'] == 'skipped' for i in items) == 25
    assert items[19]['status'] == 'failed'


def test_catalog_rejects_body_mismatch_without_storing(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'CATALOG', {'id':'small','items':[{'name':'민법','kind':'law'}]})
    api = CatalogAPI()
    api.fetch = lambda *args, **kwargs: RAW
    store = Store(tmp_path)
    manager = Management(store, FakeProvider(), api)
    queued = catalog.queue_catalog(manager, False)
    manager.run(queued['ids'], resolve_catalog=True)
    assert manager.history(1)['items'][0]['status'] == 'failed'
    assert '본문 응답' in manager.history(1)['items'][0]['reason']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0] == 0


def test_missing_keys_fail_before_queue_and_ui_cannot_skip_embedding(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), Provider(Settings(api_key='')), LawAPI(''))
    with TestClient(app) as client:
        url = '/api/management/catalog/real-estate/collect'
        response = client.post(url, json={})
        assert response.status_code == 503 and 'LAW_API_OC' in response.json()['detail']
        assert client.post(url, json={'keyword_only':True}).status_code == 422
        assert client.post('/api/management/collections', json={'items':[{'mst':'1','name':'민법'}], 'keyword_only':True}).status_code == 422
        assert client.get('/api/management/history').json()['total'] == 0
    manager = Management(Store(tmp_path), Provider(Settings(api_key='')), LawAPI('test-oc'))
    with pytest.raises(SetupError, match='LLM_API_KEY'):
        catalog.queue_catalog(manager, False)
    assert manager.history(1)['total'] == 0


def test_snapshot_matches_whitespace_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'CATALOG', {'id':'spacing','items':[{'name':'선박 안전법','kind':'law'}]})
    store = Store(tmp_path)
    manager = Management(store, FakeProvider(), CatalogAPI())
    store.ingest(RAW, FakeProvider(), mst='101', archive_only=True)
    assert catalog.snapshot(manager)['items'][0]['collected_versions'] == 1
    assert manager.revisions('선박 안전법', 'law', 1, 'collected')['total'] == 1
    restarted = Management(store, FakeProvider(), CatalogAPI())
    assert len(restarted.categories()) == 1

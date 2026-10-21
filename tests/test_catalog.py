"""Full-history preload without external calls or production data."""
import json
import pytest
from fastapi.testclient import TestClient
from app import catalog
from app import preload_real_estate as task
from app.config import Settings
from app.laws import LawAPI, SetupError
from app.main import create_app
from app.management import Management
from app.provider import Provider
from app.store import Store
from test_workflow import RAW, FakeProvider


class HistoryAPI:
    def __init__(self):
        self.fetched = []
        self.rows = [
            dict(id='101',name='과거법',kind='law',effective='20000101',promulgated='19990101'),
            dict(id='102',name='현행법',kind='law',effective='20260101',promulgated='20250101'),
            dict(id='103',name='현행법',kind='law',effective='20990101',promulgated='20260101'),
            dict(id='104',name='현행법 시행령',kind='law',effective='20260101',promulgated='20250101')]

    def search(self, query, page=1, kind='law', revisions=False, display=100):
        rows = [r for r in self.rows if query in r['name']]
        return {'laws':rows[(page-1)*display:page*display],'total':len(rows)}

    def fetch(self,mst,kind='law'):
        self.fetched.append(mst)
        row = next(r for r in self.rows if r['id']==mst)
        return RAW.replace('선박안전법'.encode(),row['name'].encode()).replace(b'20260101',row['effective'].encode())


@pytest.fixture
def small_catalog(monkeypatch):
    value = {'id':'small','items':[{'name':'현행법','kind':'law','aliases':['과거법']},{'name':'미수록지침','kind':'admrul'}]}
    monkeypatch.setattr(catalog, 'CATALOG', value)
    monkeypatch.setattr(task, 'CATALOG', value)
    return value


def test_current_names_and_migration_preserve_collected_history(tmp_path):
    store = Store(tmp_path)
    manager = Management(store,FakeProvider(),HistoryAPI())
    names = {i['name'] for i in catalog.CATALOG['items']}
    assert len(names)==27
    assert {'공인중개사법','민간임대주택에 관한 특별법','공공주택 특별법'} <= names
    assert '임대주택법' not in names
    assert len(manager.categories())==27
    assert manager.history(1)['total']==0
    # An obsolete, uncollected seeded name is removed on catalog migration.
    with store.connect() as db:
        db.execute('DELETE FROM catalog_registrations')
        db.execute("INSERT INTO categories(name,kind) VALUES('임대주택법','law')")
    assert len(Management(store,FakeProvider(),HistoryAPI()).categories())==27


def test_preload_collects_all_versions_and_aliases_and_retries_safely(tmp_path, small_catalog):
    api = HistoryAPI()
    store = Store(tmp_path)
    manager = Management(store,FakeProvider(),api)
    report = task.preload(manager,lambda _:None)
    assert not report['ok']  # absent directive must not be reported as success
    assert report['items'][0]['found']==3
    assert report['items'][0]['completed']==3
    assert set(api.fetched)=={'101','102','103'}  # includes past AND future
    assert report['items'][1]['errors']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM version_articles WHERE vector IS NULL OR title_vector IS NULL').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0]==3
        # Simulate a prior text-only collection: both embeddings must be repaired.
        db.execute("UPDATE version_articles SET title_vector=NULL WHERE version=(SELECT version FROM law_versions WHERE mst='102')")
    again = task.preload(manager,lambda _:None)
    assert again['items'][0]['already_indexed']==2 and again['items'][0]['completed']==1
    assert api.fetched.count('102')==2
    assert json.loads((tmp_path/'preload-real-estate-report.json').read_text())['finished_at']
    # After restart all three embeddings remain and are skipped without API body calls.
    third = task.preload(Management(store,FakeProvider(),api),lambda _:None)
    assert third['items'][0]['already_indexed']==3
    assert len(api.fetched)==4


def test_body_mismatch_fails_without_saving(tmp_path, small_catalog):
    api = HistoryAPI()
    api.fetch = lambda *a, **kw: RAW
    manager = Management(Store(tmp_path),FakeProvider(),api)
    report = task.preload(manager,lambda _:None)
    assert report['items'][0]['failed']==3
    assert all('본문 응답' in error['reason'] for error in report['items'][0]['errors'])
    with manager.store.connect() as db:
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0]==0


def test_missing_keys_no_queue_and_removed_catalog_routes(tmp_path):
    app = create_app(Settings(data_dir=tmp_path),Provider(Settings(api_key='')),LawAPI(''))
    with TestClient(app) as client:
        assert client.get('/api/management/catalog/real-estate').status_code==404
        assert client.post('/api/management/catalog/real-estate/collect',json={}).status_code==404
        response = client.post('/api/management/collections',json={'items':[{'mst':'1','name':'민법'}]})
        assert response.status_code==503 and 'LAW_API_OC' in response.json()['detail']
        assert client.post('/api/management/collections',json={'items':[{'mst':'1','name':'민법'}],'keyword_only':True}).status_code==422
        assert client.get('/api/management/history').json()['total']==0
    manager = Management(Store(tmp_path),Provider(Settings(api_key='')),LawAPI('test-oc'))
    with pytest.raises(SetupError,match='LLM_API_KEY'):
        task.preload(manager,lambda _:None)
    assert manager.history(1)['total']==0


def test_version_pagination_and_duplicate_ids(tmp_path, small_catalog):
    api = HistoryAPI()
    api.rows = [dict(id=str(i),name='현행법',kind='law',effective='20200101',promulgated='20190101') for i in range(1,122)]
    api.rows.append(dict(api.rows[0]))
    manager = Management(Store(tmp_path),FakeProvider(),api)
    assert len(task.discover_versions(manager,small_catalog['items'][0]))==121


def test_preload_main_refuses_to_claim_success_without_keys(monkeypatch,capsys):
    monkeypatch.setattr(task,'Settings',lambda:Settings(law_oc='',api_key=''))
    assert task.main()==2
    assert '수집을 시작하지 않았습니다' in capsys.readouterr().out

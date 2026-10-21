from datetime import date
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.laws import SetupError, parse_law
from app.main import create_app
from app.management import CollectionInput, Management
from app.store import Store
from app.temporal import resolve_date
from test_workflow import RAW, FakeProvider


def version(effective, promulgated, word='테스트 대상'):
    return RAW.replace(b'20260101',effective.encode()).replace(
        '</시행일자>'.encode(),
        ('</시행일자><공포일자>'+promulgated+'</공포일자>').encode()).replace('테스트 대상'.encode(),word.encode())


class FakeAPI:
    calls = []
    def search(self, query, page=1, kind='law', revisions=False, display=10):
        self.calls.append(('search',query,page,kind))
        items=[{'id':'101','name':'선박안전법','kind':'law','effective':'20260101','promulgated':'20251201','status':'현행'},
               {'id':'102','name':'선박안전법','kind':'law','effective':'20200101','promulgated':'20191201','status':'연혁'},
               {'id':'103','name':'선박안전법','kind':'law','effective':'20990101','promulgated':'20981201','status':'시행예정'},
               {'id':'999','name':'실패법','kind':'law','effective':'20200101','promulgated':'20190101','status':'연혁'}]
        return {'laws':items,'total':len(items),'page':page}
    def fetch(self,mst,kind='law'):
        self.calls.append(('fetch',mst))
        if mst=='999':
            raise SetupError('법령 API 요청 실패')
        dates={'101':('20260101','20251201'),'102':('20200101','20191201'),'103':('20990101','20981201')}
        return version(*dates[mst],word=mst)


@pytest.fixture
def client(tmp_path):
    app=create_app(Settings(data_dir=tmp_path),FakeProvider(),FakeAPI())
    with TestClient(app) as client:
        yield client


def item(mst='101',name='선박안전법'):
    return {'mst':mst,'name':name,'kind':'law'}


def test_specified_categories_registration_and_no_collection(client):
    assert len(client.get('/api/management/categories').json()['items'])==27
    r=client.post('/api/management/categories',json={'name':'선박안전법','kind':'law'})
    assert len(r.json()['items'])==28
    r=client.post('/api/management/categories',json={'name':'선박안전법','kind':'law'})
    assert len(r.json()['items'])==28
    assert client.get('/api/management/history').json()['total']==0
    assert client.get('/api/status').json()['laws']==[]
    assert client.post('/api/management/categories',json={'name':'  ','kind':'law'}).status_code==400


def test_collection_versions_history_failure_and_retry(client):
    r=client.post('/api/management/collections',json={'items':[item(),item('102'),item('999','실패법')]})
    assert r.status_code==202 and r.json()['queued']==3
    h=client.get('/api/management/history').json()
    assert h['total']==3 and h['pending']==0 and h['last_completed']
    assert '선박안전법' in {r['name'] for r in client.get('/api/management/categories').json()['items']}
    assert '실패법' not in {r['name'] for r in client.get('/api/management/categories').json()['items']}
    assert {r['status'] for r in h['items']}=={'completed','failed'}
    failure=next(r for r in h['items'] if r['status']=='failed')
    assert failure['reason']=='법령 API 요청 실패'
    # Server enforces missing-only collection even if a stale UI submits it again.
    r=client.post('/api/management/collections',json={'items':[item(),item('999','실패법')]})
    assert r.json()['queued']==1
    revisions=client.get('/api/management/revisions?query=선박안전법').json()
    assert revisions['total']==2
    selected=revisions['items'][0]
    result=client.get(f'/api/management/versions/{selected["version"]}/articles').json()
    assert result['total']==3 and result['items'][1]['number']=='제3조'
    assert client.get(result['items'][1]['html_url'].split('#')[0]).status_code==200
    assert client.get('/api/management/history?page=2').json()['items']==[]


def test_remote_browse_merges_collection_status(client):
    client.post('/api/management/collections',json={'items':[item()]})
    r=client.get('/api/management/revisions?source=api&query=선박안전법').json()
    assert next(x for x in r['items'] if x['id']=='101')['collection_status']=='completed'
    assert next(x for x in r['items'] if x['id']=='102')['collection_status']=='uncollected'
    assert client.get('/api/management/revisions?source=api').status_code==400


def test_restart_marks_pending_and_preserves_existing_data(tmp_path):
    store=Store(tmp_path);manager=Management(store,FakeProvider(),FakeAPI())
    manager.queue(CollectionInput(items=[item()]))
    restarted=Management(Store(tmp_path),FakeProvider(),FakeAPI())
    h=restarted.history(1)
    assert h['items'][0]['status']=='interrupted' and h['pending']==0


def test_duplicate_queue_requests_do_not_duplicate(tmp_path):
    manager=Management(Store(tmp_path),FakeProvider(),FakeAPI())
    req=CollectionInput(items=[item(),item()])
    assert manager.queue(req)['queued']==1
    assert manager.queue(req)['queued']==0


def test_chapter_hierarchy_preserved():
    raw=RAW.replace('<조문>'.encode(),'<조문><조문단위><조문여부>전문</조문여부><조문내용>제1장 총칙</조문내용></조문단위>'.encode())
    assert parse_law(raw).articles[1].chapter=='제1장 총칙'


def test_current_historical_future_boundary_and_no_fallback(tmp_path):
    store=Store(tmp_path);provider=FakeProvider()
    for effective,promulgated,mst in [('20260101','20251201','101'),('20200101','20191201','102'),('20990101','20981201','103')]:
        store.ingest(version(effective,promulgated,mst),provider,mst=mst,archive_only=True)
    q='선박안전법에 적용되지 않는 것은?'
    assert store.search(q,provider,as_of=date(2026,1,1))[0]['effective']=='20260101'
    assert store.search(q,provider,as_of=date(2025,12,31))[0]['effective']=='20200101'
    assert store.search(q,provider,as_of=date(2019,12,31))==[]
    assert store.search(q,provider,as_of=date(2028,1,1))[0]['effective']=='20260101'
    assert store.search(q,provider,as_of=date(2099,1,1))[0]['effective']=='20990101'
    assert len(store.list_laws())==1
    # Restart must neither duplicate archived articles nor discard prior revisions.
    restarted=Store(tmp_path)
    with restarted.connect() as db:
        assert db.execute('SELECT count(*) FROM version_articles').fetchone()[0]==9


def test_missing_governing_revision_abstains(client):
    client.get('/api/management/revisions?source=api&query=선박안전법')
    client.post('/api/management/collections',json={'items':[item('102')]})
    result=client.post('/api/chat',json={'question':'선박안전법에 적용되지 않는 것은?','mode':'keyword','as_of':'2026-10-21'}).json()
    assert not result['generated'] and not result['evidence']
    assert '미수집' in result['answer'] and result['missing_versions'][0]['mst']=='101'
    result=client.post('/api/search',json={'question':'2020년 1월 1일 기준 선박안전법 적용범위','mode':'keyword','threshold':0}).json()
    assert result['as_of']=='2020-01-01' and result['evidence'][0]['effective']=='20200101'


@pytest.mark.parametrize('question', ['2020년 선박안전법 적용범위','2020-01-01부터 2021-01-01까지 선박안전법','작년 당시 선박안전법','2020년 2월 31일 기준'])
def test_ambiguous_dates_require_clarification(client,question):
    r=client.post('/api/chat',json={'question':question}).json()
    assert not r['generated'] and r['clarification'] and not r['evidence']


def test_explicit_date_override():
    value,clarification=resolve_date('2020년 선박안전법',date(2020,6,1))
    assert value==date(2020,6,1) and not clarification


def test_history_and_article_pagination(tmp_path):
    store=Store(tmp_path);manager=Management(store,FakeProvider(),FakeAPI())
    manager.queue(CollectionInput(items=[item(str(i)) for i in range(1,14)]))
    assert len(manager.history(1)['items'])==10
    assert len(manager.history(2)['items'])==3
    result=store.ingest(version('20200101','20191201'),FakeProvider(),mst='102')
    assert manager.articles(result['version'],2)['items']==[]


def test_migrate_legacy_store_without_losing_index(tmp_path):
    store=Store(tmp_path);store.ingest(version('20200101','20191201'),FakeProvider())
    with store.connect() as db:
        db.execute('DELETE FROM version_articles');db.execute('DELETE FROM law_versions')
    store=Store(tmp_path)
    assert store.search('선박안전법 적용범위',FakeProvider(),as_of=date(2021,1,1),mode='keyword')
    with pytest.raises(SetupError,match='재색인'):
        store.search('선박안전법 적용범위',FakeProvider(),as_of=date(2021,1,1))
    Store(tmp_path)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM version_articles').fetchone()[0]==3


def test_duration_question_does_not_trigger_historical_clarification():
    value,clarification=resolve_date('선박 검사 유효기간은 얼마인가요?')
    assert value is not None and clarification is None


def test_archived_html_viewer_upgrades_without_api_or_embeddings(tmp_path):
    store=Store(tmp_path)
    original=version('20200101','20191201')
    saved=store.ingest(original,FakeProvider(),mst='102',archive_only=True)
    html_path=tmp_path/'html'/(saved['version']+'.html')
    html_path.write_text('<html>Legacy viewer</html>',encoding='utf-8')
    class NoNetwork:
        def __getattr__(self,name):
            raise AssertionError('No network/model call allowed for archived document viewing')
    app=create_app(Settings(data_dir=tmp_path),NoNetwork(),NoNetwork())
    with TestClient(app) as client:
        response=client.get('/documents/'+html_path.name)
        assert response.status_code==200
        assert 'data-viewer-version="2"' in response.text
        assert '[시행 2020.01.01]' in response.text
        assert 'id="선박안전법-제3조"' in response.text
        assert 'aria-label="조문 목차"' in response.text
        assert response.text==html_path.read_text(encoding='utf-8')
        assert (tmp_path/'raw'/(saved['version']+'.xml')).read_bytes()==original

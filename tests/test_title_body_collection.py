"""Collection needs only embeddings; old generated indexes cannot affect retrieval."""
import json
import sqlite3
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.laws import parse_law
from app.main import create_app
from app.provider import Provider
from app.rebuild_html import rebuild_all
from app.store import Store, body_text
from test_enhanced_retrieval import xml
from test_workflow import FakeProvider

REAL_CLIENT = httpx.Client
RAW = xml('합성서류법', [('1', '서류 제출', '신청서 및 신분증 사본을 제출한다.'),
                       ('2', '보호 요건', '보증금 반환 보호 요건을 정한다.')])
OBSOLETE_TABLES = {'search_metadata', 'search_metadata_vectors', 'article_metadata'}


class SourceAPI:
    def fetch(self, mst, **kwargs):
        return RAW


def test_management_collection_and_html_only_request_title_body_embeddings(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        assert request.url.path == '/v1/embeddings', 'Collection must not request chat completions'
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [1., .1, 0.]} for i in range(len(payload['input']))]})

    monkeypatch.setattr(httpx, 'Client', lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle), **kw))
    settings = Settings(data_dir=tmp_path, api_key='test', embedding_model='text-embedding-3-large')
    app = create_app(settings, Provider(settings), SourceAPI())
    request = {'items': [{'mst': '123', 'name': '합성서류법', 'kind': 'law'}]}
    with TestClient(app) as client:
        response = client.post('/api/management/collections', json=request)
        assert response.status_code == 202 and response.json()['queued'] == 1
        history = client.get('/api/management/history').json()['items']
        assert len(history) == 1 and history[0]['status'] == 'completed'
        law = parse_law(RAW)
        expected = [body_text({'body': a.body}) for a in law.articles]
        expected += [f'{law.name} - {a.number}({a.title})' for a in law.articles]
        assert len(calls) == 1
        assert calls[0]['input'] == expected and calls[0]['model'] == 'text-embedding-3-large'

        status = client.get('/api/status').json()
        assert 'metadata_model' not in status
        assert status['laws'][0]['embedded_count'] == 2
        assert 'metadata_count' not in status['laws'][0]
        articles = client.get(f'/api/management/versions/{law.version}/articles').json()['items']
        assert [a['body'] for a in articles] == [a.body for a in law.articles]
        assert all('search_metadata' not in a for a in articles)

        # The UI and CLI share the same queue: an already completed version costs no new API calls.
        assert client.post('/api/management/collections', json=request).json()['queued'] == 0
        assert rebuild_all(tmp_path, notify=lambda _: None)['completed'] == 1
        assert len(calls) == 1
    with app.state.store.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables.isdisjoint(OBSOLETE_TABLES)


def test_keyword_only_collection_and_html_work_without_any_ai_key(tmp_path):
    store = Store(tmp_path)
    result = store.ingest(RAW, Provider(Settings(api_key='')), keyword_only=True)
    assert result['mode'] == 'keyword' and result['articles'] == 2
    assert 'metadata_articles' not in result
    assert rebuild_all(tmp_path, notify=lambda _: None)['completed'] == 1
    with store.connect() as db:
        assert db.execute('SELECT count(vector)+count(title_vector) FROM version_articles').fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['keyword', 'hybrid'])
def test_existing_generated_indexes_are_ignored_without_reembedding(tmp_path, mode, monkeypatch):
    store, provider = Store(tmp_path), FakeProvider()
    result = store.ingest(RAW, provider)
    options = dict(mode=mode, compare=True, variants=['신청서 사본'])
    before = store.search_enhanced('서류 제출', provider, **options)
    with store.connect() as db:
        vectors_before = list(map(tuple, db.execute('SELECT vector,title_vector,model FROM version_articles ORDER BY position')))
        # Reproduce tables left by a previous release, including its real foreign keys.
        db.executescript('''
            CREATE TABLE search_metadata (key TEXT PRIMARY KEY, source_hash TEXT, generator_model TEXT,
                prompt_version TEXT, summary TEXT, questions TEXT, created_at TEXT);
            CREATE TABLE search_metadata_vectors (metadata_key TEXT REFERENCES search_metadata(key),
                field TEXT, ordinal INTEGER, text TEXT, vector BLOB, model TEXT,
                PRIMARY KEY(metadata_key,field,ordinal,model));
            CREATE TABLE article_metadata (version TEXT, position INTEGER,
                metadata_key TEXT REFERENCES search_metadata(key), status TEXT, error TEXT, updated_at TEXT,
                PRIMARY KEY(version,position), FOREIGN KEY(version,position)
                REFERENCES version_articles(version,position) ON DELETE CASCADE);
        ''')
        # Unrelated article 2 has an obsolete, misleading match in the generated fields.
        db.execute('INSERT INTO search_metadata VALUES(?,?,?,?,?,?,?)',
                   ('old', 'old-hash', 'old-model', 'old-prompt', '서류 제출', '["신청서 사본"]', ''))
        db.execute('INSERT INTO article_metadata VALUES(?,?,?,?,?,?)',
                   (result['version'], 1, 'old', 'completed', '', ''))
        db.execute('INSERT INTO search_metadata_vectors VALUES(?,?,?,?,?,?)',
                   ('old', 'question', 0, '신청서 사본', vectors_before[0][0], provider.fingerprint))

    # A restart neither reads these indexes nor needs an API call to migrate vectors.
    reopened = Store(tmp_path)
    connect = reopened.connect

    @contextmanager
    def source_only_connection():
        with connect() as db:
            db.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_READ and table in OBSOLETE_TABLES else sqlite3.SQLITE_OK)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(reopened, 'connect', source_only_connection)
        after = reopened.search_enhanced('서류 제출', provider, **options)
    assert before == after and after['rrf_fields'] == ['title', 'body']
    assert after['rrf_max_score'] == pytest.approx(2/61)
    assert after['rrf']
    for hit in after['rrf']:
        assert not ({'metadata_score', 'summary_score', 'questions_score', 'matched_metadata'} & hit.keys())
    with reopened.connect() as db:
        assert vectors_before == list(map(tuple, db.execute('SELECT vector,title_vector,model FROM version_articles ORDER BY position')))
        assert db.execute('SELECT summary FROM search_metadata').fetchone()[0] == '서류 제출'
    # Recollecting a version must still work with the legacy FK/cascade schema present.
    reopened.ingest(RAW, provider)
    assert reopened.list_laws()[0]['article_count'] == 2

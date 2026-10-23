"""NSE 3차원 임베딩 응답을 수집/검색의 문장 벡터로 변환하는 경계 검증."""
from dataclasses import replace
import json

import pytest

from app.config import Settings
from app.laws import SetupError
from app.nse_mq import NseMqSettings
from app.provider import Provider
from test_nse_mq import Broker, config, response


def embedding_reply(vectors):
    return json.dumps({'task':'embedding', 'result_status':{'code':'SUCCESS'},
                       'content':{'embedding_vectors':vectors}}).encode()


def enable(monkeypatch, handler=None):
    # LLM 모델 ID가 없어도 임베딩 모델 이름만으로 호출 가능해야 합니다.
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config(model_id=None))
    if handler is None:
        handler = lambda payload: [embedding_reply([[[i+1., .5, .25]] for i in range(len(payload['content']['texts']))])]
    return Broker(monkeypatch, replies=handler)


@pytest.mark.parametrize('backend,model', [('local_embedding','text-embedding-3-large'),
    ('LOCA_EMBEDDING','text-embedding-3-large'), ('LOCAL_EMBEDDING','text-embedding-3-large'),
    ('openai','LOCA_EMBEDDING'), ('openai','LOCAL_EMBEDDING')])
def test_nse_embedding_route_shape_and_aliases(monkeypatch, backend, model):
    broker = enable(monkeypatch)
    provider = Provider(Settings(api_key='private-openai-key', embedding_backend=backend, embedding_model=model))
    assert provider.embed(['첫 문장', '둘째 문장']) == [[1., .5, .25], [2., .5, .25]]
    request = broker.published[0]
    payload = json.loads(request['body'])
    assert payload['task'] == 'embedding'
    assert payload['content'] == {'texts':['첫 문장','둘째 문장'], 'served_model_name':'emb-bge-m3-ko'}
    assert request['routing_key'] == 'nse_request'
    assert request['properties'].reply_to.startswith('nse_result_kih_')
    assert 'private-openai-key' not in request['body'].decode()
    assert provider.settings.ai_configured('embedding')
    assert not provider.local_model_available()


@pytest.mark.parametrize('chat,embedding', [('openai','openai'),('openai','local_embedding'),
                                          ('local_model','openai'),('local_model','local_embedding')])
def test_gpt_nse_chat_and_embedding_four_combinations(monkeypatch, chat, embedding):
    from test_ai_backends import mock_http, answer, vectors
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda:config())
    def mq_reply(payload):
        if payload['task'] == 'embedding':
            return [embedding_reply([[[1., .5]] for _ in payload['content']['texts']])]
        return [response('{"query":"선박 검사"}')]
    broker = Broker(monkeypatch, replies=mq_reply)
    http_calls = []
    def http(request):
        http_calls.append(request)
        return vectors(request) if request.url.path.endswith('/embeddings') else answer('{"query":"선박 검사"}')
    mock_http(monkeypatch, http)
    provider = Provider(Settings(api_key='openai-test-key', chat_backend=chat, embedding_backend=embedding))
    assert provider.rewrite_question('선박 검사는?') == '선박 검사'
    assert provider.embed(['문장']) == [[1., .5]]
    assert len(http_calls) == (chat == 'openai') + (embedding == 'openai')
    assert len(broker.published) == (chat == 'local_model') + (embedding == 'local_embedding')


@pytest.mark.parametrize('vectors', [[], [[[1.,0.]]], [[1.,0.],[2.,0.]],
    [[[1.]], [[2.],[3.]]], [[[1.]], [[1.,2.]]], [[[float('nan')]], [[1.]]],
    [[[True]], [[1.]]], [[[0.,0.]], [[1.,2.]]], [[[]],[[1.]]], [[['bad']],[[1.]]]])
def test_invalid_or_partial_vectors_fail_before_use(monkeypatch, vectors):
    broker = enable(monkeypatch, lambda _: [embedding_reply(vectors)])
    provider = Provider(Settings(embedding_backend='local_embedding'))
    with pytest.raises(SetupError, match='임베딩|embedding_vectors'):
        provider.embed(['첫째','둘째'])
    assert broker.closed and len(broker.published) == 1


def test_embedding_failure_status_never_exposes_partial_vectors(monkeypatch):
    enable(monkeypatch, lambda _: [json.dumps({'task':'embedding','result_status':{'code':'FAIL'},
           'content':{'embedding_vectors':[[[1.,2.]]]}}).encode()])
    with pytest.raises(SetupError, match='실패 상태'):
        Provider(Settings(embedding_backend='local_embedding')).embed(['문장'])


def test_batch_and_long_input_keep_text_order(monkeypatch):
    broker = enable(monkeypatch)
    text = '긴 조문의 제목과 본문은 분할해도 순서를 유지합니다. ' * 5
    provider = Provider(Settings(embedding_backend='local_embedding',embedding_max_input_bytes=40,embedding_batch_size=2))
    result = provider.embed([text, '마지막'])
    sent = [json.loads(r['body'])['content']['texts'] for r in broker.published]
    assert all(len(batch) <= 2 for batch in sent)
    assert ''.join(t for batch in sent for t in batch) == text+'마지막'
    assert len(result) == 2 and all(len(v) == 3 for v in result)


def test_vector_identity_is_separate_from_chat_credentials_and_reply_queue(monkeypatch):
    enable(monkeypatch)
    settings = Settings(embedding_backend='local_embedding')
    provider = Provider(settings)
    fingerprint = provider.fingerprint
    assert fingerprint.startswith('nse-mq:') and fingerprint.endswith('|emb-bge-m3-ko')
    assert 'private' not in fingerprint and 'mq.example.test' not in fingerprint
    assert fingerprint == provider.for_model('LOCAL_MODEL').fingerprint
    assert fingerprint == provider.for_model('gpt-4.1').fingerprint
    assert fingerprint != Provider(replace(settings, nse_embedding_revision='new-weights')).fingerprint
    assert fingerprint != Provider(replace(settings, nse_embedding_model='emb-new-model')).fingerprint
    assert fingerprint != Provider(replace(settings, embedding_backend='openai')).fingerprint
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda:config(password='rotated-key',reply_prefix='another_result'))
    assert provider.fingerprint == fingerprint
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda:config(vhost='/another'))
    assert provider.fingerprint != fingerprint


def test_embedding_cli_is_independent_of_active_app_backend(monkeypatch, tmp_path, capsys):
    from app import check_nse_mq
    broker = enable(monkeypatch)
    path = tmp_path/'report.json'
    assert check_nse_mq.main(['--embedding', '--report', str(path)]) == 0
    report = json.loads(path.read_text())
    assert report['success'] and report['mode'] == 'embedding'
    assert report['input_count'] == report['vector_count'] == 2 and report['dimensions'] == 3
    assert 'embedding_vectors' not in path.read_text()
    assert 'private' not in capsys.readouterr().out and len(broker.published) == 1


def test_reindex_to_nse_preserves_files_and_can_resume(monkeypatch, tmp_path):
    from app import reindex_embeddings
    from app.store import Store
    from test_workflow import RAW, FakeProvider
    store = Store(tmp_path)
    store.ingest(RAW, FakeProvider())
    files = {p:p.read_bytes() for name in ('raw','html') for p in (tmp_path/name).iterdir()}
    broker = enable(monkeypatch)
    settings = Settings(data_dir=tmp_path, embedding_backend='local_embedding')
    provider = Provider(settings)
    with pytest.raises(SetupError, match='모델'):
        store.search_enhanced('선박 적용범위', provider)
    monkeypatch.setattr(reindex_embeddings, 'Settings', lambda:settings)
    assert reindex_embeddings.main([]) == 0
    assert all(p.read_bytes() == data for p, data in files.items())
    with store.connect() as db:
        assert {r[0] for r in db.execute('SELECT model FROM version_articles WHERE vector IS NOT NULL')} == {provider.fingerprint}
    assert store.search_enhanced('선박 적용범위', provider)
    broker.published.clear()
    assert reindex_embeddings.main([]) == 0 and not broker.published


def test_reindex_failure_preserves_existing_vectors(monkeypatch, tmp_path):
    from app import reindex_embeddings
    from app.store import Store
    from test_workflow import RAW, FakeProvider
    store = Store(tmp_path)
    store.ingest(RAW, FakeProvider())
    with store.connect() as db:
        before = [tuple(r) for r in db.execute('SELECT model, vector, title_vector FROM version_articles ORDER BY position')]
    enable(monkeypatch, lambda _: [embedding_reply([])])
    monkeypatch.setattr(reindex_embeddings, 'Settings', lambda:Settings(data_dir=tmp_path, embedding_backend='local_embedding'))
    assert reindex_embeddings.main([]) == 1
    with store.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT model, vector, title_vector FROM version_articles ORDER BY position')] == before

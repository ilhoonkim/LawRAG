"""Exercise real HTTP serialization for both backends without contacting any server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.laws import SetupError
from app.main import ChatRequest, create_app
from app.management import Management
from app.provider import Provider
from app.store import Store
from test_management import FakeAPI
from test_workflow import RAW

REAL_CLIENT = httpx.Client


def settings(**overrides):
    values = dict(api_key='openai-private', base_url='https://openai.test/v1',
                  chat_model='gpt-4.1', embedding_model='text-embedding-3-large',
                  company_chat_base_url='http://chat.internal:9000/v1',
                  company_chat_model='company/sllm-v1', company_chat_api_key='',
                  company_embedding_base_url='http://embed.internal:9001/v1',
                  company_embedding_model='company/embed-v1', company_embedding_api_key='')
    return Settings(**(values | overrides))


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(httpx, 'Client', lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def answer(content):
    return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':content}}]})


def vectors(request):
    inputs = json.loads(request.content)['input']
    return httpx.Response(200, json={'data':[{'index':i,'embedding':[1.,.5]} for i in range(len(inputs))][::-1]})


@pytest.mark.parametrize('chat,embedding', [('openai','openai'),('company','openai'),('openai','company'),('company','company')])
def test_independent_routing_models_and_credentials(monkeypatch, chat, embedding):
    config = settings(chat_backend=chat, embedding_backend=embedding,
                      company_chat_api_key='chat-private', company_embedding_api_key='embed-private')
    calls = []
    def handle(request):
        calls.append(request)
        return vectors(request) if request.url.path.endswith('/embeddings') else answer('{"query":"선박 검사"}')
    mock_http(monkeypatch, handle)
    provider = Provider(config)
    assert provider.rewrite_question('선박 검사') == '선박 검사'
    assert provider.embed(['첫 본문','둘째 제목']) == [[1.,.5],[1.,.5]]
    for request, purpose, backend in zip(calls, ['chat','embedding'], [chat,embedding]):
        target = config.endpoint(purpose)
        assert request.url == target.url('/chat/completions' if purpose=='chat' else '/embeddings')
        assert json.loads(request.content)['model'] == target.model
        assert request.headers['authorization'] == 'Bearer ' + target.api_key
    assert provider.fingerprint == config.endpoint('embedding').base_url + '|' + config.endpoint('embedding').model


def test_unauthenticated_company_server_never_receives_openai_key(monkeypatch):
    config = settings(chat_backend='company', embedding_backend='company')
    seen=[]
    def handle(request):
        seen.append(request)
        assert 'authorization' not in request.headers
        assert 'openai-private' not in request.content.decode()
        return vectors(request) if request.url.path.endswith('/embeddings') else answer('{"query":"선박 검사"}')
    mock_http(monkeypatch, handle)
    provider=Provider(config)
    assert provider.embed(['본문'])
    assert provider.rewrite_question('선박 검사')
    assert config.ai_configured('embedding') and config.ai_configured('chat')
    assert len(seen)==2


def test_company_collection_preflight_does_not_require_openai_key(tmp_path):
    config=settings(api_key='', chat_backend='openai', embedding_backend='company', data_dir=tmp_path)
    manager=Management(Store(tmp_path),Provider(config),FakeAPI())
    manager.require_collection_keys()
    assert config.ai_configured('embedding') and not config.ai_configured('chat')


def test_invalid_company_config_fails_locally_without_fallback(monkeypatch):
    mock_http(monkeypatch, lambda _:pytest.fail('Invalid company config reached a server'))
    for config in [settings(embedding_backend='company',company_embedding_base_url=''),
                   settings(embedding_backend='company',company_embedding_model=''),
                   settings(embedding_backend='typo'),
                   settings(embedding_backend='company',company_embedding_base_url='http://secret:private@host/v1')]:
        assert not config.ai_configured('embedding')
        with pytest.raises(SetupError) as caught:
            Provider(config).embed(['본문'])
        assert 'secret' not in str(caught.value) and 'private' not in str(caught.value)


@pytest.mark.parametrize('mode', ['auto','json_object','prompt'])
def test_company_json_modes_keep_validation(monkeypatch,mode):
    provider=Provider(settings(chat_backend='company',company_chat_json_mode=mode))
    calls=[]
    def handle(request):
        payload=json.loads(request.content)
        calls.append(payload)
        assert request.url.host=='chat.internal'
        if mode=='prompt':
            assert 'response_format' not in payload
        else:
            assert payload['response_format']=={'type':'json_object'}
        return answer('{"decisions":[]}')  # Must NOT silently approve the missing decision.
    mock_http(monkeypatch,handle)
    with pytest.raises(SetupError,match='판정 수'):
        provider.filter_evidence('선박 검사', [{'law_name':'법','number':'제1조','title':'검사','body':'검사를 받는다.'}])
    assert len(calls)==2


def test_company_model_selection_does_not_change_embedding_identity():
    original=Provider(settings(chat_backend='company',embedding_backend='company'))
    selected=original.for_model('other/model:latest')
    assert selected.chat_model=='other/model:latest'
    assert selected.settings.rerank_model=='other/model:latest'
    assert original.chat_model=='company/sllm-v1'
    assert selected.fingerprint==original.fingerprint
    assert Provider(settings()).fingerprint=='https://openai.test/v1|text-embedding-3-large'
    assert Provider(settings(embedding_max_input_bytes=2000)).fingerprint != Provider(settings()).fingerprint


def test_company_model_list_and_api_model_ids(tmp_path,monkeypatch):
    config=settings(chat_backend='company',embedding_backend='company',data_dir=tmp_path)
    def handle(request):
        assert request.url == 'http://chat.internal:9000/v1/models'
        assert 'authorization' not in request.headers
        return httpx.Response(200,json={'data':[{'id':'company/sllm-v1'},{'id':'org/another:latest'},{'id':'bad model'}]})
    mock_http(monkeypatch,handle)
    with TestClient(create_app(config)) as client:
        payload=client.get('/api/models').json()
        assert payload['models']==['company/sllm-v1','org/another:latest']
        assert payload['default']=='company/sllm-v1' and payload['backend']=='company'
        status=client.get('/api/status').json()
        assert status['llm_configured'] and status['embedding_configured']
        assert 'private' not in json.dumps(status) and 'internal' not in json.dumps(status)
    assert ChatRequest(question='선박 검사',model='org/another:latest').model=='org/another:latest'


def test_reindex_uses_selected_embedding_only_and_preserves_original_files(tmp_path,monkeypatch):
    from app import reindex_embeddings
    mock_http(monkeypatch,vectors)
    store=Store(tmp_path)
    store.ingest(RAW,Provider(settings()))
    before={p:p.read_bytes() for folder in ('raw','html') for p in (tmp_path/folder).iterdir()}
    company=settings(embedding_backend='company',data_dir=tmp_path)
    provider=Provider(company)
    with pytest.raises(SetupError,match='모델'):
        store.search_enhanced('선박 검사',provider)
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.url=='http://embed.internal:9001/v1/embeddings'
        return vectors(request)
    mock_http(monkeypatch,handle)
    monkeypatch.setattr(reindex_embeddings,'Settings',lambda:company)
    assert reindex_embeddings.main([])==0
    assert calls and all(p.read_bytes()==content for p,content in before.items())
    report=json.loads((tmp_path/'embedding-reindex-report.json').read_text())
    assert report['embedding_model']=='company/embed-v1' and report['completed']==1
    with store.connect() as db:
        assert {r[0] for r in db.execute('SELECT model FROM version_articles WHERE vector IS NOT NULL')}=={provider.fingerprint}
    calls.clear()
    assert reindex_embeddings.main([])==0 and not calls


def test_small_server_embedding_limits_keep_input_and_index_order(monkeypatch):
    config=settings(embedding_backend='company',embedding_max_input_bytes=30,embedding_batch_size=2)
    sent=[]
    def handle(request):
        inputs=json.loads(request.content)['input']
        assert len(inputs)<=2 and all(len(t.encode())<=30 for t in inputs)
        sent.extend(inputs)
        return vectors(request)
    mock_http(monkeypatch,handle)
    text='소형어선의 설치 의무 및 예외 조건 ' * 10
    assert len(Provider(config).embed([text]))==1
    assert ''.join(sent)==text


@pytest.mark.parametrize('payload', [ {'data':[{'index':0,'embedding':['bad']}]},
    {'data':[{'index':True,'embedding':[1.]}]}, {'data':[]}, {'unexpected':'response'},
    {'data':[{'index':0,'embedding':[False]}]} ])
def test_invalid_company_embedding_response_is_not_stored(monkeypatch,payload):
    mock_http(monkeypatch,lambda _:httpx.Response(200,json=payload))
    with pytest.raises(ValueError,match='임베딩'):
        Provider(settings(embedding_backend='company')).embed(['본문'])


def test_local_env_precedence_and_old_env_compatibility(tmp_path):
    # Import a copy in a fresh process so loading dotenv never mutates the test runner.
    app=tmp_path/'app';app.mkdir()
    shutil.copy(Path(__file__).parents[1]/'app/config.py',app/'config.py')
    (tmp_path/'.env').write_text('CHAT_BACKEND=openai\nCHAT_MODEL=base-model\nLLM_API_KEY=base-key\n')
    env={k:v for k,v in os.environ.items() if k not in ('CHAT_BACKEND','CHAT_MODEL','LLM_API_KEY','OPENAI_API_KEY')}
    script="import runpy,json; m=runpy.run_path('app/config.py'); s=m['Settings'](); print(json.dumps([s.chat_backend,s.chat_model,s.api_key]))"
    def read(extra=None):
        result=subprocess.run([sys.executable,'-c',script],cwd=tmp_path,env=env|(extra or {}),capture_output=True,text=True,check=True)
        return json.loads(result.stdout)
    assert read()==['openai','base-model','base-key']
    (tmp_path/'local.env').write_text('CHAT_BACKEND=company\nCHAT_MODEL=local-model\n')
    assert read()==['company','local-model','base-key']
    assert read({'CHAT_BACKEND':'openai','CHAT_MODEL':'process-model'})==['openai','process-model','base-key']


def test_all_chat_stages_use_company_model_even_with_old_rerank_setting(monkeypatch):
    provider=Provider(settings(chat_backend='company',rerank_model='gpt-4.1'))
    calls=[]
    def handle(request):
        body=json.loads(request.content)
        calls.append(body)
        assert request.url == 'http://chat.internal:9000/v1/chat/completions'
        assert body['model']=='company/sllm-v1' and 'authorization' not in request.headers
        user=body['messages'][-1]['content']
        if '재작성하세요' in body['messages'][0]['content']:
            return answer('{"query":"선박 검사 신청"}')
        if user.startswith('{'):
            payload=json.loads(user)
            if 'candidates' in payload:
                return answer(json.dumps({'decisions':[{'id':c['id'],'relevant':True,'role':'direct','conditions':[]}
                                                      for c in payload['candidates']]}))
            return answer('{"sufficient":true,"missing_issues":[],"missing_facts":[],"search_queries":[]}')
        return answer('검사를 신청해야 합니다. [1]')
    mock_http(monkeypatch,handle)
    assert provider.rewrite_question('선박 검사는?')=='선박 검사 신청'
    rows=provider.filter_evidence('선박 검사는?', [{'law_name':'선박검사법','number':'제1조','title':'검사','body':'검사를 신청한다.'}])
    assessment=provider.assess_evidence('선박 검사는?',rows,[],'2026-10-23')
    assert assessment['sufficient']
    assert provider.answer('선박 검사는?',rows,[],assessment=assessment).endswith('[1]')
    assert len(calls)==4


def test_company_model_listing_failure_keeps_configured_default(tmp_path,monkeypatch):
    mock_http(monkeypatch,lambda _:httpx.Response(404,json={}))
    with TestClient(create_app(settings(chat_backend='company',data_dir=tmp_path))) as client:
        result=client.get('/api/models').json()
    assert result['models']==['company/sllm-v1'] and result['warning']


def test_company_diagnostic_checks_both_connections_without_creating_db(tmp_path,monkeypatch,capsys):
    from app import check_ai
    config=settings(chat_backend='company',embedding_backend='company',data_dir=tmp_path/'not-created')
    monkeypatch.setattr(check_ai,'Settings',lambda:config)
    mock_http(monkeypatch,lambda r:vectors(r) if r.url.path.endswith('/embeddings') else answer('{"query":"선박 검사"}'))
    assert check_ai.main(['--chat'])==0
    output=capsys.readouterr().out
    assert '임베딩 연결 성공' in output and '채팅 연결 성공' in output and 'openai-private' not in output
    assert not config.data_dir.exists()


def test_company_selection_cannot_be_overwritten_by_openai_upgrade(monkeypatch):
    from app import upgrade_models
    monkeypatch.setattr(upgrade_models,'Settings',lambda:settings(embedding_backend='company'))
    monkeypatch.setattr(upgrade_models,'configure',lambda _:pytest.fail('Company config was overwritten'))
    assert upgrade_models.main()==2

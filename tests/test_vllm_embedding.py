"""Direct vLLM deployment contract; no real company server is contacted."""
import json
from dataclasses import replace
from pathlib import Path

import httpx
from dotenv import dotenv_values

from app import check_ai
from app.config import Settings
from app.nse_mq import NseMqSettings
from app.provider import Provider
from test_ai_backends import mock_http


def direct_settings(**overrides):
    # Exercise the exact checked-in profile users copy after git pull.
    env = dotenv_values(Path(__file__).parents[1] / 'local.env.vllm.example')
    return Settings(**(dict(
        api_key='openai-private', chat_backend=env['CHAT_BACKEND'],
        embedding_backend=env['EMBEDDING_BACKEND'],
        company_embedding_base_url=env['COMPANY_EMBEDDING_BASE_URL'],
        company_embedding_model=env['COMPANY_EMBEDDING_MODEL'],
        company_embedding_api_key=env['COMPANY_EMBEDDING_API_KEY'],
        embedding_max_input_bytes=int(env['EMBEDDING_MAX_INPUT_BYTES']),
        embedding_batch_size=int(env['EMBEDDING_BATCH_SIZE']),
    ) | overrides))


def forbid_mq(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Direct HTTP embeddings must not read MQ config or publish a request')
    monkeypatch.setattr(NseMqSettings, 'from_env', fail)
    monkeypatch.setattr(Provider, '_nse_request', fail)


def test_direct_vllm_matches_deployment_contract_and_restores_vector_order(monkeypatch):
    forbid_mq(monkeypatch)
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'POST'
        assert str(request.url) == 'http://127.0.0.1:8000/v1/embeddings'
        assert request.headers['content-type'] == 'application/json'
        assert 'authorization' not in request.headers
        assert json.loads(request.content) == {
            'model': 'emb-bge-m3-ko', 'input': ['안녕하세요 테스트', '법령 본문']}
        return httpx.Response(200, json={'object': 'list', 'model': 'emb-bge-m3-ko', 'data': [
            {'object': 'embedding', 'index': 1, 'embedding': [0.3, 0.4]},
            {'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2]},
        ]})
    mock_http(monkeypatch, handle)
    config = direct_settings()
    assert config.ai_configured('embedding')
    assert Provider(config).embed(['안녕하세요 테스트', '법령 본문']) == [[0.1, 0.2], [0.3, 0.4]]
    assert len(calls) == 1


def test_direct_vllm_diagnostic_needs_neither_mq_nor_chat_credentials(tmp_path, monkeypatch, capsys):
    forbid_mq(monkeypatch)
    config = direct_settings(api_key='', data_dir=tmp_path / 'unused')
    monkeypatch.setattr(check_ai, 'Settings', lambda: config)
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., .5]}]})
    mock_http(monkeypatch, handle)
    assert check_ai.main([]) == 0
    output = capsys.readouterr().out
    assert '임베딩 연결: vllm' in output and '임베딩 연결 성공: 2차원' in output
    assert len(calls) == 1 and not config.data_dir.exists()


def test_same_http_embedding_identity_does_not_depend_on_selector_or_chat(monkeypatch):
    forbid_mq(monkeypatch)
    config = direct_settings()
    fingerprint = Provider(config).fingerprint
    assert fingerprint == 'http://127.0.0.1:8000/v1|emb-bge-m3-ko'
    assert fingerprint == Provider(replace(config, embedding_backend='company')).fingerprint
    assert fingerprint == Provider(config).for_model('gpt-4.1').fingerprint
    assert fingerprint != Provider(replace(config, embedding_backend='openai')).fingerprint
    assert fingerprint != Provider(replace(config, company_embedding_model='other-weights')).fingerprint

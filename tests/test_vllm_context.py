import json

import httpx
import pytest

from app.laws import SetupError
from app.provider import Provider
from test_ai_backends import mock_http, settings
from test_chat_json import completion
from test_http_chat_stream import Chunks, event


@pytest.mark.parametrize('limit', [12288, 32384, 32768])
def test_actual_deployment_limit_minus_tokenized_prompt_used_for_json_and_stream(monkeypatch, limit):
    model = 'google-gemma-4-31B-it-awq'
    calls, budgets = [], []
    config = settings(chat_backend='company', company_chat_max_tokens='auto', company_chat_model=model,
                      company_chat_json_mode='prompt', company_chat_base_url='http://llm.test:9012/v1')
    def handle(request):
        calls.append(request)
        if request.method == 'GET':
            assert request.url.path == '/v1/models'
            return httpx.Response(200, json={'data': [{'id': model, 'max_model_len': limit}]})
        body = json.loads(request.content)
        if request.url.path == '/tokenize':
            assert body['model'] == model and body['messages']
            assert body['add_generation_prompt'] is True
            return httpx.Response(200, json={'count': 456, 'max_model_len': limit})
        assert request.url.path == '/v1/chat/completions'
        assert body['max_tokens'] == limit - 456
        budgets.append(body['max_tokens'])
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Chunks([
                event({'content': '답변'}), event(finish='stop'), 'data: [DONE]\n\n']))
        return httpx.Response(200, json=completion('{"query":"선박 검사"}'))
    mock_http(monkeypatch, handle)
    provider = Provider(config)
    assert provider.rewrite_question('선박 검사?') == '선박 검사'
    assert provider.stream_completion([{'role': 'user', 'content': '질문'}], lambda text: None) == '답변'
    assert budgets == [limit - 456] * 2
    assert sum(request.method == 'GET' for request in calls) == 1
    assert provider.last_token_budget == {'context_tokens': limit, 'input_tokens': 456, 'max_output_tokens': limit-456}
    # 다음 대화/모델 선택에서는 서버 한도를 다시 조회합니다.
    assert provider.for_model(model).rewrite_question('선박 검사?') == '선박 검사'
    assert sum(request.method == 'GET' for request in calls) == 2


@pytest.mark.parametrize('tokens', [12288, 13000])
def test_oversized_prompt_fails_before_generation_without_dropping_input(monkeypatch, tokens):
    messages = []
    def handle(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'gemma', 'max_model_len': 12288}]})
        assert request.url.path == '/tokenize'
        messages.extend(json.loads(request.content)['messages'])
        return httpx.Response(200, json={'count': tokens})
    mock_http(monkeypatch, handle)
    provider = Provider(settings(chat_backend='company', company_chat_model='gemma', company_chat_max_tokens='auto'))
    question = '조문의 예외 조건을 자르면 안 됩니다.'
    with pytest.raises(SetupError, match='조문을 임의로 자르지'):
        provider.rewrite_question(question)
    assert messages[-1]['content'] == question


@pytest.mark.parametrize('count', [True, -1, '300', None])
def test_bad_token_counts_cannot_create_false_budget(monkeypatch, count):
    def handle(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'gemma', 'max_model_len': 32768}]})
        assert request.url.path == '/tokenize'
        return httpx.Response(200, json={'count': count})
    mock_http(monkeypatch, handle)
    provider = Provider(settings(chat_backend='company', company_chat_model='gemma', company_chat_max_tokens='auto'))
    with pytest.raises(SetupError, match='count'):
        provider.rewrite_question('선박 검사?')


def test_smaller_tokenizer_limit_wins_without_using_estimated_token_counts(monkeypatch):
    def handle(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'gemma', 'max_model_len': 32768}]})
        if request.url.path == '/tokenize':
            return httpx.Response(200, json={'count': 500, 'max_model_len': 12288})
        assert json.loads(request.content)['max_tokens'] == 11788
        return httpx.Response(200, json=completion('{"query":"선박 검사"}'))
    mock_http(monkeypatch, handle)
    provider = Provider(settings(chat_backend='company', company_chat_model='gemma', company_chat_max_tokens='auto'))
    provider.rewrite_question('선박 검사?')
    assert provider.last_token_budget['context_tokens'] == 12288


def test_auto_mode_does_not_guess_when_tokenize_unavailable(monkeypatch):
    def handle(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'gemma', 'max_model_len': 32768}]})
        assert request.url.path == '/tokenize'
        return httpx.Response(404, json={'error': 'private upstream error'})
    mock_http(monkeypatch, handle)
    with pytest.raises(SetupError, match='tokenize') as exc:
        Provider(settings(chat_backend='company', company_chat_model='gemma', company_chat_max_tokens='auto')).rewrite_question('선박 검사?')
    assert 'private' not in str(exc.value)

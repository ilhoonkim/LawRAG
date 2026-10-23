import json

import httpx
import pytest

from app import check_ai
from app.chat_json import ChatJSONError, json_object, response_diagnostics
from app.laws import SetupError
from app.provider import Provider
from test_ai_backends import mock_http, settings, vectors


def completion(content, finish='stop', **message):
    return {'choices': [{'finish_reason': finish, 'message': {'content': content, **message}}]}


@pytest.mark.parametrize('wrapper', ['{}', '```json\n{}\n```', '```JSON\n{}\n```', '```\n{}\n```'])
def test_all_json_stages_accept_whole_fenced_objects(monkeypatch, wrapper):
    provider = Provider(settings(chat_backend='company', company_chat_json_mode='prompt'))
    replies = iter([
        {'query': '선박 검사 신청'},
        {'queries': ['선박 검사']},
        {'decisions': [{'id': 1, 'relevant': True, 'role': 'direct', 'conditions': []}]},
        {'sufficient': True, 'missing_issues': [], 'missing_facts': [], 'search_queries': []},
    ])
    monkeypatch.setattr(provider, 'post', lambda *args: completion(wrapper.format(json.dumps(next(replies)))))
    assert provider.rewrite_question('선박 검사는?') == '선박 검사 신청'
    assert provider.search_queries('선박 검사는?') == ['선박 검사']
    assert provider.filter_evidence('선박 검사는?', [{'body': '검사를 받는다.'}])[0]['relevance_checked']
    assert provider.assess_evidence('선박 검사는?', [{'body': '검사'}], [], '2026-10-23')['sufficient']


@pytest.mark.parametrize('reply', [
    completion('{"query":"검사"}', finish='length'),
    completion('{"query":"검사"}', refusal='private refusal'),
    completion('{"query":"검사"}', finish='content_filter'),
    completion(None, reasoning_content='private reasoning'),
    completion('설명입니다. {"query":"검사"}'),
    completion('```json\n{"query":"검사"}'),
    completion('[{"query":"검사"}]'),
    completion('<think>private</think>{"query":"검사"}'),
    {}, {'choices': [None]},
])
def test_partial_refused_reasoning_only_or_ambiguous_responses_fail_closed(reply, monkeypatch):
    with pytest.raises(ChatJSONError):
        json_object(reply)
    provider = Provider(settings())
    monkeypatch.setattr(provider, 'post', lambda *args: reply)
    with pytest.raises(SetupError, match='질문 재작성'):
        provider.rewrite_question('검사 서류')


def test_diagnostics_do_not_echo_response_content_or_unknown_finish_reason():
    info = response_diagnostics(completion('secret answer', finish='secret status', reasoning_content='secret thought'))
    assert info['content_chars'] == len('secret answer')
    assert info['reasoning_chars'] == len('secret thought')
    assert 'secret' not in json.dumps(info)


def test_chat_check_reports_reasoning_only_failure_without_printing_thoughts(tmp_path, monkeypatch, capsys):
    config = settings(chat_backend='company', embedding_backend='company', data_dir=tmp_path/'unused')
    monkeypatch.setattr(check_ai, 'Settings', lambda: config)
    mock_http(monkeypatch, lambda request: vectors(request) if request.url.path.endswith('/embeddings') else
              httpx.Response(200, json=completion(None, reasoning_content='private generated reasoning')))
    assert check_ai.main(['--chat']) == 1
    output = capsys.readouterr().out
    assert '추론 텍스트만' in output and 'reasoning_chars' in output
    assert 'private generated reasoning' not in output and not config.data_dir.exists()

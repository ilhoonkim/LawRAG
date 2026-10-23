import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import check_ai, main
from app.laws import SetupError
from app.provider import Provider
from test_ai_backends import mock_http, settings, vectors
from test_chat_json import completion


def event(delta=None, finish=None):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}, ensure_ascii=False) + '\n\n'


class Chunks(httpx.SyncByteStream):
    def __init__(self, events, before=None):
        self.events, self.before, self.closed = events, before, False

    def __iter__(self):
        for i, item in enumerate(self.events):
            if self.before:
                self.before(i)
            if isinstance(item, Exception):
                raise item
            # UTF-8 바이트 중간에서 네트워크 청크가 나뉘어도 글자가 깨지면 안 됩니다.
            encoded = item.encode()
            yield encoded[:7]
            yield encoded[7:]

    def close(self):
        self.closed = True


@pytest.mark.parametrize('backend,model', [('company', 'gemma'), ('openai', 'gpt-4.1'), ('openai', 'gpt-5')])
def test_http_stream_reaches_ui_callback_before_completion(monkeypatch, backend, model):
    updates, requests = [], []
    chunks = Chunks([event({'role': 'assistant', 'reasoning_content': 'private thought'}),
                     event({'content': '안녕'}), event({'content': '하세요.'}), event(finish='stop'),
                     'data: {"choices":[],"usage":{"completion_tokens":3}}\n\n', 'data: [DONE]\n\n'],
                    lambda i: None if i < 2 else assert_updated(updates))
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['stream'] is True
        assert body['model'] == model
        if model == 'gpt-5':
            assert 'temperature' not in body
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=chunks)
    mock_http(monkeypatch, handle)
    provider = Provider(settings(chat_backend=backend, chat_model=model, company_chat_model=model))
    selected = provider.for_stream(updates.append, lambda: False)
    assert selected.answer('질문', [], []) == '안녕하세요.'
    assert updates == ['안녕', '안녕하세요.']
    assert len(requests) == 1 and chunks.closed
    assert 'private' not in json.dumps(selected.last_stream_diagnostics)


def assert_updated(updates):
    assert updates  # 다음 네트워크 이벤트가 오기 전에 이미 화면으로 전달됨


@pytest.mark.parametrize('tail,match', [
    ([], '완료 신호'),
    ([event(finish='length'), 'data: [DONE]\n\n'], '출력 길이'),
    (['data: [DONE]\n\n'], '정상 완료'),
    (['data: {"error":{"message":"private upstream error"}}\n\n'], '형식 오류'),
    ([event({'refusal': 'private refusal'})], '형식 오류'),
    ([httpx.ReadTimeout('private connection details')], '연결 오류'),
])
def test_interrupted_or_invalid_stream_is_not_retried_or_completed(monkeypatch, tail, match):
    calls, updates = [], []
    chunks = Chunks([event({'content': '부분 답변'})] + tail)
    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=chunks)
    mock_http(monkeypatch, handle)
    provider = Provider(settings(chat_backend='company'))
    with pytest.raises(SetupError, match=match) as exc:
        provider.stream_completion([{'role': 'user', 'content': '질문'}], updates.append)
    assert 'private' not in str(exc.value)
    assert len(calls) == 1 and chunks.closed and updates == ['부분 답변']


def test_cancelled_stream_closes_upstream(monkeypatch):
    updates = []
    chunks = Chunks([event({'content': '첫 조각'}), event({'content': '둘째'}), event(finish='stop'), 'data: [DONE]\n\n'])
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=chunks))
    provider = Provider(settings(chat_backend='company')).for_stream(updates.append, lambda: bool(updates))
    with pytest.raises(InterruptedError):
        provider.answer('질문', [], [])
    assert updates == ['첫 조각'] and chunks.closed


def test_non_stream_response_is_not_fake_typed(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=completion('한 번에 온 답변')))
    with pytest.raises(SetupError, match='SSE'):
        Provider(settings(chat_backend='company')).stream_completion([], lambda text: pytest.fail('Fake stream'))


@pytest.mark.parametrize('complete', [True, False])
def test_sse_is_bridged_to_browser_ndjson_and_only_completed_answer_gets_result(tmp_path, monkeypatch, complete):
    def run(store, provider, config, body, progress):
        progress('answer')
        return {'answer': provider.answer(body.question, [], []), 'evidence': []}
    monkeypatch.setattr(main, 'run_chat', run)
    tail = [event(finish='stop'), 'data: [DONE]\n\n'] if complete else []
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'},
              stream=Chunks([event({'content': '전달된 답변'})] + tail)))
    with TestClient(main.create_app(settings(chat_backend='company', data_dir=tmp_path))) as client:
        events = [json.loads(line) for line in client.post('/api/chat/stream', json={'question': '검사 서류'}).text.splitlines() if line]
    assert any(e['type'] == 'answer_text' and e['text'] == '전달된 답변' for e in events)
    assert any(e['type'] == ('result' if complete else 'error') for e in events)
    assert not any(e['type'] == ('error' if complete else 'result') for e in events)


def test_stream_diagnostic_checks_json_and_actual_sse_without_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_ai, 'Settings', lambda: settings(chat_backend='company', embedding_backend='company', data_dir=tmp_path/'unused'))
    def handle(request):
        if request.url.path.endswith('/embeddings'):
            return vectors(request)
        if json.loads(request.content).get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Chunks([
                event({'content': '안녕'}), event({'content': '하세요'}), event(finish='stop'), 'data: [DONE]\n\n']))
        return httpx.Response(200, json=completion('```json\n{"query":"선박 검사"}\n```'))
    mock_http(monkeypatch, handle)
    assert check_ai.main(['--stream']) == 0
    output = capsys.readouterr().out
    assert '스트리밍 연결 성공' in output and '안녕하세요' in output
    assert not (tmp_path/'unused').exists()

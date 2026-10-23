"""첨부 NSE 규약의 합성 응답으로 점검. 실제 사내 MQ에는 연결하지 않습니다."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pika
import pytest

from app import check_nse_mq
from app.nse_mq import MqCheckError, NseMqClient, NseMqSettings, NseReply


def config(**values):
    return replace(NseMqSettings(host='mq.example.test', username='private-user',
                               password='private-password', model_id=22), **values)


def response(text='', *, status=None, number=1, chunk=None, success=True):
    content = {'generated_text': text}
    if status is not None:
        content['streaming_context'] = {'stream_status': status, 'chunk_no': number, 'current_chunk': chunk}
    return json.dumps({'content': content, 'result_status': {'code': 'SUCCESS' if success else 'FAIL',
                        'message': 'private upstream details', 'traceback': 'private trace'}}, ensure_ascii=False).encode()


class Broker:
    """네트워크 대신 pika 호출 경계에서 합성 수신 이벤트를 발생시킵니다."""
    def __init__(self, monkeypatch, *, stream=False, consumers=1, replies=None):
        self.is_open, self.closed = True, False
        self.events, self.acks, self.published = [], [], []
        self.callback = None
        self.stream, self.consumers, self.replies = stream, consumers, replies
        def connect(p):
            self.is_open = True
            return self
        monkeypatch.setattr(pika, 'BlockingConnection', connect)

    def channel(self):
        return self

    def queue_declare(self, **kw):
        self.events.append(('declare', kw))
        return NS(method=NS(consumer_count=self.consumers, message_count=3))

    def basic_qos(self, **kw):
        pass

    def basic_consume(self, **kw):
        self.callback = kw['on_message_callback']
        self.events.append(('consume', kw['queue']))

    def confirm_delivery(self):
        self.events.append(('confirm', None))

    def basic_publish(self, **kw):
        self.events.append(('publish', None))
        self.published.append(kw)

    def basic_ack(self, **kw):
        self.acks.append(kw['delivery_tag'])

    def process_data_events(self, time_limit):
        props = self.published[-1]['properties']
        items = self.replies
        if callable(items):
            items = items(json.loads(self.published[-1]['body']))
        if items is None:
            items = ([response('서울', status=21, number=1, chunk='서울'),
                      response('서울입니다', status=22, number=2, chunk='입니다'),
                      response('서울입니다.', status=23, number=3, chunk='.')]
                     if self.stream else [response('서울입니다.')])
        for i, raw in enumerate(items):
            self.callback(self, NS(delivery_tag=i), props, raw)

    def close(self):
        self.closed, self.is_open = True, False


def test_probe_never_publishes_or_declares_shared_queue(monkeypatch):
    broker = Broker(monkeypatch, consumers=0)
    client = NseMqClient(config(model_id=None))
    assert client.check(connect_only=True) == ''
    assert not broker.published and not broker.callback and broker.closed
    assert broker.events == [('declare', {'queue': 'nse_request', 'passive': True})]
    assert client.report['consumers'] == 0 and not client.report['published']


@pytest.mark.parametrize('stream', [False, True])
def test_nse_wire_format_subscription_correlation_and_cleanup(monkeypatch, stream):
    broker = Broker(monkeypatch, stream=stream)
    client = NseMqClient(config(domain_id=1, user_id=2))
    chunks = []
    assert client.check(stream=stream, on_chunk=chunks.append) == '서울입니다.'
    request = broker.published[0]
    payload, props = json.loads(request['body']), request['properties']
    assert payload['task'] == 'llm_generation'
    assert payload['master'] == {'request_id': props.correlation_id, 'domain_id': 1, 'user_id': 2}
    assert payload['content']['stream'] is stream
    assert payload['content']['physical_model_id'] == 22
    assert payload['content']['infer_argument']['max_tokens'] == 128
    assert props.content_type == 'application/json' and props.expiration == '120000'
    assert request['exchange'] == '' and request['mandatory'] is True
    assert request['routing_key'] == 'nse_request'
    assert 'private' not in request['body'].decode()
    temp = broker.events[1][1]
    assert temp == {'queue': props.reply_to, 'durable': False, 'exclusive': True, 'auto_delete': True}
    assert props.reply_to.startswith('nse_result_kih_')
    assert all(kw['queue'] == props.reply_to for name, kw in broker.events if name == 'declare' and not kw.get('passive'))
    names = [name for name, _ in broker.events]
    assert names.index('consume') < names.index('confirm') < names.index('publish')
    assert broker.closed and len(broker.published) == 1
    assert client.report['generated_chars'] == len('서울입니다.')
    assert client.report['elapsed_seconds'] >= 0
    if stream:
        assert payload['content']['streaming_reply_destination'] == {
            'streaming_reply_to': props.reply_to, 'correlation_id': props.correlation_id, 'task': 'llm_generation'}
        assert chunks == ['서울', '입니다'] and client.report['stream_statuses'] == [21, 22, 23]
        assert client.report['first_chunk_seconds'] is not None
    else:
        assert chunks == [] and 'streaming_reply_destination' not in payload['content']


def test_no_consumer_prevents_generation(monkeypatch):
    broker = Broker(monkeypatch, consumers=0)
    with pytest.raises(MqCheckError, match='소비자가 0'):
        NseMqClient(config()).check()
    assert broker.closed and not broker.published


def test_foreign_correlation_is_ignored_before_parsing():
    reply = NseReply('own', False, lambda _: pytest.fail('Unexpected chunk'))
    reply.accept(NS(correlation_id='other'), b'not JSON')
    assert not reply.done and reply.ignored == 1
    reply.accept(NS(correlation_id='own'), response('답변'))
    assert reply.done and reply.text == '답변'


def test_nonstream_does_not_accept_an_intermediate_result():
    reply = NseReply('own', False, lambda _: None)
    with pytest.raises(MqCheckError, match='최종 END'):
        reply.accept(NS(correlation_id='own'), response('미완성', status=21))
    assert not reply.done


def test_other_task_is_not_treated_as_llm_output():
    reply = NseReply('own', False, lambda _: None)
    with pytest.raises(MqCheckError, match='task'):
        reply.accept(NS(correlation_id='own'), json.dumps({'task':'embedding','content':{'generated_text':'다른 작업'}}).encode())


def test_duplicate_chunks_and_final_accumulation_do_not_repeat():
    chunks = []
    reply = NseReply('own', True, chunks.append)
    props = NS(correlation_id='own')
    for raw in [response('하', status=21, number=1, chunk='하'),
                response('하', status=21, number=1, chunk='하'), response('하하', status=22, number=2, chunk='하'),
                response('하하!', status=23, number=3, chunk='!')]:
        reply.accept(props, raw)
    assert chunks == ['하', '하'] and reply.text == '하하!' and reply.done


@pytest.mark.parametrize('items,snapshots', [
    ([response('짧은 답변', status=23, number=1, chunk='짧은 답변')], ['짧은 답변']),
    ([response('시작', status=21, number=1, chunk='시작'),
      response('시작 끝', status=23, number=2, chunk=' 끝')], ['시작', '시작 끝']),
    ([response('시작', status=21, number=1, chunk='시작'),
      response('시작 끝', status=22, number=2, chunk=' 끝'),
      response('시작 끝', status=23, number=2, chunk=' 끝')], ['시작', '시작 끝', '시작 끝']),
])
def test_streaming_document_final_only_last_chunk_and_repeated_end(items, snapshots):
    received = []
    reply = NseReply('own', True, lambda _:None, received.append)
    for raw in items:
        reply.accept(NS(correlation_id='own'), raw)
    assert reply.done and received == snapshots and reply.text == snapshots[-1]


@pytest.mark.parametrize('items,match', [
    ([b'bad json'], 'JSON'),
    ([b'[]'], '객체'),
    ([response(success=False)], '실패 상태'),
    ([response('답변')], 'streaming_context'),
    ([response(status=99)], 'stream_status'),
    ([response('앞', status=21), response('다른 결과', status=23, number=2)], '일치하지'),
    ([response(status=21), response('둘', status=22, number=3, chunk='둘'), response('둘하나', status=22, number=2, chunk='하나')], '순서'),
    ([response(status=21), response('하나', status=22, number=2, chunk='하나'), response('다름', status=22, number=2, chunk='다름')], '같은 순번'),
])
def test_invalid_reply_fails_and_closes_without_retry(monkeypatch, items, match):
    broker = Broker(monkeypatch, replies=items)
    with pytest.raises(MqCheckError, match=match) as caught:
        NseMqClient(config()).check(stream=True)
    assert broker.closed and len(broker.published) == 1
    assert 'private' not in str(caught.value)


def test_timeout_and_interrupt_clean_up(monkeypatch):
    from app import nse_mq
    broker = Broker(monkeypatch, replies=[])
    ticks = iter([0, 0, 0, 2, 2])
    monkeypatch.setattr(nse_mq.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(MqCheckError, match='제한 시간'):
        NseMqClient(config()).check(timeout=1)
    assert broker.closed and len(broker.published) == 1


def test_keyboard_interrupt_closes_reply_queue(monkeypatch):
    broker = Broker(monkeypatch)
    def interrupt(**kw):
        raise KeyboardInterrupt()
    broker.process_data_events = interrupt
    with pytest.raises(KeyboardInterrupt):
        NseMqClient(config()).check()
    assert broker.closed


def test_env_netrc_suffix_and_explicit_queue(monkeypatch, tmp_path):
    for name in list(__import__('os').environ):
        if name.startswith('NSE_') or name == 'SUFFIX':
            monkeypatch.delenv(name)
    path = tmp_path/'_netrc'
    path.write_text('machine mq.example.test login netrc-user password netrc-secret\n')
    monkeypatch.setenv('NSE_MQ_HOST', 'mq.example.test')
    monkeypatch.setenv('NSE_MQ_NETRC', str(path))
    monkeypatch.setenv('SUFFIX', 'some-environment')
    settings = NseMqSettings.from_env()
    assert settings.username == 'netrc-user' and settings.password == 'netrc-secret'
    assert settings.request_queue == 'nse_request_some_environment'
    assert 'netrc-secret' not in repr(settings)
    monkeypatch.setenv('NSE_MQ_SUFFIX', 'own')
    assert NseMqSettings.from_env().request_queue == 'nse_request_own'
    monkeypatch.setenv('NSE_MQ_REQUEST_QUEUE', 'exact_queue')
    assert NseMqSettings.from_env().request_queue == 'exact_queue'
    monkeypatch.setenv('NSE_MQ_USERNAME', 'explicit-user')
    with pytest.raises(MqCheckError, match='PASSWORD'):
        NseMqSettings.from_env().validate()  # Explicit user must not inherit another password.


@pytest.mark.parametrize('exception,match', [
    (pika.exceptions.ProbableAuthenticationError('private-password'), '인증'),
    (pika.exceptions.ProbableAccessDeniedError('private-vhost'), '가상 호스트'),
    (pika.exceptions.ChannelClosedByBroker(404, 'private-queue'), '큐가 없습니다'),
    (pika.exceptions.ChannelClosedByBroker(403, 'private-details'), '권한'),
    (pika.exceptions.AMQPConnectionError('private-host'), 'MQ 연결'),
])
def test_connection_errors_are_actionable_and_redacted(monkeypatch, exception, match):
    def fail(_):
        raise exception
    monkeypatch.setattr(pika, 'BlockingConnection', fail)
    with pytest.raises(MqCheckError, match=match) as caught:
        NseMqClient(config()).check()
    assert 'private' not in str(caught.value)


def test_cli_report_contains_metrics_without_credentials_or_text(monkeypatch, tmp_path, capsys):
    Broker(monkeypatch, stream=True)
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    path = tmp_path/'report.json'
    assert check_nse_mq.main(['--stream', '--report', str(path)]) == 0
    report = json.loads(path.read_text())
    assert report['success'] and report['chunks'] == 2 and report['stream_statuses'] == [21, 22, 23]
    assert '서울' not in path.read_text() and 'private' not in path.read_text()
    assert 'mq.example.test' not in path.read_text()
    assert 'private' not in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.json']


def test_cli_json_validation_failure_is_not_connection_failure(monkeypatch, tmp_path):
    broker = Broker(monkeypatch)
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    path = tmp_path/'report.json'
    assert check_nse_mq.main(['--json', '--report', str(path)]) == 1
    report = json.loads(path.read_text())
    assert not report['success'] and report['stage'] == 'json' and report['published']
    assert broker.closed


def test_cli_json_success(monkeypatch, tmp_path):
    Broker(monkeypatch, replies=[response('{"ok":true}')])
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    assert check_nse_mq.main(['--json', '--report', str(tmp_path/'report.json')]) == 0


def test_cli_configuration_error_does_not_dial(monkeypatch, tmp_path):
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config(model_id=None))
    monkeypatch.setattr(pika, 'BlockingConnection', lambda _:pytest.fail('Must validate model before connecting'))
    assert check_nse_mq.main(['--report', str(tmp_path/'report.json')]) == 1
    assert json.loads((tmp_path/'report.json').read_text())['stage'] == 'config'


def test_cli_final_only_does_not_claim_live_stream_observed(monkeypatch, tmp_path):
    Broker(monkeypatch, replies=[response('짧은 답변', status=23, number=1)])
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    path = tmp_path/'report.json'
    assert check_nse_mq.main(['--stream', '--report', str(path)]) == 1
    report = json.loads(path.read_text())
    assert report['stage'] == 'stream_unconfirmed' and not report['stream_observed']
    assert report['generated_chars'] == len('짧은 답변')


def test_cancel_closes_connection_without_republishing(monkeypatch):
    broker = Broker(monkeypatch, replies=[])
    client = NseMqClient(config())
    with pytest.raises(InterruptedError):
        client.check(cancelled=lambda: bool(broker.published))
    assert broker.closed and len(broker.published) == 1


def test_gpt_and_local_model_are_isolated_and_keep_embedding_identity(monkeypatch):
    import httpx
    from app.config import Settings
    from app.provider import Provider
    from test_ai_backends import mock_http, answer
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    broker = Broker(monkeypatch, replies=[response('{"query":"선박 검사"}')])
    http_calls = []
    def http(request):
        http_calls.append(request)
        return answer('{"query":"GPT 검색어"}')
    mock_http(monkeypatch, http)
    original = Provider(Settings(api_key='openai-secret', base_url='https://openai.test/v1'))
    local = original.for_model('LOCAL_MODEL')
    assert local.rewrite_question('선박 검사는?') == '선박 검사'
    assert not http_calls and len(broker.published) == 1
    payload = json.loads(broker.published[0]['body'])
    assert payload['content']['messages'][-1]['content'] == '선박 검사는?'
    assert payload['content']['stream'] is False and 'response_format' not in payload
    assert 'openai-secret' not in broker.published[0]['body'].decode()
    gpt = local.for_model('gpt-4.1')
    assert gpt.rewrite_question('선박 검사는?') == 'GPT 검색어'
    assert len(http_calls) == 1 and len(broker.published) == 1
    assert local.fingerprint == gpt.fingerprint == original.fingerprint
    assert original.settings.chat_backend == 'openai' and local.chat_model == 'LOCAL_MODEL'
    assert original.for_model('LOCA_MODEL').chat_model == 'LOCAL_MODEL'
    assert http_calls[0].headers['authorization'] == 'Bearer openai-secret'


def test_local_pipeline_json_stays_private_and_final_answer_streams(monkeypatch):
    from app.config import Settings
    from app.provider import Provider
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    def reply_to(payload):
        content = payload['content']
        messages = content['messages']
        user = messages[-1]['content']
        assert content['physical_model_id'] == 22
        if '재작성하세요' in messages[0]['content']:
            assert content['stream'] is False
            return [response('{"query":"선박 검사 신청"}')]
        if user.startswith('{'):
            assert content['stream'] is False
            data = json.loads(user)
            if 'candidates' in data:
                return [response(json.dumps({'decisions':[{'id':c['id'],'relevant':True,'role':'direct','conditions':[]}
                                             for c in data['candidates']]}))]
            return [response('{"sufficient":true,"missing_issues":[],"missing_facts":[],"search_queries":[]}')]
        assert content['stream'] is True
        assert messages[1] == {'role':'user','content':'이전 질문'}
        return [response('검사를', status=21, chunk='검사를'),
                response('검사를 신청하세요. [1]', status=23, number=2, chunk=' 신청하세요. [1]')]
    broker = Broker(monkeypatch, replies=reply_to)
    snapshots = []
    provider = Provider(Settings(rerank_model='gpt-4.1')).for_model('LOCAL_MODEL').for_stream(snapshots.append, lambda:False)
    assert provider.rewrite_question('선박 검사는?') == '선박 검사 신청'
    rows = provider.filter_evidence('선박 검사는?', [{'law_name':'선박검사법','number':'제1조','title':'검사','body':'검사를 신청한다.'}])
    assessment = provider.assess_evidence('선박 검사는?', rows, [], '2026-10-23')
    assert not snapshots and assessment['sufficient']
    assert provider.answer('선박 검사는?', rows, [{'role':'user','content':'이전 질문'}], assessment=assessment) == '검사를 신청하세요. [1]'
    assert snapshots == ['검사를', '검사를 신청하세요. [1]']
    queues = [r['properties'].reply_to for r in broker.published]
    assert len(queues) == len(set(queues)) == 4
    assert all(q.startswith('nse_result_kih_') for q in queues)


def test_local_models_list_and_status_do_not_expose_mq_settings(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    monkeypatch.setattr(pika, 'BlockingConnection', lambda _:pytest.fail('Listing models must not open MQ'))
    with TestClient(create_app(Settings(data_dir=tmp_path, chat_backend='local_model', api_key=''))) as client:
        models, status = client.get('/api/models').json(), client.get('/api/status').json()
    assert models['default'] == 'LOCAL_MODEL'
    assert 'gpt-4.1' in models['models'] and 'LOCAL_MODEL' in models['models']
    assert status['llm_configured'] and status['local_model_configured']
    assert 'private' not in json.dumps(status) + json.dumps(models)


def test_ndjson_delivers_snapshots_and_failure_does_not_produce_final_result(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings
    from app.provider import Provider
    from app import main as main_module
    from app.laws import SetupError
    monkeypatch.setattr(NseMqSettings, 'from_env', lambda: config())
    def pipeline(store, provider, settings, body, progress):
        progress('answer')
        for snapshot in ['첫', '첫 부분', '첫 부분 마지막']:
            provider._on_answer_text(snapshot)
        if body.question == '실패 질문':
            raise SetupError('합성 오류')
        return {'answer':'첫 부분 마지막','evidence':[]}
    monkeypatch.setattr(main_module, 'run_chat', pipeline)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        for question, final_type in [('정상 질문','result'), ('실패 질문','error')]:
            events = [json.loads(line) for line in client.post('/api/chat/stream', json={'question':question,'model':'LOCAL_MODEL'}).text.splitlines() if line]
            snapshots = [e['text'] for e in events if e['type'] == 'answer_text']
            assert snapshots[-1] == '첫 부분 마지막' and events[-1]['type'] == final_type
            if final_type == 'error':
                assert not any(e['type'] == 'result' for e in events)

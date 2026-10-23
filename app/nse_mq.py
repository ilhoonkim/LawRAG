"""NSE의 RabbitMQ LLM 요청/응답 규약. 웹 Provider와 진단 CLI가 함께 사용합니다.

참고: nse_mq_ref의 LlmGenerationContent/Result, StreamingContext, Master.
공유 요청 큐는 조회·발행만 하고, 응답은 이 연결 전용 임시 큐에서만 읽습니다.
모든 pika 객체는 같은 스레드에서 사용합니다. 발행 이후에는 자동 재시도하지
않습니다. 연결 오류여도 서버가 이미 생성 중일 수 있기 때문입니다.
"""
import json
import hashlib
import math
import netrc
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import ssl
import time
import uuid

import pika

from . import config as _config  # 프로세스 환경변수 > local.env > .env 로딩
from .laws import SetupError


class MqCheckError(SetupError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def _integer(name, default='', minimum=1):
    value = os.getenv(name, default)
    try:
        number = int(value)
        if number < minimum:
            raise ValueError()
        return number
    except (ValueError, TypeError):
        raise MqCheckError('config', f'{name}에 {minimum} 이상의 정수를 입력하세요.') from None


def _boolean(name):
    value = os.getenv(name, 'false').lower()
    if value not in ('true', 'false'):
        raise MqCheckError('config', f'{name}는 true 또는 false여야 합니다.')
    return value == 'true'


@dataclass(frozen=True)
class NseMqSettings:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    port: int = 5672
    vhost: str = '/'
    request_queue: str = 'nse_request'
    reply_prefix: str = 'nse_result_kih'
    tls: bool = False
    ca_file: str = ''
    model_id: int | None = None
    domain_id: int | None = None
    user_id: int | None = None

    @classmethod
    def from_env(cls):
        host = os.getenv('NSE_MQ_HOST', '').strip()
        username = os.getenv('NSE_MQ_USERNAME', '')
        password = os.getenv('NSE_MQ_PASSWORD', '')
        # 다른 계정의 비밀번호를 섞지 않도록 두 항목 모두 없을 때만 netrc 사용.
        if host and not username and not password:
            explicit = os.getenv('NSE_MQ_NETRC', '')
            paths = [Path(explicit).expanduser()] if explicit else [Path.home()/'.netrc', Path.home()/'_netrc']
            for path in paths:
                if not path.is_file():
                    if explicit:
                        raise MqCheckError('config', 'NSE_MQ_NETRC 파일을 찾지 못했습니다.')
                    continue
                try:
                    auth = netrc.netrc(str(path)).authenticators(host)
                except (OSError, netrc.NetrcParseError):
                    raise MqCheckError('config', 'netrc 형식 또는 읽기 권한을 확인하세요.') from None
                if auth:
                    username, password = auth[0], auth[2]
                    break
        suffix = os.getenv('NSE_MQ_SUFFIX', os.getenv('SUFFIX', '')).strip().replace('-', '_')
        queue = os.getenv('NSE_MQ_REQUEST_QUEUE', '').strip() or 'nse_request' + (f'_{suffix}' if suffix else '')
        optional_id = lambda name: _integer(name) if os.getenv(name, '').strip() else None
        return cls(host=host, port=_integer('NSE_MQ_PORT', '5672'),
                   username=username, password=password,
                   vhost=os.getenv('NSE_MQ_VHOST', '/'), request_queue=queue,
                   reply_prefix=os.getenv('NSE_MQ_REPLY_PREFIX', 'nse_result_kih'),
                   tls=_boolean('NSE_MQ_TLS'), ca_file=os.getenv('NSE_MQ_CA_FILE', ''),
                   model_id=optional_id('NSE_PHYSICAL_MODEL_ID'),
                   domain_id=optional_id('NSE_DOMAIN_ID'), user_id=optional_id('NSE_USER_ID'))

    def validate(self, generation=False):
        if not self.host or any(c in self.host for c in '/@\r\n'):
            raise MqCheckError('config', 'NSE_MQ_HOST에 URL이 아닌 MQ 호스트명을 입력하세요.')
        if not self.username or not self.password:
            raise MqCheckError('config', 'NSE_MQ_USERNAME/PASSWORD 또는 해당 호스트의 netrc 계정이 필요합니다.')
        if not 1 <= self.port <= 65535:
            raise MqCheckError('config', 'NSE_MQ_PORT는 1~65535여야 합니다.')
        if not self.vhost or not self.request_queue or len(self.request_queue.encode()) > 255:
            raise MqCheckError('config', 'NSE_MQ_VHOST와 NSE_MQ_REQUEST_QUEUE를 확인하세요.')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,180}', self.reply_prefix):
            raise MqCheckError('config', 'NSE_MQ_REPLY_PREFIX는 영문·숫자·밑줄·하이픈 1~180자여야 합니다.')
        if self.ca_file and not self.tls:
            raise MqCheckError('config', 'CA 파일을 사용하려면 NSE_MQ_TLS=true로 설정하세요.')
        if generation and (type(self.model_id) is not int or self.model_id <= 0):
            raise MqCheckError('config', 'NSE_PHYSICAL_MODEL_ID 또는 --model에 실제 모델의 숫자 ID를 입력하세요.')

    def parameters(self):
        tls_options = None
        if self.tls:
            try:
                context = ssl.create_default_context(cafile=self.ca_file or None)
            except (OSError, ssl.SSLError):
                raise MqCheckError('config', 'NSE_MQ_CA_FILE의 인증서와 읽기 권한을 확인하세요.') from None
            tls_options = pika.SSLOptions(context, self.host)
        return pika.ConnectionParameters(
            host=self.host, port=self.port, virtual_host=self.vhost,
            credentials=pika.PlainCredentials(self.username, self.password),
            ssl_options=tls_options, heartbeat=60, socket_timeout=10, stack_timeout=15,
            blocked_connection_timeout=10, connection_attempts=1)


def build_request(settings, request_id, reply_queue, prompt, stream, max_tokens, messages=None, temperature=0.0,
                  texts=None, served_model_name=None):
    """HTTP와 같은 NSE 본문 + AMQP 속성으로 응답 목적지를 전달합니다."""
    master = {'request_id': request_id}
    for key in ('domain_id', 'user_id'):
        value = getattr(settings, key)
        if value is not None:
            master[key] = value
    if texts is not None:
        # 임베딩은 physical_model_id가 아니라 served_model_name으로 선택합니다.
        return {'master': master, 'task': 'embedding',
                'content': {'texts': texts, 'served_model_name': served_model_name}}
    content = {'physical_model_id': settings.model_id, 'query': prompt,
               'system': '한국어로 짧게 답하세요.', 'stream': stream,
               'infer_argument': {'max_tokens': max_tokens, 'temperature': temperature,
                                  'top_p': 0.9, 'repetition_penalty': 1.1}}
    if messages:
        # NSE는 messages가 있으면 query/system을 무시합니다. 재작성/검토의
        # 시스템 지시와 최종 답변의 대화 이력을 그대로 전달합니다.
        content['messages'] = messages
        content['query'] = ''
        content.pop('system')
    if stream:
        # 최종 reply_to와 스트리밍 목적지를 명시적으로 같은 전용 큐로 맞춥니다.
        content['streaming_reply_destination'] = {
            'streaming_reply_to': reply_queue, 'correlation_id': request_id,
            'task': 'llm_generation'}
    return {'master': master, 'task': 'llm_generation', 'content': content}


class NseReply:
    """AMQP correlation_id를 우선 확인하고, 델타와 최종 누적 응답을 구분합니다."""
    def __init__(self, request_id, stream, on_chunk, on_snapshot=lambda text: None,
                 task='llm_generation', expected_count=0):
        self.request_id, self.stream, self.on_chunk = request_id, stream, on_chunk
        self.on_snapshot = on_snapshot
        self.text, self.partial, self.done = '', '', False
        self.statuses, self.seen = [], {}
        self.messages = self.ignored = self.chunks = 0
        self.first_response = self.first_chunk = None
        self.started = time.monotonic()
        self.last_chunk_no = -1
        self.task, self.expected_count, self.vectors = task, expected_count, None

    def accept(self, properties, raw):
        if self.done:
            return
        if properties.correlation_id != self.request_id:
            self.ignored += 1
            return  # 본문에 같은 request_id가 있어도 다른 AMQP 응답을 가져오지 않습니다.
        if len(raw) > 2_000_000:
            raise MqCheckError('response', 'NSE 응답이 점검용 최대 크기를 초과했습니다.')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise MqCheckError('response', 'NSE 응답이 JSON 형식이 아닙니다.') from None
        if not isinstance(data, dict):
            raise MqCheckError('response', 'NSE 응답은 JSON 객체여야 합니다.')
        if data.get('task') not in (None, self.task):
            raise MqCheckError('response', 'NSE 응답의 task가 요청한 작업과 다릅니다.')
        master = data.get('master') or {}
        if not isinstance(master, dict) or master.get('request_id') not in (None, '', self.request_id):
            raise MqCheckError('response', 'AMQP correlation_id와 응답 master.request_id가 다릅니다.')
        result = data.get('result_status')
        if result is not None and (not isinstance(result, dict) or result.get('code') != 'SUCCESS' or result.get('is_success') is False):
            # 서버 message/detail/traceback에는 내부 정보가 포함될 수 있어 출력하지 않습니다.
            raise MqCheckError('server', 'NSE가 실패 상태를 반환했습니다. request_id로 서버 로그와 모델 상태를 확인하세요.')
        content = data.get('content')
        if not isinstance(content, dict):
            raise MqCheckError('response', 'NSE 응답에 content 객체가 없습니다.')
        self.messages += 1
        if self.first_response is None:
            self.first_response = time.monotonic() - self.started
        if self.task == 'embedding':
            self._embedding(content)
            return
        context = content.get('streaming_context')
        if not self.stream:
            # stream=false에는 최종 결과 한 건만 와야 합니다. 상태가 있으면
            # END를 검사해 중간 내용을 완성된 JSON으로 오인하지 않습니다.
            if context is not None and (not isinstance(context, dict) or context.get('stream_status') not in (23, 'END')):
                raise MqCheckError('response', '비스트리밍 요청에 최종 END가 아닌 응답이 도착했습니다.')
            self._finish(content)
            return
        if not isinstance(context, dict):
            raise MqCheckError('stream', 'stream=true 응답에 streaming_context가 없습니다.')
        status = context.get('stream_status')
        status = {'START': 21, 'STREAMING': 22, 'END': 23}.get(status, status) if isinstance(status, (str, int)) else None
        number, chunk = context.get('chunk_no'), context.get('current_chunk')
        if type(status) is not int or status not in (21, 22, 23) or type(number) is not int or number < 1:
            raise MqCheckError('stream', 'stream_status 또는 chunk_no 형식이 올바르지 않습니다.')
        if chunk is not None and not isinstance(chunk, str):
            raise MqCheckError('stream', 'current_chunk가 문자열이 아닙니다.')
        snapshot = content.get('generated_text')
        if not isinstance(snapshot, str):
            raise MqCheckError('stream', '누적 generated_text가 문자열이 아닙니다.')
        key = (status, number)
        # 누적 본문을 모든 순번마다 저장하면 긴 답변의 메모리가 제곱으로 증가합니다.
        # 재전송 판별에는 해시만 남기고 실제 전문은 최신 본문 하나만 보관합니다.
        signature = (hashlib.sha256(chunk.encode('utf-8')).digest() if chunk is not None else None,
                     hashlib.sha256(snapshot.encode('utf-8')).digest())
        if key in self.seen:
            if self.seen[key] != signature:
                raise MqCheckError('stream', '같은 순번에 서로 다른 스트리밍 청크가 도착했습니다.')
            return  # 동일 순번의 재전송만 제거; 같은 텍스트의 새 순번은 보존합니다.
        self.seen[key] = signature
        if status not in self.statuses:
            self.statuses.append(status)
        if status == 23:
            self._finish(content)
            if not self.text.startswith(self.partial):
                raise MqCheckError('stream', '수신한 델타와 최종 generated_text가 일치하지 않습니다.')
            # STREAMING.md: 동일 큐는 마지막 조각이 최종 응답에만 포함됩니다.
            # 한 조각짜리 답변은 START 없이 END(1)만 와도 정상입니다.
            self.on_snapshot(self.text)
            return  # END의 generated_text는 누적 값이므로 청크에 중복 추가하지 않습니다.
        if number <= self.last_chunk_no or (status == 21 and number != 1):
            raise MqCheckError('stream', '스트리밍 청크의 순서가 뒤바뀌었습니다.')
        self.last_chunk_no = number
        if len(snapshot) > 1_000_000 or not snapshot.startswith(self.partial):
            raise MqCheckError('stream', '스트리밍 누적 응답의 길이 또는 앞부분이 일치하지 않습니다.')
        delta = snapshot[len(self.partial):]
        self.partial = snapshot
        if delta:
            self.chunks += 1
            if self.first_chunk is None:
                self.first_chunk = time.monotonic() - self.started
            self.on_chunk(delta)
            self.on_snapshot(snapshot)

    def _finish(self, content):
        text = content.get('generated_text')
        if not isinstance(text, str) or not text.strip():
            raise MqCheckError('response', '최종 content.generated_text가 비어 있거나 문자열이 아닙니다.')
        self.text, self.done = text, True

    def _embedding(self, content):
        # NSE: texts[i] → embedding_vectors[i][0]. 다중 wrapper를 임의로
        # 평탄화하면 토큰별 벡터를 문장 벡터로 오인할 수 있어 정확히 검사합니다.
        wrapped = content.get('embedding_vectors')
        if not isinstance(wrapped, list) or len(wrapped) != self.expected_count:
            raise MqCheckError('embedding', 'NSE 임베딩 응답의 벡터 수가 입력 수와 다릅니다.')
        vectors, dimension = [], None
        for item in wrapped:
            if not isinstance(item, list) or len(item) != 1 or not isinstance(item[0], list):
                raise MqCheckError('embedding', 'NSE embedding_vectors[i][0] 형식이 올바르지 않습니다.')
            vector = item[0]
            if not vector or not all(type(x) in (int, float) and math.isfinite(x) for x in vector) or not any(vector):
                raise MqCheckError('embedding', 'NSE 임베딩 벡터에 비정상 수치 또는 영벡터가 있습니다.')
            if dimension is not None and dimension != len(vector):
                raise MqCheckError('embedding', 'NSE 임베딩 벡터의 차원이 서로 다릅니다.')
            dimension = len(vector)
            vectors.append(vector)
        self.vectors, self.done = vectors, True


class NseMqClient:
    def __init__(self, settings):
        self.settings = settings
        self.report = {}

    def check(self, *, connect_only=False, stream=False, prompt='대한민국의 수도를 한 문장으로 알려주세요.',
              timeout=120.0, max_tokens=128, on_progress=lambda message: None,
              on_chunk=lambda chunk: None, on_snapshot=lambda text: None,
              messages=None, temperature=0.0, cancelled=lambda: False,
              texts=None, served_model_name=None):
        embedding = texts is not None
        self.settings.validate(generation=not connect_only and not embedding)
        if embedding and (stream or not isinstance(texts, list) or not 1 <= len(texts) <= 16
                          or not all(isinstance(t, str) and t.strip() for t in texts)
                          or not isinstance(served_model_name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}', served_model_name)):
            raise MqCheckError('config', '임베딩은 비스트리밍이며 texts 1~16개와 served_model_name이 필요합니다.')
        if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise MqCheckError('config', '--timeout은 1~3600초여야 합니다.')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 32768 or not prompt.strip():
            raise MqCheckError('config', 'max_tokens는 1~32768이며 질문은 비어 있으면 안 됩니다.')
        request_id = ('lawrag-embedding-' if embedding else 'lawrag-chat-' if messages else 'lawrag-check-') + uuid.uuid4().hex
        connection, phase, reply = None, 'connect', None
        self.report = {'request_id': request_id, 'mode': 'connect' if connect_only else 'embedding' if embedding else 'stream' if stream else 'reply',
                       'model_id': None if connect_only or embedding else self.settings.model_id, 'published': False}
        started = time.monotonic()
        try:
            if cancelled():
                raise InterruptedError()
            on_progress('MQ 연결·인증 확인 중')
            connection = pika.BlockingConnection(self.settings.parameters())
            channel = connection.channel()
            phase = 'queue'
            # passive=True: 없는 큐를 만들어 성공한 것처럼 보이지 않게 합니다.
            queue = channel.queue_declare(queue=self.settings.request_queue, passive=True).method
            self.report.update(consumers=queue.consumer_count, queued_messages=queue.message_count)
            on_progress(f'MQ 연결·인증 및 요청 큐 확인 완료 · 소비자 {queue.consumer_count}개')
            if connect_only:
                return ''
            if not queue.consumer_count:
                raise MqCheckError('queue', '요청 큐의 소비자가 0개입니다. NSE 서비스와 큐 이름/SUFFIX를 확인하세요. 요청은 보내지 않았습니다.')
            phase = 'subscribe'
            # 종료·오류·Ctrl+C 때 연결이 닫히면 이 큐도 자동 삭제됩니다.
            reply_queue = self.settings.reply_prefix + '_' + uuid.uuid4().hex
            channel.queue_declare(queue=reply_queue, durable=False, exclusive=True, auto_delete=True)
            channel.basic_qos(prefetch_count=32)
            reply = NseReply(request_id, stream, on_chunk, on_snapshot,
                             task='embedding' if embedding else 'llm_generation',
                             expected_count=len(texts) if embedding else 0)

            def receive(ch, method, properties, body):
                try:
                    reply.accept(properties, body)
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            # 먼저 구독을 완료해야 아주 빠른 첫 청크도 놓치지 않습니다.
            channel.basic_consume(queue=reply_queue, on_message_callback=receive, auto_ack=False)
            channel.confirm_delivery()
            payload = build_request(self.settings, request_id, reply_queue, prompt, stream, max_tokens,
                                    messages, temperature, texts, served_model_name)
            phase = 'publish'
            if cancelled():
                raise InterruptedError()
            reply.started = time.monotonic()
            channel.basic_publish(exchange='', routing_key=self.settings.request_queue, mandatory=True,
                properties=pika.BasicProperties(content_type='application/json', content_encoding='utf-8',
                    correlation_id=request_id, message_id=request_id, reply_to=reply_queue,
                    delivery_mode=1, expiration=str(int(timeout*1000))),
                body=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            self.report['published'] = True
            phase = 'response'
            on_progress('요청 발행 확인 완료 · 응답 대기 중')
            while not reply.done:
                if cancelled():
                    raise InterruptedError()
                remaining = timeout - (time.monotonic() - reply.started)
                if remaining <= 0:
                    raise MqCheckError('timeout', '제한 시간 안에 최종 응답을 받지 못했습니다. 모델 로딩·NSE 로그·응답 큐 권한을 확인하세요. 자동 재발행하지 않았습니다.')
                connection.process_data_events(time_limit=min(0.2, remaining))
            return reply.vectors if embedding else reply.text
        except InterruptedError:
            raise
        except pika.exceptions.ProbableAuthenticationError:
            raise MqCheckError('auth', 'MQ 인증이 거부되었습니다. 계정·비밀번호를 확인하세요.') from None
        except pika.exceptions.ProbableAccessDeniedError:
            raise MqCheckError('auth', 'MQ 가상 호스트 접근이 거부되었습니다. VHOST와 계정 권한을 확인하세요.') from None
        except pika.exceptions.ChannelClosedByBroker as exc:
            if exc.reply_code == 404:
                message = '요청 큐가 없습니다. NSE_MQ_REQUEST_QUEUE 또는 NSE_MQ_SUFFIX를 확인하세요.'
            else:
                message = 'MQ 채널이 거부되었습니다. 요청 큐 조회·발행 및 전용 응답 큐 접두사에 대한 생성·구독 권한을 확인하세요.'
            raise MqCheckError(phase, message) from None
        except (pika.exceptions.UnroutableError, pika.exceptions.NackError):
            raise MqCheckError('publish', '브로커가 요청 발행을 확인하지 못했습니다. 큐 상태를 확인하세요. 자동 재발행하지 않았습니다.') from None
        except (pika.exceptions.AMQPError, OSError):
            raise MqCheckError(phase, 'MQ 연결이 실패하거나 끊겼습니다. VPN·DNS·포트·TLS·VHOST와 계정 권한을 확인하세요. 발행 중 오류라면 서버가 요청을 받았을 수도 있습니다.') from None
        finally:
            self.report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            if reply:
                self.report.update(messages=reply.messages, ignored_messages=reply.ignored,
                    chunks=reply.chunks, stream_statuses=reply.statuses,
                    first_response_seconds=reply.first_response, first_chunk_seconds=reply.first_chunk,
                    generated_chars=len(reply.text), received_chars=len(reply.partial),
                    stream_observed=bool(reply.chunks))
                if embedding:
                    self.report.update(input_count=len(texts), vector_count=len(reply.vectors or []),
                                       dimensions=len(reply.vectors[0]) if reply.vectors else None)
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except (pika.exceptions.AMQPError, OSError):
                    pass

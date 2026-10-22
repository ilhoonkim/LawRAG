"""Transient failures recover without replaying successful batches or leaking credentials."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json

import httpx
import pytest

from app import http_retry
from app.config import Settings
from app.laws import LawAPI, SetupError
from app.provider import Provider

REAL_CLIENT = httpx.Client


@pytest.fixture
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr(http_retry.time,'sleep',delays.append)
    monkeypatch.setattr(http_retry.random,'uniform',lambda *_:0)
    return delays


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(httpx,'Client',lambda **kw:REAL_CLIENT(transport=httpx.MockTransport(handler),**kw))


@pytest.mark.parametrize('status',[408,409,425,429,500,502,503,504])
def test_transient_status_retries_three_times_then_succeeds(monkeypatch, waits, status):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls)<4:
            return httpx.Response(status,json={'error':{'code':'rate_limit_exceeded'}})
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[1.,0.]}]})
    mock_http(monkeypatch,handle)
    assert Provider(Settings(api_key='test')).embed(['조문'])==[[1.,0.]]
    assert len(calls)==4 and all(c==calls[0] for c in calls)
    assert waits==[2,4,8]


@pytest.mark.parametrize('status,code',[(400,'invalid_request_error'),(401,'invalid_api_key'),
    (403,'permission_denied'),(404,'model_not_found'),(422,None),(429,'insufficient_quota'),
    (429,'billing_hard_limit_reached'),(501,None)])
def test_permanent_failures_not_repeated(monkeypatch,waits,status,code):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,json={'error':{'code':code}})
    mock_http(monkeypatch,handle)
    with pytest.raises(SetupError,match=f'HTTP {status}'):
        Provider(Settings(api_key='test')).embed(['조문'])
    assert len(calls)==1 and waits==[]


def test_retry_after_is_respected_bounded_and_invalid_headers_ignored(monkeypatch,waits):
    for value,expected in [('12',12),('900',60),('garbage',2),('nan',2),('-5',2)]:
        assert http_retry.retry_delay(1,httpx.Response(429,headers={'Retry-After':value}))==expected
    when=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=25),usegmt=True)
    assert 23 <= http_retry.retry_delay(1,httpx.Response(503,headers={'Retry-After':when})) <=25


def test_network_failures_and_final_error_are_sanitized(monkeypatch,waits,caplog):
    calls=[]
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('secret-token https://private.test',request=request)
    mock_http(monkeypatch,handle)
    with pytest.raises(SetupError,match='시간 초과') as exc:
        Provider(Settings(api_key='secret-token')).embed(['private legal text'])
    assert len(calls)==4 and '총 4회' in str(exc.value) and waits==[2,4,8]
    assert 'secret-token' not in str(exc.value)+caplog.text
    assert 'private' not in str(exc.value)+caplog.text


def test_successful_embedding_batches_are_not_replayed(monkeypatch,waits):
    calls=[]
    def handle(request):
        batch=json.loads(request.content)['input']
        calls.append(batch)
        if len(calls)==2:
            raise httpx.ConnectError('temporary',request=request)
        return httpx.Response(200,json={'data':[{'index':i,'embedding':[float(t)+1,0.]} for i,t in enumerate(batch)]})
    mock_http(monkeypatch,handle)
    assert Provider(Settings(api_key='test')).embed([str(i) for i in range(17)])==[[float(i)+1,0.] for i in range(17)]
    assert [len(c) for c in calls]==[16,1,1] and waits==[2]


def test_400_reports_review_stage_and_parameter_without_raw_response(monkeypatch,waits):
    mock_http(monkeypatch,lambda request:httpx.Response(400,json={'error':{
        'type':'invalid_request_error','code':'invalid_json_schema','param':'response_format',
        'message':'secret-value'}}))
    provider=Provider(Settings(api_key='test',rerank_model='gpt-4.1'))
    with pytest.raises(SetupError) as exc:
        provider.filter_evidence('신청 방법', [{'law_name':'합성법','number':'제1조','title':'신청','body':'신청한다.'}])
    reason=str(exc.value)
    assert all(s in reason for s in ['근거 관련성 검토','gpt-4.1','response_format','JSON'])
    assert 'secret-value' not in reason and waits==[]


def test_exhausted_status_reports_attempt_count(monkeypatch,waits):
    mock_http(monkeypatch,lambda _:httpx.Response(503,json={'error':{'message':'secret'}}))
    with pytest.raises(SetupError,match='총 4회 시도') as exc:
        Provider(Settings(api_key='test')).embed(['조문'])
    assert 'HTTP 503' in str(exc.value) and len(waits)==3


def test_law_api_uses_same_retry_without_exposing_oc(monkeypatch,waits,caplog):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(502) if len(calls)==1 else httpx.Response(200,content=b'<law/>')
    mock_http(monkeypatch,handle)
    assert LawAPI('private-oc').request('lawService.do',MST='123')==b'<law/>'
    assert len(calls)==2 and waits==[2] and 'private-oc' not in caplog.text

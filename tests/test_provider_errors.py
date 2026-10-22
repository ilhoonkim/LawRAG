import httpx
import pytest

from app import check_ai
from app.config import Settings
from app.laws import SetupError
from app.provider import Provider

REAL_CLIENT = httpx.Client

@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr('app.http_retry.time.sleep',lambda _:None)

def mock_response(monkeypatch, handler):
    monkeypatch.setattr('app.provider.httpx.Client',
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('status,code,expected', [
    (401,'invalid_api_key','인증이 거부'),
    (403,'permission_denied','접근이 거부'),
    (404,'model_not_found','모델 또는 API 경로'),
    (429,'insufficient_quota','잔액 또는 결제 한도'),
    (429,'rate_limit_exceeded','요청 한도를 초과'),
    (400,'context_length_exceeded','토큰 한도'),
    (400,None,'입력 또는 모델 설정'),
    (503,None,'서버 오류'),
])
def test_ai_status_and_safe_category_without_echoing_secret(monkeypatch,status,code,expected):
    secret = 'sk-test-credential-never-show'
    mock_response(monkeypatch,lambda request: httpx.Response(status,json={
        'error':{'code':code,'message':f'Invalid key {secret}; https://example.test/?key={secret}'}}))
    with pytest.raises(SetupError) as caught:
        Provider(Settings(api_key=secret)).embed(['연결 확인'])
    reason = str(caught.value)
    assert f'HTTP {status}' in reason and expected in reason
    assert secret not in reason and 'https://' not in reason


@pytest.mark.parametrize('exception,expected', [(httpx.ConnectError,'연결 오류'),(httpx.ReadTimeout,'시간 초과')])
def test_transport_errors_do_not_echo_urls(monkeypatch,exception,expected):
    def fail(request):
        raise exception('secret-credential-in-upstream-exception',request=request)
    mock_response(monkeypatch,fail)
    with pytest.raises(SetupError,match=expected) as caught:
        Provider(Settings(api_key='test')).embed(['연결 확인'])
    assert 'secret-credential' not in str(caught.value)


def test_non_json_and_unknown_error_code_are_not_echoed(monkeypatch):
    mock_response(monkeypatch,lambda request: httpx.Response(400,json={
        'error':{'code':'secret-value','message':'secret-value'}}))
    with pytest.raises(SetupError) as caught:
        Provider(Settings(api_key='test')).embed(['연결 확인'])
    assert 'HTTP 400' in str(caught.value) and 'secret-value' not in str(caught.value)
    mock_response(monkeypatch,lambda request: httpx.Response(200,text='secret-value'))
    with pytest.raises(SetupError,match='응답 형식 오류') as caught:
        Provider(Settings(api_key='test')).embed(['연결 확인'])
    assert 'secret-value' not in str(caught.value)


def test_diagnostic_reports_embedding_result_without_writing_data(tmp_path,monkeypatch,capsys):
    settings = Settings(api_key='sk-test-private',data_dir=tmp_path/'unused')
    monkeypatch.setattr(check_ai,'Settings',lambda:settings)
    monkeypatch.setattr(check_ai,'ai_key_source',lambda:'LLM_API_KEY / 프로젝트 .env')
    mock_response(monkeypatch,lambda request: httpx.Response(200,json={
        'data':[{'index':0,'embedding':[1.,0.]}]}))
    assert check_ai.main() == 0
    output = capsys.readouterr().out
    assert '임베딩 연결 성공' in output and 'sk-test-private' not in output
    assert not settings.data_dir.exists()


def test_diagnostic_reports_quota_failure(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(check_ai,'Settings',lambda:Settings(api_key='test',data_dir=tmp_path/'unused'))
    mock_response(monkeypatch,lambda request: httpx.Response(429,json={'error':{'code':'insufficient_quota'}}))
    assert check_ai.main() == 1
    assert 'insufficient_quota' in capsys.readouterr().out


def test_reported_key_source_matches_configuration_precedence(monkeypatch):
    from app import config
    monkeypatch.setenv('LLM_API_KEY','primary-private')
    monkeypatch.setenv('OPENAI_API_KEY','fallback-private')
    monkeypatch.setattr(config,'_KEYS_FROM_PROCESS',{'LLM_API_KEY'})
    assert config.ai_key_source() == 'LLM_API_KEY / 프로세스 환경변수 (.env보다 우선)'
    monkeypatch.setattr(config,'_KEYS_FROM_PROCESS',set())
    assert config.ai_key_source() == 'LLM_API_KEY / 프로젝트 .env'
    monkeypatch.setenv('LLM_API_KEY','')
    assert config.ai_key_source() == 'OPENAI_API_KEY / 프로젝트 .env'


def test_embedding_failure_reason_persists_to_collection_status(tmp_path,monkeypatch):
    from app.management import Management, CollectionInput
    from app.store import Store
    from test_management import FakeAPI
    mock_response(monkeypatch,lambda request:httpx.Response(429,json={'error':{'code':'insufficient_quota'}}))
    manager = Management(Store(tmp_path),Provider(Settings(api_key='test')),FakeAPI())
    queued = manager.queue(CollectionInput(items=[{'mst':'101','name':'선박안전법','effective':'20260101'}]))
    manager.run(queued['ids'])
    event = manager.history(1)['items'][0]
    assert event['status'] == 'failed' and 'HTTP 429 / insufficient_quota' in event['reason']
    status = manager.decorate({'kind':'law','id':'101','effective':'20260101'})
    assert status['reason'] == event['reason'] and status['version'] is None

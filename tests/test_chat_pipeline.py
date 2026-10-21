import json

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app, ChatRequest
from app.provider import Provider
from app.laws import SetupError
from app.chat_pipeline import run_chat
from test_workflow import FakeProvider


def row(n, score, body='서류 제출 본문'):
    return {'version':'v1', 'law_name':'합성법', 'number':f'제{n}조', 'title':f'제목{n}',
            'body':body, 'score':score, 'html_url':f'/documents/v1.html#제{n}조'}


class PipelineStore:
    def temporal_versions(self, cutoff):
        return {'versions':[], 'missing':[], 'note':'합성 테스트'}
    def search(self, question, provider, threshold, limit, mode, **kw):
        assert provider.steps == ['rewrite']
        assert question == '집주인이 필요한 서류는?'
        assert kw['query_variants'] == ['임대인 필요 준비 서류']
        assert limit == 20
        provider.steps.append('search')
        return [row(1,.9), row(2,.8,'무관한 본문'), row(3,.7)]
    def expand_references(self, roots):
        assert {r['number'] for r in roots} == {'제1조','제3조'}
        # A rejected search result must not be reintroduced as a reference.
        return [{**r,'reference_titles':[{'number':'제2조','law_name':'합성법','title':'무관한 조문'}]} for r in roots] + [
            row(2,.9,'무관한 본문'), {**row(4,.95),'referenced_by':'제1조'},
            {**row(5,.95,'무관한 본문'),'referenced_by':'제1조'}], [{'law_name':'외부법','number':'별표 1'}]


class PipelineProvider:
    def __init__(self):
        self.steps = []
        self.context = None
    def rewrite_question(self, question):
        self.steps.append('rewrite')
        return '임대인 필요 준비 서류'
    def filter_evidence(self, question, candidates):
        self.steps.append('review')
        if any(r['number'] == '제4조' for r in candidates):
            assert all('parent_context' in r for r in candidates)
            assert all(r['number'] != '제2조' for r in candidates)
        return [r for r in candidates if '무관한' not in r['body']]
    def answer(self, question, evidence, history, missing_references=None):
        self.steps.append('answer')
        assert '집주인이 필요한 서류는?' in question
        assert '임대인 필요 준비 서류' not in question
        assert missing_references == [{'law_name':'외부법','number':'별표 1'}]
        self.context = evidence
        return '확인된 서류 [1]'


def test_four_stages_rejected_refs_removed_sorted_before_answer():
    provider, stages = PipelineProvider(), []
    result = run_chat(PipelineStore(),provider,Settings(),ChatRequest(question='집주인이 필요한 서류는?',limit=2),stages.append)
    assert stages == ['rewrite','search','review','answer']
    assert [r['number'] for r in result['evidence']] == ['제4조','제1조','제3조']
    assert result['evidence'] == provider.context
    assert all(not r.get('reference_titles') for r in result['evidence'])
    assert result['generated']


def test_rejected_everything_does_not_generate_or_expand():
    class EmptyStore(PipelineStore):
        def expand_references(self, roots):
            assert roots == []
            return [], []
    provider = PipelineProvider()
    provider.filter_evidence = lambda *a: []
    stages = []
    result = run_chat(EmptyStore(),provider,Settings(),ChatRequest(question='집주인이 필요한 서류는?'),stages.append)
    assert result['evidence'] == [] and not result['generated']
    assert 'answer' not in stages


@pytest.mark.parametrize('decisions', [[], [{'id':1,'relevant':'true'}],
    [{'id':1,'relevant':True},{'id':1,'relevant':False}],
    [{'id':99,'relevant':True}], [{'id':True,'relevant':True}]])
def test_verification_rejects_malformed_or_invented_decisions(monkeypatch, decisions):
    provider = Provider(Settings(api_key='test-only'))
    monkeypatch.setattr(provider,'post',lambda *a:{'choices':[{'message':{'content':json.dumps({'decisions':decisions})}}]})
    with pytest.raises(SetupError,match='검증'):
        provider.filter_evidence('질문', [row(1,.9)])


def test_filter_reads_full_bodies_batches_and_keeps_original_data(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    batches = []
    def post(path, body):
        docs = json.loads(body['messages'][-1]['content'])['candidates']
        batches.append(docs)
        return {'choices':[{'message':{'content':json.dumps({'decisions':[{'id':d['id'],'relevant':d['number'] != '제2조'} for d in docs]})}}]}
    monkeypatch.setattr(provider,'post',post)
    candidates = [row(i,.9, '본문' * 5000 + f'끝{i}') for i in range(1,4)]
    result = provider.filter_evidence('질문', candidates)
    assert len(batches) == 3
    assert batches[2][0]['body'].endswith('끝3')
    assert [r['number'] for r in result] == ['제1조','제3조']
    assert all(r['relevance_checked'] for r in result)
    assert result[0]['html_url'] == candidates[0]['html_url']


@pytest.mark.parametrize('content', ['{}','not-json','{"query": null}', '{"query":""}'])
def test_rewrite_bad_output_is_explicit_failure(monkeypatch, content):
    provider = Provider(Settings(api_key='test-only'))
    monkeypatch.setattr(provider,'post',lambda *a:{'choices':[{'message':{'content':content}}]})
    with pytest.raises(SetupError,match='재작성'):
        provider.rewrite_question('집주인 서류')


def test_selected_model_is_request_local_and_gpt5_omits_temperature(monkeypatch):
    import httpx
    settings = Settings(api_key='test-only', chat_model='gpt-4.1', embedding_model='text-embedding-3-large')
    original = Provider(settings)
    selected = original.for_model('gpt-5.4')
    other = original.for_model('gpt-4o-mini')
    assert selected.settings.rerank_model == 'gpt-5.4'
    assert selected.fingerprint == original.fingerprint == other.fingerprint
    assert original.settings.chat_model == 'gpt-4.1'
    requests = []
    real_client = httpx.Client
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"query":"임대인 필요 서류"}'}}]})
    monkeypatch.setattr(httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(handle), **kw))
    assert selected.rewrite_question('집주인 서류') == '임대인 필요 서류'
    assert 'temperature' not in requests[-1]
    other.rewrite_question('집주인 서류')
    assert requests[-1]['model'] == 'gpt-4o-mini' and requests[-1]['temperature'] == 0


def test_stream_reports_real_stage_order_and_stops_on_failure(tmp_path):
    class FailedProvider(FakeProvider):
        def rewrite_question(self, question):
            raise SetupError('합성 재작성 오류')
    app = create_app(Settings(data_dir=tmp_path),FailedProvider(),object())
    client = TestClient(app)
    response = client.post('/api/chat/stream',json={'question':'준비 서류'})
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert [e['type'] for e in events] == ['stage','error']
    assert events[0]['stage'] == 'rewrite' and events[1]['detail'] == '합성 재작성 오류'
    assert 'evidence' not in response.text


def test_stream_and_json_use_identical_verified_context(tmp_path):
    from test_workflow import RAW
    app = create_app(Settings(data_dir=tmp_path),FakeProvider(),object())
    app.state.store.ingest(RAW,FakeProvider())
    client = TestClient(app)
    body = {'question':'선박안전법 적용범위', 'as_of':'2026-10-21'}
    events = [json.loads(line) for line in client.post('/api/chat/stream',json=body).text.splitlines() if line]
    assert [e['stage'] for e in events if e['type']=='stage'] == ['rewrite','search','review','answer']
    assert events[-1]['result'] == client.post('/api/chat',json=body).json()

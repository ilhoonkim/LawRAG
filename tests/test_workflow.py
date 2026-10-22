from pathlib import Path
from urllib.parse import unquote
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.laws import parse_law, render_html, LawAPI, SetupError
from app.store import Store
from app.main import create_app

RAW = (Path(__file__).parent / 'fixture.xml').read_bytes()

class FakeProvider:
    """Deterministic test double; no assertion here measures real model quality."""
    fingerprint = 'test-model-v1'
    calls = 0
    def embed(self, texts):
        return [[1., .1, 0.] if any(t in s for t in ['적용', '범위']) else [0., .1, 1.] for s in texts]
    def rewrite_question(self, question):
        return question
    def assess_evidence(self, question, evidence, missing_references, as_of):
        return {"sufficient":bool(evidence),"missing_issues":[],"missing_facts":[],"search_queries":[]}
    def filter_evidence(self, question, candidates):
        return candidates
    def answer(self, question, evidence, history, missing_references=None, assessment=None):
        self.calls += 1
        assert evidence[0]['number'] == '제3조'
        return '테스트용 선박은 적용 제외 대상입니다. [1]'

@pytest.fixture
def provider():
    return FakeProvider()

@pytest.fixture
def store(tmp_path, provider):
    s = Store(tmp_path)
    s.ingest(RAW, provider)
    return s


def test_parser_preserves_hierarchy_newlines_and_branch():
    law = parse_law(RAW)
    assert len(law.articles) == 3
    article = law.articles[1]
    assert article.section_id == '선박안전법-제3조'
    assert '\n①' in article.body and '\n1.' in article.body
    assert '\n┃ 항목' in article.body
    assert law.articles[2].number == '제30조의2'


def test_html_safe_and_anchor():
    law = parse_law(RAW)
    law.articles[1].body += '<script>alert(1)</script>'
    html = render_html(law)
    assert 'id="선박안전법-제3조"' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'white-space:pre-wrap' in html

@pytest.mark.parametrize('raw', [b'<bad>', b'<root/>', b'<!DOCTYPE x><root/>'])
def test_invalid_xml(raw):
    with pytest.raises(ValueError):
        parse_law(raw)

@pytest.mark.parametrize('mode', ['hybrid', 'keyword'])
def test_application_scope_ranks_first(store, provider, mode):
    results = store.search('선박안전법에 적용되지 않는 것은?', provider, .48, mode=mode)
    assert results and results[0]['number'] == '제3조'
    assert all(e['score'] >= .48 for e in results)
    assert unquote(results[0]['html_url']).endswith('#선박안전법-제3조')


def test_unrelated_question_abstains(store, provider):
    assert store.search('초콜릿 케이크 레시피', provider, .48, mode='keyword') == []


def test_threshold_is_enforced(store, provider):
    assert store.search('선박안전법에 적용되지 않는 것은?', provider, 1.) == []


def test_reingest_no_duplicate_and_old_snapshot_retained(store, provider):
    before = store.list_laws()[0]['html']
    store.ingest(RAW, provider)
    assert store.list_laws()[0]['article_count'] == 3
    store.ingest(RAW.replace('테스트 대상'.encode(), '변경된 테스트 대상'.encode()), provider)
    after = store.list_laws()[0]['html']
    assert before != after
    assert (store.directory / 'html' / before).exists()
    assert (store.directory / 'html' / after).exists()


def test_embedding_failure_preserves_previous_index(store, provider):
    class Broken(FakeProvider):
        def embed(self, texts):
            raise SetupError('upstream unavailable')
    before = store.list_laws()
    with pytest.raises(SetupError):
        store.ingest(RAW, Broken())
    assert store.list_laws() == before


def test_model_mismatch_requires_reindex(store, provider):
    provider.fingerprint = 'different-model'
    with pytest.raises(SetupError):
        store.search('선박안전법 적용범위', provider)


def test_keyword_only_explicit(tmp_path, provider):
    s = Store(tmp_path)
    s.ingest(RAW, provider, keyword_only=True)
    assert s.list_laws()[0]['embedded_count'] == 0
    with pytest.raises(SetupError):
        s.search('선박안전법 적용범위', provider)
    assert s.search('선박안전법 적용범위', provider, mode='keyword')


def test_missing_oc():
    with pytest.raises(SetupError, match='LAW_API_OC'):
        LawAPI('').fetch('123')


def test_named_law_filters_other_laws(store, provider):
    other = RAW.replace(b'TEST001', b'TEST002').replace('선박안전법'.encode(), '다른법'.encode())
    store.ingest(other, provider)
    results = store.search('선박안전법에 적용되지 않는 것은?', provider)
    assert {r['law_name'] for r in results} == {'선박안전법'}


def test_api_workflow_and_no_evidence_no_llm(tmp_path, provider):
    class FakeLawAPI:
        def search(self, query, page):
            return {'total':'1', 'page':page, 'laws':[{'id':'123','name':'선박안전법','effective':'테스트자료'}]}
        def fetch(self, mst):
            return RAW
    client = TestClient(create_app(Settings(data_dir=tmp_path), provider, FakeLawAPI()))
    assert client.get('/').status_code == 200
    assert client.get('/api/laws/search?query=선박안전법').json()['total'] == '1'
    assert client.post('/api/laws/ingest', json={'mst':'123'}).json()['articles'] == 3
    response = client.post('/api/chat', json={'question':'선박안전법에 적용되지 않는 것은?'})
    assert response.status_code == 200
    result = response.json()
    assert result['generated'] and result['evidence'][0]['number'] == '제3조'
    doc = client.get(result['evidence'][0]['html_url'].split('#')[0])
    assert doc.status_code == 200 and '선박안전법-제3조' in doc.text
    assert 'Content-Security-Policy' in doc.headers
    assert provider.calls == 1
    result = client.post('/api/chat', json={'question':'초콜릿 케이크 레시피', 'mode':'keyword'}).json()
    assert not result['generated'] and not result['evidence'] and provider.calls == 1
    assert client.post('/api/search', json={'question':'테스트', 'limit':11}).status_code == 422
    assert client.post('/api/laws/ingest', json={'mst':'../a'}).status_code == 422
    assert client.post('/api/chat', json={'question':'테스트'}, headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.get('/documents/invalid.html').status_code == 404


def test_missing_key_does_not_silently_answer(tmp_path):
    from app.provider import Provider
    provider = Provider(Settings(data_dir=tmp_path, api_key=''))
    with pytest.raises(SetupError, match='LLM_API_KEY'):
        provider.embed(['테스트'])


def test_embedded_break_tags():
    raw = RAW.replace('검사 서류를 제출한다.'.encode(), b'first<br/>second&lt;br/&gt;third')
    assert parse_law(raw).articles[2].body == 'first\nsecond\nthird'


def test_provider_protocol_and_sorted_embeddings(tmp_path, monkeypatch):
    import httpx
    import app.provider as mod
    seen = []
    def handle(request):
        import json
        data = json.loads(request.content)
        seen.append((request.url.path, data))
        assert request.headers['Authorization'] == 'Bearer test-only'
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data':[{'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}]})
        return httpx.Response(200, json={'choices':[{'message':{'content':'근거 답변 [1]'}}]})
    real_client = httpx.Client
    monkeypatch.setattr(mod.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    provider = mod.Provider(Settings(data_dir=tmp_path, api_key='test-only'))
    assert provider.embed(['a','b']) == [[1.,0.],[0.,1.]]
    answer = provider.answer('질문', [{'law_name':'법','number':'제1조','title':'제목','body':'본문'}], [])
    assert answer == '근거 답변 [1]'
    assert '[1] 법 제1조(제목) 시행일: 미상\n본문' in seen[-1][1]['messages'][-1]['content']


def test_law_api_protocol(tmp_path, monkeypatch):
    import httpx
    import app.laws as mod
    def handle(request):
        assert request.url.params['OC'] == 'test-oc'
        if request.url.path.endswith('lawSearch.do'):
            assert request.url.params['target'] == 'eflaw'
            assert request.url.params['nw'] == '3'
            return httpx.Response(200, content='<LawSearch><totalCnt>1</totalCnt><law><법령일련번호>123</법령일련번호><법령명한글>선박안전법</법령명한글><시행일자>20260101</시행일자></law></LawSearch>'.encode())
        assert request.url.params['target'] == 'law'
        assert request.url.params['MST'] == '123'
        return httpx.Response(200, content=RAW)
    real_client = httpx.Client
    monkeypatch.setattr(mod.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    api = LawAPI('test-oc')
    assert api.search('선박안전법')['laws'][0]['id'] == '123'
    assert parse_law(api.fetch('123')).articles[1].number == '제3조'

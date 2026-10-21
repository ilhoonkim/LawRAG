"""Synthetic retrieval cases; these texts are not authoritative legal content."""
import json
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.laws import SetupError
from app.main import create_app
from app.provider import Provider
from app.store import Store

QUESTION = '전세사기 당했는데 서울에선 얼마까지 보호되나요'
TERMS = '소액임차인 보증금 최우선변제 범위 금액 서울특별시'


def sample_xml(name, articles):
    nodes = ''.join(f'<조문단위><조문번호>{i}</조문번호><조문제목>{escape(title)}</조문제목>'
                    f'<조문내용>{escape(body)}</조문내용></조문단위>' for i, (title,body) in enumerate(articles,1))
    return (f'<법령><기본정보><법령ID>{name}</법령ID><법령명_한글>{name}</법령명_한글>'
            '<시행일자>20200101</시행일자></기본정보><조문>' + nodes + '</조문></법령>').encode()


SAMPLES = [
    sample_xml('합성 임대차법', [
        ('보증금 중 일정액의 보호', '[합성 테스트 데이터: 실제 법령 아님] 소액임차인의 보증금 최우선변제 범위와 금액을 정한다. 서울특별시의 지역 구분을 확인한다.'),
        ('계약갱신 요구 등', '[합성 테스트 데이터: 실제 법령 아님] 임차인의 계약갱신 요구와 임대인의 거절 사유에 관한 규정.')]),
    sample_xml('합성 조세법', [('세율', '[합성 테스트 데이터: 실제 법령 아님] 소득세의 과세표준과 세율을 정한다.')]),
]


class SearchProvider:
    fingerprint = 'synthetic-semantic-vectors'

    def __init__(self):
        self.batches = []
        self.expansions = []

    def embed(self, texts):
        self.batches.append(list(texts))
        vectors = []
        for text in texts:
            index = (0 if any(t in text for t in ['소액임차인','보증금']) else
                     1 if '계약갱신' in text else 2 if '소득세' in text or '조세' in text else 3)
            vectors.append([float(i == index) for i in range(4)])
        return vectors

    def search_queries(self, question, candidates=None):
        self.expansions.append(question)
        assert candidates and 'body' in candidates[0]
        return [TERMS]


def test_ordinary_question_searches_all_laws_and_keeps_original(tmp_path):
    store, provider = Store(tmp_path), SearchProvider()
    for raw in SAMPLES:
        store.ingest(raw,provider,archive_only=True)
    result = store.search(QUESTION,provider)
    assert provider.expansions == [QUESTION]
    assert provider.batches[-2:] == [[QUESTION], [TERMS]]
    assert len(result) == 1
    assert result[0]['law_name'] == '합성 임대차법'
    assert result[0]['title'] == '보증금 중 일정액의 보호'
    assert result[0]['matched_query'] == TERMS
    assert result[0]['score'] >= .48
    assert result[0]['title_score'] >= .15 and result[0]['body_score'] >= .20
    assert '합성 테스트 데이터' in result[0]['body']  # evidence comes from stored XML
    assert store.search(QUESTION,provider,threshold=1) == []


def test_keyword_search_stays_offline(tmp_path):
    store, provider = Store(tmp_path), SearchProvider()
    store.ingest(SAMPLES[0],provider,archive_only=True)
    provider.batches.clear()
    store.search(QUESTION,provider,mode='keyword')
    assert provider.expansions == [] and provider.batches == []


def test_chat_answers_unnamed_original_question_from_stored_evidence(tmp_path):
    provider = SearchProvider()
    answered = []
    def answer(question, evidence, history):
        answered.append(question)
        assert QUESTION in question and TERMS not in question
        assert evidence[0]['title'] == '보증금 중 일정액의 보호'
        return '합성 자료에 근거한 테스트 답변 [1]'
    provider.answer = answer
    app = create_app(Settings(data_dir=tmp_path),provider,law_api=object())
    for raw in SAMPLES:
        app.state.store.ingest(raw,provider,archive_only=True)
    with TestClient(app) as client:
        response = client.post('/api/chat',json={'question':QUESTION})
        assert response.status_code == 200
        result = response.json()
        assert result['generated'] and result['missing_versions'] == []
        assert result['evidence'][0]['law_name'] == '합성 임대차법'
        assert len(answered) == 1


def test_empty_expansion_still_searches_original_question(tmp_path):
    store, provider = Store(tmp_path), SearchProvider()
    store.ingest(SAMPLES[0],provider,archive_only=True)
    provider.search_queries = lambda *args,**kwargs: []
    assert store.search(TERMS,provider)[0]['title'] == '보증금 중 일정액의 보호'


def test_different_queries_cannot_supply_title_and_body_separately(tmp_path):
    store, provider = Store(tmp_path), SearchProvider()
    store.ingest(sample_xml('합성 불일치법', [('보증금 보호','소득세 과세표준')]),provider,archive_only=True)
    provider.search_queries = lambda q, **kw: ['보증금 보호','소득세 과세표준']
    assert store.search(QUESTION,provider) == []


@pytest.mark.parametrize('content', ['invalid json', '{}', '{"queries":"wrong"}', '{"queries":[null,3,{},"x"]}'])
def test_invalid_expansion_response_falls_back_to_original(monkeypatch, content):
    provider = Provider(Settings(api_key='test-key'))
    monkeypatch.setattr(provider,'post',lambda *a,**kw:{'choices':[{'message':{'content':content}}]})
    assert provider.search_queries(QUESTION) == []


def test_expansion_is_bounded_and_provider_failure_is_optional(monkeypatch):
    provider = Provider(Settings(api_key='test-key'))
    def post(path, body):
        assert path == '/chat/completions'
        assert json.loads(body['messages'][-1]['content'])['question'] == QUESTION
        return {'choices':[{'message':{'content':json.dumps({'queries':[TERMS,TERMS,'임차권등기명령','세 번째 쿼리','a'*201]})}}]}
    monkeypatch.setattr(provider,'post',post)
    assert provider.search_queries(QUESTION) == [TERMS,'임차권등기명령']
    def fail(*args,**kwargs):
        raise SetupError('AI API unavailable')
    monkeypatch.setattr(provider,'post',fail)
    assert provider.search_queries(QUESTION) == []

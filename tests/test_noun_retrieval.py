"""Noun matching/recall regressions; synthetic vectors, no legal-year assertions."""
from concurrent.futures import ThreadPoolExecutor
from math import sqrt
from xml.sax.saxutils import escape

import pytest
import sqlite_vec
from fastapi.testclient import TestClient

from app.config import Settings
from app.keywords import NounIndex, query_terms
from app.main import create_app
from app.store import Store
from test_field_rrf import QueryProvider


QUESTION = '총톤수 10톤 미만 소형어선의 단독경보형감지기는 몇 년도 이후 건조된 어선부터 설치해야 하는가?'
NAME = '총톤수 10톤 미만 소형어선의 구조 및 설비기준'
# 사용자 화면의 설치 요건 발췌입니다. 개정일을 건조 적용일로 해석하지 않습니다.
ARTICLE_58 = ('총톤수 2톤 이상 어선의 무인기관실에는 다음의 어느 하나에 해당하는 소화설비를 갖추어야 한다. '
              '다만, 선실의 경우에는 화재탐지장치 및 휴대식 소화기로 갈음할 수 있다.\n'
              '1. 해당 기관실 용적에 충분한 용량의 무인기관실용 자동 소화장치 및 단독경보형감지기 <개정 2021. 10. 26.>\n'
              '2. 화재탐지장치 및 고정식 소화장치')


@pytest.mark.parametrize('question', ['단독경보형감지기는 설치해야 하나요?',
                                     '단독경보형감지기를 설치하나요?',
                                     '단독 경보형 감지기의 설치 기준은?'])
def test_particles_spacing_and_compound_nouns(question):
    nouns, compounds = query_terms(question)
    assert '단독경보형감지기' in nouns | compounds
    assert not any(t in nouns | compounds for t in ('는', '를', '의', '기준', '하나요'))
    index = NounIndex(['소방설비', '일반'], [ARTICLE_58, '무관한 주차장 관리'])
    plan = index.plan(question)
    assert '단독경보형감지기' in plan.matches(0, 'body')
    assert plan.score(plan.matches(0, 'body')) > .5
    assert plan.score(plan.matches(1, 'body')) == 0


def test_specific_noun_outweighs_common_terms_and_repetition():
    common = '어선의 설치 기준과 사항을 정한다.'
    bodies = [ARTICLE_58, '건조된 어선에는 설비를 설치한다.'] + [common] * 40
    index = NounIndex(['일반'] * len(bodies), bodies)
    plan = index.plan(QUESTION)
    assert plan.weights['단독경보형감지기'] > plan.weights['어선']
    assert plan.score(plan.matches(0, 'body')) > plan.score(plan.matches(1, 'body'))
    assert plan.score_text(common * 100) == plan.score_text(common)
    assert '단독경보형감지기' not in query_terms('관련 기준과 사항은 무엇인가?')[0]


def test_no_substring_fragment_or_punctuation_spanning_compound_match():
    index = NounIndex(['일반'] * 3, ['단독경보형감지기', '단독경보형소화기', '단독경보형. 감지기'])
    plan = index.plan('단독경보형감지기는?')
    assert plan.matches(0, 'body') == ['단독경보형감지기']
    assert plan.score(plan.matches(1, 'body')) == 0
    assert plan.score(plan.matches(2, 'body')) == 0
    assert '단독경보형감지기' not in query_terms('단독경보형. 감지기')[1]


def test_missing_key_noun_is_not_discarded_from_query_and_titles_do_not_fake_body_hits():
    index = NounIndex(['단독경보형감지기', '일반'], ['어선의 건조 조건', '어선 검사 기준'])
    plan = index.plan('어선의 단독경보형감지기 설치')
    assert '설치' in plan.weights  # 자료에 없는 질문의 독립 명사도 분모에 유지합니다.
    assert '단독경보형감지기' in plan.matches(0, 'title')
    assert '단독경보형감지기' not in plan.matches(0, 'body')
    assert plan.score(plan.matches(0, 'body')) < plan.score(plan.matches(0, 'title'))


def test_query_analysis_is_cached_and_safe_for_concurrent_requests():
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(query_terms, [QUESTION] * 8))
    assert all(terms == result[0] for terms in result)
    before = query_terms.cache_info()
    query_terms(QUESTION)
    assert query_terms.cache_info().hits == before.hits + 1


def corpus(store):
    articles = [('58', '무인기관실의 소화장치', ARTICLE_58),
                ('59', '소방설비', '어선의 소방설비 설치에 관한 합성 시험 문장이다.')]
    articles += [(str(100+i), '일반사항', '어선 검사 및 건조에 관한 기준과 사항을 정한다.') for i in range(40)]
    nodes = ''.join(f'<조문내용>제{n}조({title}) {escape(body)}</조문내용>' for n, title, body in articles)
    raw = (f'<행정규칙><행정규칙ID>noun-fixture</행정규칙ID><행정규칙명>{NAME}</행정규칙명>'
           f'<시행일자>20200101</시행일자>{nodes}</행정규칙>').encode()
    store.ingest(raw, QueryProvider(), kind='admrul', archive_only=True)
    pack = sqlite_vec.serialize_float32
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=?,title_vector=?',
                   (pack([.1, sqrt(.99), 0.]), pack([0., 1., 0.])))
        db.execute("UPDATE version_articles SET vector=? WHERE number='제59조'", (pack([.8, .6, 0.]),))


@pytest.mark.parametrize('mode', ['hybrid', 'keyword'])
def test_article_58_reaches_top_candidates_with_weak_embedding_and_generic_title(tmp_path, mode):
    store = Store(tmp_path)
    corpus(store)
    with store.connect() as db:
        vectors = list(map(tuple, db.execute('SELECT vector,title_vector FROM version_articles ORDER BY position')))
    provider = QueryProvider()
    hits = store.search_enhanced(QUESTION, provider, mode=mode, threshold=.3, fusion='rrf', limit=15)
    hit = next(e for e in hits if e['number'] == '제58조')
    assert '단독경보형감지기' in hit['matched_body_nouns']
    assert hit['body_keyword_score'] > hit['body_ngram_score']
    assert hit['weighted_score'] >= .3
    if mode == 'hybrid':
        old = .8 * (.65*hit['body_semantic_score'] + .35*hit['body_ngram_score']) + .2*hit['title_ngram_score'] + .2*hit['law_name_score']
        assert old < .3  # 기존 문자인식·약한 벡터에서는 임계치로 누락되는 사례를 재현합니다.
        assert any(e['number'] == '제59조' for e in hits)
    assert hit['matched_units'][0]['location'] == '제1호'
    assert '다만' in hit['body'] and '2. 화재탐지장치' in hit['body']
    assert provider.queries == ([[QUESTION]] if mode == 'hybrid' else [])
    with store.connect() as db:
        assert vectors == list(map(tuple, db.execute('SELECT vector,title_vector FROM version_articles ORDER BY position')))


def test_matched_article_is_reviewed_by_llm_and_remains_an_original_source(tmp_path):
    class Provider(QueryProvider):
        def filter_evidence(self, question, candidates):
            assert len(candidates) <= 15
            assert any(e['number'] == '제58조' for e in candidates)
            return [e for e in candidates if e['number'] in ('제58조', '제59조')]

        def answer(self, question, evidence, history, **kwargs):
            assert any(e['number'] == '제58조' and '단독경보형감지기' in e['body'] for e in evidence)
            return '설치 요건은 확인되지만 건조 적용 시점은 추가 근거가 필요합니다. [1]'

    app = create_app(Settings(data_dir=tmp_path), Provider(), object())
    corpus(app.state.store)
    with TestClient(app) as client:
        response = client.post('/api/chat', json={'question': QUESTION})
    assert response.status_code == 200
    result = response.json()
    assert result['generated'] and result['fusion'] == 'rrf'
    hit = next(e for e in result['evidence'] if e['number'] == '제58조')
    assert hit['law_name'] == NAME and hit['relevance_checked']
    assert '/documents/' in hit['html_url']

"""여객 정원 검색 회귀 검사.

사용자가 제시한 실제 법령명/조문 제목을 사용하되 본문과 벡터는 합성합니다.
아래 문장은 법령 원문이나 적용 범위의 법적 판단으로 사용하지 않습니다.
"""
from math import sqrt

import pytest
import sqlite_vec
from fastapi.testclient import TestClient

from app.retrieval import law_name_score
from app.keywords import NounIndex
from app.config import Settings
from app.main import create_app
from app.store import Store
from test_enhanced_retrieval import put
from test_field_rrf import QueryProvider


QUESTION = '일반선의 여객인원을 산정할 때 어떤 항목을 검토해야 하는가?'
GENERAL = '선박설비기준'
SPECIAL = '범선의 구조 및 설비 등에 관한 기준'


@pytest.mark.parametrize('question,name', [
    (QUESTION, SPECIAL),
    ('어선의 검사 절차는?', SPECIAL),
    ('일반선의 안전 기준은?', '범선의 구조 기준'),
    ('금액을 계산하는 방법은?', '산업안전보건법'),
    ('국민법 제1조', '민법'),
])
def test_particles_and_arbitrary_substrings_are_not_law_name_matches(question, name):
    assert law_name_score(question,name) == 0


def test_actual_specialized_subject_still_matches_its_law():
    assert law_name_score('범선의 최대승선인원 산정 기준은?',SPECIAL) > 0
    assert law_name_score('일반선의 여객인원 산정 기준은?',SPECIAL) == 0


def passenger_corpus(store):
    # 합성 본문은 주제별 역할만 나타냅니다. 여객인원/최대승선인원의 혼동과
    # '선의' 문자 겹침에 따른 이름 RRF 가산이 어떤 영향을 주는지 검증합니다.
    put(store, GENERAL, [
        ('8','객석면적의 산입제외장소','객석면적에서 제외할 장소를 정한다. 여객이 이용하는 객석의 면적을 확인한다.'),
        ('9','객석면적의 산정방법','여객의 객석면적 산정방법을 정한다. 객실과 통로의 면적을 구분한다.'),
        ('10','여객의 정원 등','여객의 정원은 객석면적과 객석설비를 고려하여 산정한다.'),
        ('12','객석설비의 종류 등','여객의 객석설비 종류를 구분한다. 여객 정원을 계산하는 데 객석설비를 확인한다.'),
        ('17','임시여객의 정원산정 장소 등','임시여객의 정원을 산정하는 장소에 관한 사항을 정한다.'),
    ])
    put(store,SPECIAL,[('25','최대승선인원의 산정 등',
                       '범선의 최대승선인원을 산정할 때 인원과 면적을 검토한다. 범선의 승선인원 산정 항목에 적용한다.')])
    # 경쟁 후보가 한 법령에서 다수 검색되는 실제 후보 선정 경계를 포함합니다.
    put(store,'범선의 검사 기준',[(str(i),'인원의 산정','범선의 인원을 산정하는 검사 항목을 검토한다.')
                                   for i in range(1,21)])
    pack = lambda cosine: sqlite_vec.serialize_float32([cosine,sqrt(1-cosine*cosine),0.])
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=?,title_vector=?', (pack(.55),pack(.55)))
        version = db.execute('SELECT version FROM law_versions WHERE name=?',(GENERAL,)).fetchone()[0]
        for position in range(5):
            # 의미 검색은 여객 관련 조문을 더 가깝게 찾는 조건입니다. 임의의
            # 이름 보너스 때문에 이 올바른 검색 신호가 뒤집히면 안 됩니다.
            db.execute('UPDATE version_articles SET vector=?, title_vector=? WHERE version=? AND position=?',
                       (pack(.8-position*.01),pack(.8-position*.01),version,position))


@pytest.mark.parametrize('variants',[[], ['일반선의 여객 정원 산정 기준']])
def test_passenger_articles_are_not_displaced_by_sailing_law_name_bonus(tmp_path, variants):
    store = Store(tmp_path)
    passenger_corpus(store)
    hits = store.search_enhanced(QUESTION,QueryProvider(),variants=variants,threshold=.3,fusion='rrf',limit=15)
    assert len(hits) <= 15
    general = [row for row in hits if row['law_name']==GENERAL]
    expected = {'제8조','제9조','제10조','제17조'}
    if variants:
        expected.add('제12조')
    assert expected <= {row['number'] for row in general}
    assert hits[0]['law_name'] == GENERAL
    assert all(row['law_name_score']==0 and row['rrf_field_ranks']['law_name'] is None
               for row in hits if '범선' in row['law_name'])


def test_body_and_title_match_nouns_not_parts_of_words_or_accidental_spacing():
    index = NounIndex(['범선의 운항', '선의 취득'],
                     ['범선의 운항을 점검 토대로 판단한다.', '선의 취득 요건을 검토한다.'])
    plan = index.plan('선의 취득을 검토하는가?')
    assert not {'선','선의'} & set(plan.matches(0,'title'))
    assert not {'선','선의'} & set(plan.matches(0,'body'))
    assert '검토' not in plan.matches(0,'body')
    assert '검토' in plan.matches(1,'body')
    assert {'선','선의'} & set(plan.matches(1,'body'))


def test_keyword_scores_have_no_bigram_fallback(tmp_path):
    store = Store(tmp_path)
    put(store,'합성 기준',[('1','전문학교','전문학교의 일반 운영에 관한 내용이다.')])
    trace = {}
    store.search_enhanced('전문가 등록',QueryProvider(),mode='keyword',threshold=.3,
                          fusion='rrf',diagnostics=trace)
    row = trace['candidates'][0]
    assert row['title_keyword_score'] == row['body_keyword_score'] == 0
    assert row['keyword_method'] == 'noun_idf'
    assert row['stage'] == 'below_threshold'


def test_sailing_question_can_still_retrieve_sailing_rule(tmp_path):
    store = Store(tmp_path)
    passenger_corpus(store)
    hits = store.search_enhanced('범선의 최대승선인원을 어떻게 산정하는가?',QueryProvider(),
                                 threshold=.3,fusion='rrf',limit=15)
    assert any(row['law_name']==SPECIAL and row['number']=='제25조' for row in hits)


def test_passenger_candidate_set_reaches_review_and_only_approved_scope_is_visible(tmp_path):
    class ReviewProvider(QueryProvider):
        def rewrite_question(self, question):
            return '일반선의 여객 정원 산정 기준'

        def filter_evidence(self, question, candidates):
            assert question == QUESTION
            assert len(candidates) <= 15
            assert {row['number'] for row in candidates if row['law_name']==GENERAL} == {
                '제9조','제10조','제17조'}
            assert all(row['score'] >= .85 for row in candidates)
            assert all(row['law_name'] and row['body'] for row in candidates)
            # 실제 GPT 판정의 정확도를 주장하는 테스트가 아닙니다. 검토기가
            # 적용 대상이 다른 후보를 거절했을 때 답변/출처로 우회 복귀하지
            # 않고 승인된 조문 전체를 전달하는지 확인합니다.
            return [row for row in candidates if row['law_name']==GENERAL]

        def answer(self, question, evidence, *args, **kwargs):
            assert {row['law_name'] for row in evidence} == {GENERAL}
            return '검토된 여객 정원 근거를 사용하는 합성 답변 [1]'

    app = create_app(Settings(data_dir=tmp_path),ReviewProvider(),object())
    passenger_corpus(app.state.store)
    with TestClient(app) as client:
        response = client.post('/api/chat',json={'question':QUESTION,'model':'gpt-4.1'})
    assert response.status_code == 200
    result = response.json()
    # 8·12조는 이 합성 자료에서 85% 미달입니다. 명사 일치만으로 우회시키지 않습니다.
    assert result['generated'] and len(result['evidence']) == 3
    assert all(row['relevance_checked'] and row['law_name']==GENERAL for row in result['evidence'])
    assert all(row['rrf_field_weights'] == {'body':.6,'title':.3,'law_name':.1} for row in result['evidence'])

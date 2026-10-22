"""사용자 제공 본문 + 경쟁 후보/벡터를 이용한 검색 회귀 검사.

아래 법령 발췌의 개정일을 건조 적용일이나 정답으로 사용하지 않습니다.
벡터와 경쟁 조문은 합성 자료이며 실제 운영 검색 품질 평가는 별도입니다.
"""
from math import sqrt

import pytest
import sqlite_vec

from app.keywords import query_terms
from app.config import Settings
from app.diagnose_search import diagnose
from app.chat_pipeline import run_chat
from app.main import ChatRequest
from app.store import Store
from test_enhanced_retrieval import put
from test_field_rrf import QueryProvider


DETECTOR_QUESTION = '총톤수 10톤 미만 소형어선의 단독경보형감지기는 몇 년도 이후 건조된 어선부터 설치해야 하는가?'
DETECTOR_LAW = '총톤수 10톤 미만 소형어선의 구조 및 설비기준'
ARTICLE_58 = '제58조(무인기관실의 소화장치) 총톤수 2톤 이상 어선의 무인기관실(기관실 외부에서 원격으로 조종되는 주기관을 설치한 어선으로서 기관 운전중 선원이 계속적으로 배치되지 않는 기관실을 말한다)에는 다음의 어느 하나에 해당하는 소방설비를 갖추어야 한다. 다만, 금속제의 선체인 경우에는 화재탐지장치 및 기관실 각 출입구마다 휴대식 분말소화기(ABC급) 또는 휴대식 탄산가스소화기를 2개 이상의 소방설비로 갈음할 수 있다.   1. 해당 기관실 용적에 충분한 용량의 무인기관실용 자동 소화장치 및 단독경보형감지기 <개정 2021. 10. 26.>   2. 화재탐지장치 및 고정식 소화장치'
ARTICLE_59 = '제59조(소화기의 비치종류 및 수량 등) ① 소형어선에는 다음 표에 따른 비치수량 이상의 소화기를 설치장소의 출입구 가까운 곳에 갖추어 두어야 하며, 항행 중 항상 양호한 상태로 유지되고 즉시 사용할 수 있도록 준비되어 있어야 한다. <개정 2021. 10. 26.> [이미지: 원문 확인 필요]    ② 총톤수 2톤 이상 어선의 거주제실, 선원실, 조타실에는 각 구획마다 1개 이상의 단독경보형감지기를 설치해야 한다. 이 경우 조리실 구획은 정온식의 감지기로 설치해야 한다.. <신설 2021. 10. 26.>   ③ 제2항에도 불구하고 화재탐지장치를 설치한 경우에는 그러하지 아니하다. <신설 2021. 10. 26.>'
ANCHOR_QUESTIONS = [
    '의장수별로 비치해야 하는 앵커의 크기와 수량은 어떻게 되는가?',
    '이 선박의 의장수가 〇〇인데, 비치해야 하는 닻의 종류·질량·수량은 어떻게 되는가?',
]
ARTICLE_57 = '제57조(강선 등의 의장수) ① 선박의 의장수(E)는 다음 각 호의 어느 하나에 의하여 산정한다.   1. 예인선을 제외한 선박은 다음 식에 의하여 산정한다. [이미지: 원문 확인 필요]    2. 예인선은 다음식에 의하여 산정한다. [이미지: 원문 확인 필요]    ② 제1항에도 불구하고 한국형닻을 비치하고자 하는 경우 또는 선박 길이 30미터(여객선으로서 고속선 및 공기부양선에 있어서는 40미터) 미만으로서 특수형닻을 비치하고자 하는 경우에 선박의 의장수는 다음 각 호의 어느 하나에 의하여 산정한다. <개정 2007. 11. 2.>   1. 강선, 특수재질로 건조된 선박 또는 이와 동등한 재질로 건조된 선박은 다음 식에 의하여 산정한다. [이미지: 원문 확인 필요]    2. 목선은 다음 식에 의하여 산정한다. [이미지: 원문 확인 필요]    ③ 제1항 및 제2항의 의장수 산정에 있어서 형배수량의 산정 기준 및 선루 또는 갑판실 등의 높이, 길이, 너비, 면적 측정방법은 별표 17(형배수량의 산정 기준 및 선루 또는 갑판실 등의 높이 및 너비측정방법 등(제57조제3항관련))에 의한다.   ④ 제1항부터 제3항까지의 의장수의 계산에 있어서 유효숫자의 처리는 다음 각 호에 의한다.   1. 길이ㆍ너비ㆍ높이 등: 소수점이하 3자리에서 반올림한다. [이미지: 원문 확인 필요]    3. 의장수: 소수점이하는 버린다.   [전문개정 2002. 1. 18.] 제58조 <삭제 2002. 01. 18>'


def crowded_corpus(store, scenario, target_similarity=.1):
    if scenario == 'detector':
        name = DETECTOR_LAW
        articles = [('58', '무인기관실의 소화장치', ARTICLE_58),
                    ('59', '소화기의 비치종류 및 수량 등', ARTICLE_59)]
        distractor = ('어선의 건조 및 설치', '소형어선의 건조 및 설치에 필요한 일반 검사 사항을 정한다.')
        target = '제58조'
    else:
        name = '선박설비기준'
        articles = [('57', '강선 등의 의장수', ARTICLE_57)]
        distractor = ('설비 비치 크기와 수량', '선박 설비를 비치하는 종류와 질량 및 크기와 수량에 관한 일반 사항이다.')
        target = '제57조'
    # 상위 15개/필드 50개보다 경쟁 문서가 많은 조건을 반드시 검증합니다.
    articles += [(str(100+i), *distractor) for i in range(80)]
    put(store, name, articles)
    def pack(similarity):
        return sqlite_vec.serialize_float32([similarity, sqrt(1-similarity**2), 0.])
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=?, title_vector=?', (pack(.6), pack(.6)))
        db.execute('UPDATE version_articles SET vector=?, title_vector=? WHERE number=?',
                   (pack(target_similarity), pack(.1), target))
    return target


def test_technical_compound_and_one_character_noun_are_preserved():
    for question in ANCHOR_QUESTIONS:
        nouns, compounds = query_terms(question)
        assert '의장수' in nouns | compounds
        assert '수' not in nouns
    assert '닻' in query_terms(ANCHOR_QUESTIONS[1])[0]
    assert not ({'법', '영', '수', '것'} & query_terms('법과 영의 수와 것은?')[0])


@pytest.mark.parametrize('similarity', [0., .1, .4])
def test_both_detector_articles_survive_crowded_rrf_pool(tmp_path, similarity):
    store = Store(tmp_path)
    crowded_corpus(store, 'detector', similarity)
    hits = store.search_enhanced(DETECTOR_QUESTION, QueryProvider(), threshold=.3, fusion='rrf', limit=15)
    assert len(hits) <= 15
    assert {'제58조', '제59조'} <= {row['number'] for row in hits}
    assert next(row for row in hits if row['number'] == '제58조')['body'] == ARTICLE_58
    hit = next(row for row in hits if row['number'] == '제58조')
    assert hit['retrieval_rank'] > 15  # 단순 RRF 절단으로는 놓쳤던 실제 경계 조건
    assert '단독경보형감지기' in hit['matched_focus_nouns']
    assert hit['admission_score'] >= .3


def test_exact_body_term_can_qualify_without_semantic_or_law_name_bonus(tmp_path):
    store = Store(tmp_path)
    crowded_corpus(store, 'detector', 0.)
    hits = store.search_enhanced('단독경보형감지기 설치 시점은?',QueryProvider(),
                                 threshold=.3,fusion='rrf',limit=15)
    hit = next(row for row in hits if row['number']=='제58조')
    assert hit['weighted_score'] < .3 <= hit['admission_score']
    assert hit['law_name_score'] == 0


@pytest.mark.parametrize('question', ANCHOR_QUESTIONS)
def test_equipment_number_article_survives_crowded_rrf_pool(tmp_path, question):
    store = Store(tmp_path)
    crowded_corpus(store, 'anchor')
    hits = store.search_enhanced(question, QueryProvider(), threshold=.3, fusion='rrf', limit=15)
    assert len(hits) <= 15
    hit = next(row for row in hits if row['number'] == '제57조')
    assert '의장수' in hit['matched_body_nouns']
    assert hit['body'] == ARTICLE_57


def test_protected_hits_still_require_llm_review_and_rejection_is_diagnosable(tmp_path):
    class ReviewProvider(QueryProvider):
        def filter_evidence(self, question, candidates):
            assert len(candidates) <= 15
            assert {'제58조', '제59조'} <= {row['number'] for row in candidates}
            return [row for row in candidates if row['number'] == '제59조']

        def answer(self, question, evidence, *args, **kwargs):
            assert all(row['number'] != '제58조' for row in evidence)
            return '검토된 근거만 사용한 합성 답변'

    store = Store(tmp_path)
    crowded_corpus(store, 'detector', 0.)
    report = diagnose(store,ReviewProvider(),Settings(data_dir=tmp_path),DETECTOR_QUESTION,
                      chat=True,law=DETECTOR_LAW,articles=['제58조', '제59조'],as_of='2026-10-22')
    assert report['searches'][0]['total_scored'] == 82
    assert report['searches'][0]['selected_count'] == 15
    outcomes = {row['number']:row['chat_stage'] for row in report['searches'][0]['candidates']}
    assert outcomes == {'제58조':'llm_rejected', '제59조':'visible_source'}
    assert len(report['inventory']) == 2
    assert all(row['active_at_cutoff'] for row in report['inventory'])
    assert all('body' not in row and 'vector' not in row for row in report['searches'][0]['candidates'])


def test_diagnostics_separate_missing_inactive_threshold_and_candidate_limit(tmp_path):
    store = Store(tmp_path)
    crowded_corpus(store, 'detector')
    put(store, '무관한 법률', [('1','주차장', '주차장 도로 시설')])
    pack = sqlite_vec.serialize_float32
    with store.connect() as db:
        db.execute("UPDATE version_articles SET vector=?, title_vector=? WHERE number='제1조'",
                   (pack([0.,1.,0.]), pack([0.,1.,0.])))
    report = diagnose(store,QueryProvider(),Settings(),DETECTOR_QUESTION)
    rows = report['searches'][0]['candidates']
    assert next(row for row in rows if row['number']=='제1조')['stage'] == 'below_threshold'
    assert any(row['stage']=='outside_candidate_limit' for row in rows)
    assert next(row for row in rows if row['number']=='제58조')['stage'] == 'selected'
    past = diagnose(store,QueryProvider(),Settings(),DETECTOR_QUESTION,as_of='2019-01-01')
    assert all(not row['active_at_cutoff'] for row in past['inventory'])
    assert not past['searches'][0]['candidates']
    missing = diagnose(store,QueryProvider(),Settings(),DETECTOR_QUESTION,law='미수집법')
    assert missing['inventory'] == [] and '자료가 없습니다' in missing['inventory_note']


def test_rewrite_does_not_reserve_invented_core_nouns(tmp_path):
    store = Store(tmp_path)
    put(store, '합성법', [('1', '일반', '주차장 등록'), ('2', '일반', '희귀탐지기 설치')])
    hits = store.search_enhanced('주차장 등록',QueryProvider(),variants=['희귀탐지기 설치'],
                                 mode='keyword',threshold=.3,fusion='rrf',limit=15)
    invented = next(row for row in hits if row['number']=='제2조')
    assert not invented['body_focus_score'] and not invented['matched_focus_nouns']


def test_law_name_or_title_only_never_qualifies_for_noun_reserve(tmp_path):
    store = Store(tmp_path)
    put(store, '단독경보형감지기 법', [('1', '단독경보형감지기', '주차장 등록 및 신고')])
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=?,title_vector=?',
                   (sqlite_vec.serialize_float32([0.,1.,0.]),sqlite_vec.serialize_float32([1.,0.,0.])))
    trace = {}
    hits = store.search_enhanced('단독경보형감지기 설치',QueryProvider(),
                                 threshold=.3,fusion='rrf',limit=15,diagnostics=trace)
    assert not hits
    assert trace['candidates'][0]['body_focus_score'] == 0


def test_protected_candidates_survive_additional_search_merge(tmp_path):
    class GapProvider(QueryProvider):
        def __init__(self):
            super().__init__()
            self.assessments = 0
            self.reviewed = []

        def filter_evidence(self, question, candidates):
            assert len(candidates) <= 15
            self.reviewed.extend(row['number'] for row in candidates)
            return candidates

        def assess_evidence(self, *args):
            self.assessments += 1
            return {'sufficient':False,'missing_issues':['건조 시점 적용례 미확보'],
                    'missing_facts':[], 'search_queries':['소형어선 건조 적용례 부칙']}

        def answer(self, question, evidence, *args, **kwargs):
            assert {'제58조','제59조'} <= {row['number'] for row in evidence}
            return '적용 시점은 미확인'

    store, provider = Store(tmp_path), GapProvider()
    crowded_corpus(store, 'detector')
    result = run_chat(store,provider,Settings(data_dir=tmp_path),ChatRequest(question=DETECTOR_QUESTION))
    assert result['additional_search_rounds'] == 1
    assert provider.reviewed.count('제58조') == provider.reviewed.count('제59조') == 1

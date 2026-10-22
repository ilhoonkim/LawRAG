"""Law-name recall on synthetic administrative rules, not actual installation dates."""
from math import sqrt
from xml.sax.saxutils import escape

import pytest
import sqlite_vec
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.retrieval import field_rrf, law_name_score
from app.store import Store
from test_field_rrf import observations, QueryProvider

QUESTION = '총톤수 10톤 미만 소형어선의 단독경보형감지기는 몇 년도 이후 건조된 어선부터 설치해야 하는가?'
SPECIFIC = '총톤수 10톤 미만 소형어선의 구조 및 설비기준'
GENERAL = '어선설비기준'


def put_rule(store, name, articles):
    nodes = ''.join(f'<조문내용>제{i}조({escape(title)}) {escape(body)}</조문내용>'
                    for i, (title, body) in enumerate(articles, 1))
    raw = (f'<행정규칙><행정규칙ID>{escape(name)}</행정규칙ID><행정규칙명>{escape(name)}</행정규칙명>'
           f'<시행일자>20200101</시행일자>{nodes}</행정규칙>').encode()
    return store.ingest(raw, QueryProvider(), kind='admrul', archive_only=True)['version']


def corpus(tmp_path):
    store = Store(tmp_path)
    # Artificial dates and vectors deliberately make the specific article miss
    # the old title/body threshold while the general equipment rule qualifies.
    specific = put_rule(store, SPECIFIC, [
        ('소방설비', '단독경보형감지기는 2030년 1월 1일 이후 건조되는 어선에 설치하여야 한다.'),
        *[('회계처리', '주차장 기금 납부 징수 대금 처리 방법.') for _ in range(60)],
    ])
    general = put_rule(store, GENERAL, [
        ('단독경보형감지기', '단독경보형감지기 설치 건조 어선 적용 기준은 별도로 정한다.'),
    ])
    wrong_scope = put_rule(store, SPECIFIC.replace('10톤', '20톤'), [
        ('소방설비', '단독경보형감지기는 2030년 1월 1일 이후 건조되는 어선에 설치하여야 한다.'),
    ])
    pack = sqlite_vec.serialize_float32
    with store.connect() as db:
        # All decoy title vectors match, but their bodies are unrelated. They
        # must not fill the RRF pool or reach the LLM just because of the name.
        db.execute('UPDATE version_articles SET vector=?,title_vector=?', (pack([0., 1., 0.]), pack([1., 0., 0.])))
        for version, cosine in [(specific, .4), (general, .8), (wrong_scope, .4)]:
            db.execute('UPDATE version_articles SET vector=?,title_vector=? WHERE version=? AND position=0',
                       (pack([cosine, sqrt(1-cosine*cosine), 0.]), pack([cosine, sqrt(1-cosine*cosine), 0.]), version))
    return store


def test_partial_name_is_continuous_and_number_boundaries_matter():
    score = law_name_score(QUESTION, SPECIFIC)
    assert 0 < score < 1
    assert score > law_name_score(QUESTION, GENERAL)
    assert score > law_name_score(QUESTION, SPECIFIC.replace('10톤', '20톤'))
    assert score > law_name_score(QUESTION, SPECIFIC.replace('미만', '이상'))
    assert law_name_score('총톤수 １０ 톤 미만 소형어선의 감지기 설치', SPECIFIC) == score
    assert law_name_score(SPECIFIC.replace(' ', '') + ' 제1조', SPECIFIC) == 1


@pytest.mark.parametrize('question', ['관련 기준과 규정은 무엇인가?', '기준 알려줘'])
def test_generic_words_do_not_create_name_votes(question):
    assert law_name_score(question, SPECIFIC) == 0


@pytest.mark.parametrize('question,name', [
    ('어선의 검사는 어떻게 받나요?', '어선법'),
    ('어선의 검사는 어떻게 받나요?', GENERAL),
    ('레저기구를 검사받으려면 어떻게 하나요?', '수상레저안전법'),
    ('레저기구를 검사받으려면 어떻게 하나요?', '수상레저기구의 등록 및 검사에 관한 법률'),
])
def test_short_domain_terms_also_score_law_names(question, name):
    assert 0 < law_name_score(question, name) < 1


@pytest.mark.parametrize('fusion', ['weighted', 'rrf'])
def test_partial_name_rescues_specific_rule_without_losing_general_rule(tmp_path, fusion):
    store, provider = corpus(tmp_path), QueryProvider()
    with store.connect() as db:
        vectors = list(map(tuple, db.execute('SELECT vector,title_vector FROM version_articles ORDER BY version,position')))
    results = store.search_enhanced(QUESTION, provider, threshold=.4, fusion=fusion)
    assert {r['law_name'] for r in results} == {SPECIFIC, GENERAL, SPECIFIC.replace('10톤', '20톤')}
    specific = next(r for r in results if r['law_name'] == SPECIFIC)
    # 문자 겹침만 쓰던 이전 점수는 임계치 미만입니다. 이제 명사 일치도 함께 반영합니다.
    old_quality = .8 * (.65*specific['body_semantic_score'] + .35*specific['body_ngram_score']) + .2*specific['title_ngram_score']
    assert old_quality < .4 <= specific['weighted_score']
    wrong = next(r for r in results if r['law_name'] == SPECIFIC.replace('10톤', '20톤'))
    assert wrong['weighted_score'] < specific['weighted_score']
    assert specific['law_name_score'] > 0 and specific['rrf_field_ranks']['law_name'] == 1
    assert specific['number'] == '제1조' and '2030년' in specific['body']
    assert '/documents/' in specific['html_url']
    general = next(r for r in results if r['law_name'] == GENERAL)
    assert specific['law_name_score'] > general['law_name_score'] > 0
    assert general['rrf_field_ranks']['law_name'] == 2
    assert provider.queries == [[QUESTION]]  # No new law-name embeddings/API calls.
    with store.connect() as db:
        assert vectors == list(map(tuple, db.execute('SELECT vector,title_vector FROM version_articles ORDER BY version,position')))


def test_name_rrf_ranks_laws_once_and_keeps_other_laws_despite_many_articles():
    scores = observations(law={**{f'a{i:03}': .9 for i in range(70)}, 'b': .8})
    for score in scores:
        score['row']['law_name'] = 'A 규칙' if score['row']['id'].startswith('a') else 'B 규칙'
    fused, ranks = field_rrf([scores], 'keyword')
    assert {ranks['law_name'][f'a{i:03}'] for i in range(70)} == {1}
    assert ranks['law_name']['b'] == 2 and fused['b'] == pytest.approx(1/62)
    assert field_rrf([scores, scores], 'keyword') == (fused, ranks)


def test_name_boost_comes_from_original_not_rewrite_and_does_not_force_an_article(tmp_path):
    store = corpus(tmp_path)
    result = store.search_enhanced('단독경보형감지기 설치', QueryProvider(), threshold=.2,
                                   variants=[SPECIFIC + ' 제1조'], fusion='rrf')
    assert result and all(r['law_name_score'] == 0 and not r['exact_identifier'] for r in result)


def test_keyword_mode_and_comparison_do_not_require_new_embeddings(tmp_path):
    store = corpus(tmp_path)
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=NULL,title_vector=NULL,model=NULL')
    provider = QueryProvider()
    result = store.search_enhanced(QUESTION, provider, mode='keyword', compare=True, threshold=.4)
    assert provider.queries == []
    assert SPECIFIC in {r['law_name'] for r in result['rrf']}
    assert {r['id'] for r in result['rrf']} == {r['id'] for r in result['weighted']}
    assert result['rrf_fields'] == ['law_name', 'title', 'body']
    for hit in result['rrf']:
        raw = sum(1/(60+rank) for rank in hit['rrf_field_ranks'].values() if rank)
        assert hit['rrf_raw_score'] == pytest.approx(raw)
        assert hit['rrf_max_score'] == pytest.approx(3/61)
        assert hit['score'] == pytest.approx(raw/(3/61), abs=1e-6)


def test_both_rules_reach_llm_review_and_sources(tmp_path):
    corpus(tmp_path)
    wrong_size = SPECIFIC.replace('10톤', '20톤')
    class ChatProvider(QueryProvider):
        def filter_evidence(self, question, candidates):
            # 고정 임계치 0.3에서는 약한 수치 불일치 후보도 검토 대상이 됩니다.
            assert {r['law_name'] for r in candidates} == {SPECIFIC, GENERAL, wrong_size}
            assert all(r['number'] == '제1조' for r in candidates)
            return [r for r in candidates if r['law_name'] != wrong_size]
        def answer(self, question, evidence, history, **kwargs):
            assert {r['law_name'] for r in evidence} == {SPECIFIC, GENERAL}
            return '합성 테스트 자료 답변 [1][2]'
    app = create_app(Settings(data_dir=tmp_path), ChatProvider(), object())
    with TestClient(app) as client:
        result = client.post('/api/chat', json={'question': QUESTION})
    assert result.status_code == 200
    assert result.json()['generated']
    assert {r['law_name'] for r in result.json()['evidence']} == {SPECIFIC, GENERAL}

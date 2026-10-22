"""Field fusion contracts with synthetic rankings/vectors, not legal quality tests."""
import pytest
import sqlite_vec

from app.laws import SetupError
from app.retrieval import field_rrf
from app.store import Store
from test_enhanced_retrieval import put
from test_workflow import FakeProvider


def observations(**fields):
    keys = sorted(set().union(*(set(values) for values in fields.values())))
    return [{'row': {'id': key, 'law_name': key}, **{field: fields.get(field, {}).get(key, 0.)
            for field in ('title', 'title_semantic', 'lexical', 'semantic', 'law')}} for key in keys]


def test_keyword_rrf_has_separate_title_and_body_ranks():
    # The 0.80 body / 0.20 title blend picks body-only; field RRF rewards the
    # article supported by both fields. These must not be the same ranking.
    scores = observations(title={'both': .6}, lexical={'body-only': 1., 'both': .7})
    fused, ranks = field_rrf([scores], 'keyword')
    assert ranks == {'title': {'both': 1}, 'body': {'body-only': 1, 'both': 2}, 'law_name': {}}
    assert fused['both'] == pytest.approx(.3/61 + .6/62)
    assert fused['body-only'] / (1/61) == pytest.approx(.6)
    assert fused['both'] > fused['body-only']


def test_hybrid_combines_retrievers_within_fields_before_combining_fields():
    scores = observations(title={'a': .9, 'b': .8}, title_semantic={'b': .9},
                          lexical={'c': .9, 'b': .8}, semantic={'b': .9})
    fused, ranks = field_rrf([scores], 'hybrid')
    assert ranks == {'title': {'b': 1, 'a': 2}, 'body': {'b': 1, 'c': 2}, 'law_name': {}}
    assert fused['b'] == pytest.approx(.9/61)
    assert fused['a'] == pytest.approx(.3/62)
    assert fused['c'] == pytest.approx(.6/62)


def test_rewrites_do_not_add_votes_at_final_field_level():
    original = observations(title={'a': .9}, lexical={'a': .9})
    rewrite = observations(title_semantic={'b': .8}, semantic={'b': .8})
    fused, ranks = field_rrf([original, rewrite], 'hybrid')
    assert set(fused) == {'a', 'b'}
    assert fused['a'] == pytest.approx(.9/61)
    assert fused['b'] == pytest.approx(.9/62)
    assert set(ranks) == {'law_name', 'title', 'body'}
    assert field_rrf([original, rewrite, original, rewrite], 'hybrid') == (fused, ranks)


def test_empty_field_contributes_zero_and_exact_lookup_does_not_invent_a_name_match():
    scores = observations(lexical={'body': .8})
    fused, ranks = field_rrf([scores], 'keyword')
    assert ranks['title'] == {} and fused['body'] / (1/61) == pytest.approx(.6)
    fused, ranks = field_rrf([scores], 'keyword', exact_ids=['exact'])
    assert ranks['title']['exact'] == ranks['body']['exact'] == 1
    assert fused['exact'] == pytest.approx(.9/61)
    assert field_rrf([], 'keyword') == ({}, {'title': {}, 'body': {}, 'law_name': {}})


def test_field_pool_is_bounded_deterministic_and_zero_scores_do_not_vote():
    scores = observations(title={f'{i:03}': 1. for i in range(70)}, lexical={'zero': 0.})
    fused, ranks = field_rrf([scores], 'keyword')
    assert len(fused) == len(ranks['title']) == 50
    assert ranks['title']['000'] == 1 and ranks['title']['049'] == 50
    assert 'zero' not in fused and not ranks['body']
    assert field_rrf([list(reversed(scores))], 'keyword') == (fused, ranks)


class QueryProvider(FakeProvider):
    def __init__(self):
        self.queries = []

    def embed(self, texts):
        self.queries.append(list(texts))
        return [[1., 0., 0.] for _ in texts]


def vector_fixture(tmp_path):
    store = Store(tmp_path)
    put(store, '합성법', [('1', '가나다', '계약 서류'),
                         ('2', '라마바', '계약 서류'),
                         ('3', '계약 서류', '주차 시설 구조')])
    pack = sqlite_vec.serialize_float32
    with store.connect() as db:
        # The second heading is semantically related with no literal overlap;
        # the third matches in title only and must fail the body gate.
        db.execute('UPDATE version_articles SET vector=?, title_vector=?',
                   (pack([1., 0., 0.]), pack([0., 1., 0.])))
        db.execute('UPDATE version_articles SET title_vector=? WHERE number IN (?,?)',
                   (pack([1., 0., 0.]), '제2조', '제3조'))
        db.execute('UPDATE version_articles SET vector=? WHERE number=?',
                   (pack([0., 1., 0.]), '제3조'))
    return store


def test_existing_title_vector_affects_rrf_without_reembedding_or_body_gate_bypass(tmp_path):
    store, provider = vector_fixture(tmp_path), QueryProvider()
    result = store.search_enhanced('계약 서류', provider, variants=['필요 서류'], compare=True)
    assert provider.queries == [['계약 서류', '필요 서류']]
    assert result['rrf_strategy'] == 'field_hierarchical'
    assert result['rrf_fields'] == ['law_name', 'title', 'body']
    assert result['rrf_max_score'] == pytest.approx(1/61)
    assert result['rrf_field_weights'] == {'body':.6,'title':.3,'law_name':.1}
    assert {e['number'] for e in result['rrf']} == {'제1조', '제2조'}
    assert result['rrf'][0]['number'] == '제2조'
    first = result['rrf'][0]
    assert first['title_keyword_score'] == 0 and first['title_semantic_score'] == 1
    ranks = first['rrf_field_ranks']
    expected = sum(result['rrf_field_weights'][field]/(60+rank) for field,rank in ranks.items() if rank)
    assert first['rrf_raw_score'] == pytest.approx(expected)
    assert first['score'] == pytest.approx(expected/(1/61), abs=1e-6)
    assert {e['id'] for e in result['weighted']} == {e['id'] for e in result['rrf']}
    assert result['weighted'][0]['weighted_score'] == result['weighted'][1]['weighted_score']


def test_keyword_mode_needs_no_vectors_and_reports_three_fields(tmp_path):
    store, provider = vector_fixture(tmp_path), QueryProvider()
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=NULL, title_vector=NULL')
    result = store.search_enhanced('계약 서류', provider, mode='keyword', compare=True)
    assert provider.queries == []
    assert result['rrf'] and result['rrf_fields'] == ['law_name', 'title', 'body']
    assert all(e['number'] != '제3조' for e in result['rrf'])


@pytest.mark.parametrize('vector', [None, sqlite_vec.serialize_float32([1., 0.])])
def test_missing_or_wrong_dimension_title_vector_cannot_silently_change_fusion(tmp_path, vector):
    store = vector_fixture(tmp_path)
    with store.connect() as db:
        db.execute('UPDATE version_articles SET title_vector=? WHERE number=?', (vector, '제1조'))
    with pytest.raises(SetupError, match='재임베딩'):
        store.search_enhanced('계약 서류', QueryProvider(), fusion='rrf')


def test_explicit_identifier_and_duplicate_rewrite_keep_three_field_normalization(tmp_path):
    store = Store(tmp_path)
    put(store, '합성법', [('1', '일반', '다른 내용')])
    result = store.search_enhanced('합성법 제1조', QueryProvider(), mode='keyword',
                                   variants=['합성법 제1조'], compare=True)
    assert result['queries'] == ['합성법 제1조']
    hit = result['rrf'][0]
    assert hit['exact_identifier'] and hit['score'] == 1
    assert hit['rrf_field_ranks'] == {'law_name': 1, 'title': 1, 'body': 1}
    assert hit['rrf_raw_score'] == pytest.approx(1/61)


def test_empty_compare_preserves_score_metadata(tmp_path):
    result = Store(tmp_path).search_enhanced('계약 서류', QueryProvider(), compare=True)
    assert not result['weighted'] and not result['rrf']
    assert result['rrf_fields'] == ['law_name', 'title', 'body']
    assert result['rrf_max_score'] == pytest.approx(1/61)


def test_law_name_has_only_a_tenth_of_the_normalized_maximum():
    scores = observations(law={'name-only':1.}, lexical={'body-only':1.},title={'title-only':1.})
    fused, _ = field_rrf([scores],'keyword')
    assert fused['body-only']/(1/61) == pytest.approx(.6)
    assert fused['title-only']/(1/61) == pytest.approx(.3)
    assert fused['name-only']/(1/61) == pytest.approx(.1)

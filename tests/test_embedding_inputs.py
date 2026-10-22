"""Blank/moved articles and oversized UTF-8 inputs must not cause API 400s."""
import json
import math

import httpx
import pytest
import sqlite_vec

from app.config import Settings
from app.embedding_inputs import BATCH_SIZE, MAX_INPUT_BYTES, split_text
from app.provider import Provider
from app.store import Store
from app.upgrade_models import reindex
from test_enhanced_retrieval import xml
from test_workflow import FakeProvider


def sparse_xml():
    # Reproduce the uploaded rule's moved articles: XML contains only the old number.
    raw = xml('합성서식법', [('1', '서식 제출', '신청인은 별지 제1호서식의 신청서를 제출한다.'),
                         ('35', '', '제35조'), ('37', '', '제37조'),
                         ('57', '', '제57조'), ('60', '', '제60조'), ('61', '', '제61조'),
                         ('62', '검사', '선박 검사 기준을 따른다.')], annex='''
        <별표><별표단위><별표구분>서식</별표구분><별표번호>1</별표번호>
        <별표제목>신청서</별표제목><별표내용>FORM_PAYLOAD</별표내용></별표단위>
        <별표단위><별표구분>별표</별표구분><별표번호>1</별표번호>
        <별표제목>기준</별표제목><별표내용>ANNEX_PAYLOAD</별표내용></별표단위></별표>''')
    return raw


class NonemptyProvider(FakeProvider):
    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        assert all(t.strip() for t in texts)
        assert all('FORM_PAYLOAD' not in t and 'ANNEX_PAYLOAD' not in t for t in texts)
        self.inputs.extend(texts)
        return [[float(i+1), 1.] for i in range(len(texts))]


def test_moved_articles_and_form_payloads_are_not_embedded_or_searched(tmp_path):
    store, provider = Store(tmp_path), NonemptyProvider()
    result = store.ingest(sparse_xml(), provider)
    assert result['articles'] == 7 and len(provider.inputs) == 4
    assert '별지 제1호서식' in provider.inputs[0]  # Keep the actual legal requirement about a form.
    with store.connect() as db:
        rows = list(db.execute('SELECT * FROM version_articles ORDER BY position'))
        assert all(r['vector'] is None and r['title_vector'] is None for r in rows[1:6])
        assert rows[1]['body'] == '제35조'  # Original XML/HTML article stays available.
        assert rows[-1]['vector'] == sqlite_vec.serialize_float32([2., 1.])
        assert rows[-1]['title_vector'] == sqlite_vec.serialize_float32([4., 1.])
    assert store.list_laws()[0]['embedded_count'] == 2
    assert '제35조' in (tmp_path/'html'/f'{result["version"]}.html').read_text()
    for search in (store.search, store.search_enhanced):
        hits = search('서식 제출', provider, threshold=.1, mode='hybrid')
        assert hits and {h['number'] for h in hits} <= {'제1조', '제62조'}


def test_reindex_skips_empty_articles_and_is_resumable(tmp_path):
    store, provider = Store(tmp_path), NonemptyProvider()
    store.ingest(sparse_xml(), provider, keyword_only=True)
    report = reindex(store, provider, tmp_path/'report.json', notify=lambda _: None)
    assert report['completed'] == 1 and report['failed'] == 0 and len(provider.inputs) == 4
    assert reindex(store, provider, tmp_path/'report.json', notify=lambda _: None)['skipped'] == 1
    assert len(provider.inputs) == 4


@pytest.mark.parametrize('text', ['한글😀e\u0301' * 5000, '앞문단\n' * 4000, ' ' * 8500 + '끝', '짧은 본문'])
def test_split_keeps_all_characters_and_unicode_boundaries(text):
    chunks = split_text(text)
    assert ''.join(chunks) == text
    assert all(0 < len(chunk.encode('utf-8')) <= MAX_INPUT_BYTES for chunk in chunks)


@pytest.mark.parametrize('text', ['', ' \n\t ', None])
def test_empty_inputs_fail_locally_before_any_paid_request(text, monkeypatch):
    provider = Provider(Settings(api_key='test'))
    monkeypatch.setattr(provider, 'post', lambda *args: pytest.fail('Invalid input reached API'))
    with pytest.raises(ValueError, match='2번째가 비어'):
        provider.embed(['정상 본문', text])


def test_long_inputs_are_fully_embedded_and_return_one_vector_per_article(monkeypatch):
    calls = []
    real_client = httpx.Client

    def handle(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        assert len(batch) <= BATCH_SIZE
        assert all(0 < len(t.encode('utf-8')) <= MAX_INPUT_BYTES for t in batch)
        # API order is intentionally reversed. The last segment must affect the merged vector.
        data = [{'index': i, 'embedding': [0., 1.] if '마지막단서' in t else [1., 0.]} for i, t in enumerate(batch)]
        return httpx.Response(200, json={'data': data[::-1]})

    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    text = '가나다라마' * 6000 + '마지막단서'
    inputs = [text, *['짧은 제목']*17]
    vectors = Provider(Settings(api_key='test')).embed(inputs)
    assert len(vectors) == len(inputs) and vectors[1:] == [[1., 0.]]*17
    assert vectors[0][1] > 0 and math.hypot(*vectors[0]) == pytest.approx(1.)
    sent = [t for batch in calls for t in batch]
    assert ''.join(sent[:len(split_text(text))]) == text
    assert len(calls) > 1

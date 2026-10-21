"""Discovery metadata must improve recall without becoming legal evidence."""
from dataclasses import replace
from datetime import date
import errno
import json
from pathlib import Path
import sqlite3
import pytest
import sqlite_vec

from app.config import Settings
from app import enrich_metadata
from app.enrich_metadata import backfill, status
from app.laws import SetupError
from app.main import ChatRequest
from app.chat_pipeline import run_chat
from app.management import Management
from app.provider import Provider
from app.search_metadata import MetadataGenerationError, generate, prepare
from app.store import Store
from test_enhanced_retrieval import xml
from test_workflow import FakeProvider

QUESTION = '전세 사기당했을 때 일부 보호가 될까요'
BODY = '임차인은 보증금 중 일정액을 다른 담보물권자보다 우선하여 변제받을 권리가 있다. 다만, 요건을 갖추어야 한다.'
SUMMARY = '보증금의 일부 우선변제 권리와 적용 요건을 정한다.'
QUESTIONS = [QUESTION, '집주인에게 맡긴 돈을 못 받으면 어떤 보호가 있나요?']
RAW = xml('합성 임대차법', [('8','보증금 중 일정액의 보호',BODY)])


class MetadataProvider(FakeProvider):
    settings = Settings(api_key='synthetic-key', metadata_model='gpt-4o-mini', embedding_model='synthetic-vectors')

    def __init__(self):
        self.generations, self.embeddings, self.events = [], [], []
        self.fail_generation = self.fail_embedding = False

    def generate_search_metadata(self, documents):
        self.generations.append(documents)
        self.events.append('generate')
        if self.fail_generation:
            raise SetupError('합성 생성 실패')
        return [{'summary':SUMMARY,'questions':QUESTIONS} for row in documents]

    def embed(self, texts):
        self.embeddings.append(texts)
        self.events.append('embed')
        if self.fail_embedding:
            raise SetupError('합성 임베딩 실패')
        # Original legal terms are orthogonal to colloquial questions. Only the
        # added question vectors bridge the gap in this controlled example.
        return [[1.,0.,0.] if text in QUESTIONS else [0.,1.,0.] for text in texts]

    def rewrite_question(self, question):
        return question

    def filter_evidence(self, question, candidates):
        self.reviewed = candidates
        return candidates

    def answer(self, question, evidence, history, **kwargs):
        self.answered = evidence
        assert evidence[0]['body'] == BODY
        return '조건에 맞는 경우 보호될 수 있습니다. [1]'


def test_ingest_generates_before_embedding_and_reuses_cache_across_versions(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    first = store.ingest(RAW,provider,archive_only=True)
    assert provider.events[0] == 'generate'
    assert first['metadata_articles'] == 1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM article_metadata').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM search_metadata_vectors').fetchone()[0] == 3
        saved = db.execute('SELECT * FROM search_metadata').fetchone()
        assert json.loads(saved['questions']) == QUESTIONS
        assert saved['generator_model'] == 'gpt-4o-mini'
    second = store.ingest(RAW.replace(b'20200101',b'20210101'),provider,archive_only=True)
    assert first['version'] != second['version']
    assert len(provider.generations) == 1  # identical source text in a new revision
    assert len(provider.embeddings) == 3  # one metadata batch; two original batches
    store.ingest(RAW,provider,archive_only=True)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM article_metadata').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM search_metadata').fetchone()[0] == 1


@pytest.mark.parametrize('mode',['keyword','hybrid'])
@pytest.mark.parametrize('fusion',['weighted','rrf'])
def test_colloquial_metadata_match_recovers_original_parent_with_low_body_score(tmp_path, mode, fusion):
    store, provider = Store(tmp_path), MetadataProvider()
    # Legacy records still work, but this question cannot find this fixture.
    # A legacy provider simulates an existing un-enriched DB.
    store.ingest(RAW,FakeProvider(),archive_only=True)
    with store.connect() as db:
        db.execute('UPDATE version_articles SET vector=?,title_vector=?',
                   (sqlite_vec.serialize_float32([0.,1.,0.]),sqlite_vec.serialize_float32([0.,1.,0.])))
    assert store.search_enhanced(QUESTION,provider,mode=mode,fusion=fusion) == []
    store.ingest(RAW,provider,archive_only=True)
    hits = store.search_enhanced(QUESTION,provider,mode=mode,fusion=fusion)
    assert len(hits) == 1 and hits[0]['body'] == BODY
    hit = hits[0]
    assert hit['body_score'] < .20 and hit['retrieval_route'] == 'metadata'
    assert hit['metadata_score'] >= .48
    assert set(hit['rrf_field_ranks']) == {'title','body','summary','questions'}
    assert hit['rrf_max_score'] == pytest.approx(4/61)
    assert hit['matched_metadata']['field'] == 'questions'
    assert 'vector' not in json.dumps(hit)
    assert store.search_enhanced('초콜릿 케이크 조리법',provider,mode='keyword') == []
    assert all(r['weighted_score'] >= 1 for r in store.search_enhanced(QUESTION,provider,mode=mode,threshold=1))


def test_questions_do_not_multiply_parent_votes_and_other_version_metadata_is_not_used(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    old = store.ingest(RAW,provider,archive_only=True)
    first = store.search_enhanced(QUESTION,provider,mode='keyword',fusion='rrf')[0]
    with store.connect() as db:
        # Simulate a legacy cache with duplicate questions; scoring uses max,
        # not sum or one vote per generated question.
        db.execute('UPDATE search_metadata SET questions=?',(json.dumps([QUESTION]*5),))
    second = store.search_enhanced(QUESTION,provider,mode='keyword',fusion='rrf')[0]
    assert first['score'] == second['score']
    assert first['questions_score'] == second['questions_score']
    store.ingest(xml('합성 임대차법',[('8','일반','주차장 건축 구조')],effective='20250101'),FakeProvider(),archive_only=True)
    assert store.search_enhanced(QUESTION,provider,mode='keyword') == []
    historical = store.search_enhanced(QUESTION,provider,mode='keyword',cutoff=date(2021,1,1))
    assert historical[0]['version'] == old['version']


def test_backfill_resumes_after_failure_preserves_originals_and_reuses_paid_generation(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    law = store.ingest(RAW,FakeProvider(),archive_only=True)
    with store.connect() as db:
        before = dict(db.execute('SELECT * FROM version_articles').fetchone())
    path = tmp_path/'metadata-report.json'
    provider.fail_embedding = True
    failed = backfill(store,provider,path,notify=lambda _:None)
    assert failed['failed'] == 1 and status(store)['failed'] == 1
    with store.connect() as db:
        assert dict(db.execute('SELECT * FROM version_articles').fetchone()) == before
        assert db.execute('SELECT count(*) FROM search_metadata').fetchone()[0] == 1
        assert db.execute('SELECT metadata_key FROM article_metadata').fetchone()[0] is None
    provider.fail_embedding = False
    done = backfill(store,provider,path,notify=lambda _:None)
    assert done['completed'] == 1 and not done['failed']
    assert len(provider.generations) == 1
    calls = len(provider.embeddings)
    repeated = backfill(store,provider,path,notify=lambda _:None)
    assert repeated['skipped'] == 1 and len(provider.embeddings) == calls
    assert json.loads(path.read_text()) == repeated
    assert status(store) == {'total':1,'completed':1,'failed':0,'pending':0}
    manager = Management(store,provider,object())
    metadata = manager.articles(law['version'],1)['items'][0]['search_metadata']
    assert metadata['questions'] == QUESTIONS and metadata['summary'] == SUMMARY


def test_source_changes_invalidate_generation_and_new_embedding_model_reuses_text(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,provider,archive_only=True)
    provider.fingerprint = 'another-embedding-model'
    backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert len(provider.generations) == 1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM search_metadata_vectors').fetchone()[0] == 6
        db.execute("UPDATE version_articles SET body='변경된 원문 내용'")
    assert store.search_enhanced(QUESTION,provider,mode='keyword') == []
    backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert len(provider.generations) == 2
    provider.settings = replace(provider.settings,metadata_model='gpt-4.1-mini')
    backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert len(provider.generations) == 3


@pytest.mark.parametrize('incompatible',['dimension','model'])
def test_incompatible_metadata_vectors_are_not_compared_but_keyword_text_remains(tmp_path,incompatible):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,provider,archive_only=True)
    with store.connect() as db:
        if incompatible == 'dimension':
            db.execute('UPDATE search_metadata_vectors SET vector=?',(sqlite_vec.serialize_float32([1.,0.]),))
        else:
            db.execute("UPDATE search_metadata_vectors SET model='another-model'")
    hit = store.search_enhanced(QUESTION,provider)[0]
    assert hit['metadata_field_scores']['questions_semantic'] == 0
    assert hit['metadata_field_scores']['questions_keyword'] == 1
    assert hit['body'] == BODY


def test_failed_metadata_refresh_keeps_previous_usable_binding(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,provider,archive_only=True)
    provider.settings = replace(provider.settings,metadata_model='different-generator')
    provider.fail_generation = True
    report = backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert report['failed'] == 1
    hit = store.search_enhanced(QUESTION,provider)[0]
    assert hit['body'] == BODY and hit['retrieval_route'] == 'metadata'


def test_enrichment_generation_and_embedding_are_batched(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    rows = [(str(i),f'제목 {i}',f'{BODY} 조문 {i}') for i in range(1,12)]
    store.ingest(xml('합성법',rows),FakeProvider(),archive_only=True)
    result = backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert result['completed'] == 11
    assert [len(batch) for batch in provider.generations] == [5,5,1]
    assert [len(batch) for batch in provider.embeddings] == [15,15,3]
    assert all(BODY in doc['body'] for batch in provider.generations for doc in batch)


def test_failed_new_collection_does_not_replace_old_articles(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,FakeProvider(),archive_only=True)
    provider.fail_generation = True
    with pytest.raises(SetupError):
        store.ingest(RAW,provider,archive_only=True)
    with store.connect() as db:
        assert db.execute('SELECT body FROM version_articles').fetchone()[0] == BODY
        assert db.execute('SELECT count(*) FROM article_metadata').fetchone()[0] == 0


def test_metadata_candidates_are_reviewed_and_rejection_removes_actual_sources(tmp_path):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,provider,archive_only=True)
    result = run_chat(store,provider,Settings(),ChatRequest(question=QUESTION,mode='keyword'))
    assert result['generated'] and provider.reviewed[0]['body'] == BODY
    assert result['evidence'][0]['number'] == '제8조'
    provider.filter_evidence = lambda *args: []
    rejected = run_chat(store,provider,Settings(),ChatRequest(question=QUESTION,mode='keyword'))
    assert not rejected['generated'] and not rejected['evidence']


def response(items, reason='stop'):
    return {'choices':[{'finish_reason':reason,'message':{'content':json.dumps({'articles':items},ensure_ascii=False)}}]}


def test_generation_model_is_independent_and_id_order_dedup_are_validated(monkeypatch):
    provider = Provider(Settings(api_key='synthetic-key',chat_model='gpt-5.4',metadata_model='gpt-4o-mini')).for_model('gpt-4.1')
    docs = [{'law_name':'합성법','number':f'제{i}조','title':'제목','body':BODY} for i in (1,2)]
    calls = []
    def post(path, request):
        calls.append(request)
        return response([{'id':2,'summary':'둘째 조문 요약','questions':['일상 질문?','일상 질문?']},
                         {'id':1,'summary':'첫째 조문 요약','questions':['다른 질문?']}])
    monkeypatch.setattr(provider,'post',post)
    result = generate(provider,docs)
    assert calls[0]['model'] == 'gpt-4o-mini'
    assert calls[0]['response_format']['type'] == 'json_schema'
    assert result[0]['summary'] == '첫째 조문 요약' and result[1]['questions'] == ['일상 질문?']
    assert json.loads(calls[0]['messages'][-1]['content'])['articles'][0]['body'] == BODY


@pytest.mark.parametrize('items',[
    [], [{'id':True,'summary':'요약','questions':[]}],
    [{'id':1,'summary':'','questions':[]}],
    [{'id':1,'summary':'요약','questions':'문자열'}],
    [{'id':1,'summary':'요약','questions':[QUESTION]*6}],
    [{'id':1,'summary':'요약','questions':[None]}],
])
def test_malformed_generation_retries_once_without_persisting_bad_text(tmp_path,monkeypatch,items):
    provider = Provider(Settings(api_key='synthetic-key'))
    calls = []
    def post(*args):
        calls.append(args)
        return response(items)
    monkeypatch.setattr(provider,'post',post)
    store = Store(tmp_path)
    with pytest.raises(SetupError,match='메타데이터 생성 실패'):
        prepare(store,provider,[{'law_name':'합성법','number':'제1조','title':'제목','body':BODY}])
    assert len(calls) == 2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM search_metadata').fetchone()[0] == 0


def test_generated_search_text_never_enters_actual_verification_or_answer_prompt(monkeypatch):
    provider = Provider(Settings(api_key='synthetic-key',chat_model='gpt-4.1'))
    candidate = {'law_name':'합성법','number':'제8조','title':'보호','body':BODY,'effective':'20200101',
                 'matched_metadata':{'field':'questions','text':'GENERATED_ONLY_MARKER'},'metadata_score':.9}
    prompts = []
    def post(path, request):
        prompts.append(request)
        if len(prompts)==1:
            return {'choices':[{'message':{'content':json.dumps({'decisions':[
                {'id':1,'relevant':True,'role':'direct','conditions':[]}]})}}]}
        return {'choices':[{'message':{'content':'원문 근거 답변 [1]'}}]}
    monkeypatch.setattr(provider,'post',post)
    approved = provider.filter_evidence(QUESTION,[candidate])
    provider.answer(QUESTION,approved,[])
    assert len(prompts) == 2
    assert all(BODY in json.dumps(p,ensure_ascii=False) for p in prompts)
    assert all('GENERATED_ONLY_MARKER' not in json.dumps(p) for p in prompts)


def documents(count):
    return [{'law_name':'합성법','number':f'제{i}조','title':f'제목 {i}','body':BODY+str(i)}
            for i in range(1,count+1)]


@pytest.mark.parametrize('broken',['one_item','truncated'])
def test_generation_retries_only_unvalidated_ids_in_smaller_batches(monkeypatch,broken):
    provider, calls = Provider(Settings(api_key='synthetic-key')), []
    def post(path,request):
        payload = json.loads(request['messages'][-1]['content'])
        calls.append(payload)
        items = [{'id':doc['id'],'summary':doc['number'],'questions':QUESTIONS} for doc in payload['articles']]
        if len(calls) == 1:
            if broken == 'truncated':
                return response([],reason='length')
            items[2]['summary'] = '가'*1801
        return response(items)
    monkeypatch.setattr(provider,'post',post)
    result = generate(provider,documents(5))
    ids = [[doc['id'] for doc in call['articles']] for call in calls]
    assert ids == ([[1,2,3,4,5],[3]] if broken == 'one_item' else [[1,2,3,4,5],[1,2],[3,4],[5]])
    assert [item['summary'] for item in result] == [f'제{i}조' for i in range(1,6)]
    assert 'retry_requirements' in calls[1]
    assert all(doc['body'] == documents(5)[doc['id']-1]['body'] for call in calls for doc in call['articles'])


@pytest.mark.parametrize('answer,reason',[
    (response([],reason='length'),'출력 토큰 한도'),
    (response([{'id':1,'summary':'가'*1801,'questions':[]}]),'요약 내용 또는 길이'),
    (response([{'id':1,'summary':'요약','questions':['가'*201]}]),'예상 질문 내용 또는 개수'),
    (response([]),'조문 id가 누락'),
    (response([{'id':1,'summary':'요약','questions':[]}]*2),'조문 id가 중복'),
    ({'choices':[{'message':{'content':'invalid-json SECRET_MARKER'}}]},'응답 JSON'),
])
def test_metadata_failure_reports_safe_specific_reason(monkeypatch,answer,reason):
    provider = Provider(Settings(api_key='synthetic-key'))
    monkeypatch.setattr(provider,'post',lambda *args:answer)
    with pytest.raises(MetadataGenerationError,match=reason) as failure:
        generate(provider,documents(1))
    assert 'SECRET_MARKER' not in str(failure.value)


@pytest.mark.parametrize('api_retry_failure',[False,True])
def test_partial_batch_successes_are_indexed_and_only_failed_article_is_retried(tmp_path,monkeypatch,api_retry_failure):
    store, provider, calls = Store(tmp_path), MetadataProvider(), []
    raw = xml('합성법',[(str(i),f'제목 {i}',BODY+str(i)) for i in range(1,6)])
    store.ingest(raw,FakeProvider(),archive_only=True)
    with store.connect() as db:
        before = [dict(row) for row in db.execute('SELECT * FROM version_articles')]
    broken = True
    def post(path,request):
        docs = json.loads(request['messages'][-1]['content'])['articles']
        calls.append([doc['number'] for doc in docs])
        if api_retry_failure and len(calls) == 2:
            raise SetupError('AI API 요청 실패 [시간 초과]')
        return response([{'id':doc['id'],'summary':'' if broken and doc['number']=='제3조' else SUMMARY,
                          'questions':QUESTIONS} for doc in docs])
    monkeypatch.setattr(provider,'post',post,raising=False)
    monkeypatch.setattr(provider,'generate_search_metadata',lambda docs:generate(provider,docs))
    report = backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert (report['completed'],report['failed']) == (4,1)
    assert status(store) == {'total':5,'completed':4,'failed':1,'pending':0}
    assert calls == [[f'제{i}조' for i in range(1,6)],['제3조']]
    with store.connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM version_articles')] == before
        assert db.execute('SELECT count(*) FROM search_metadata').fetchone()[0] == 4
        assert db.execute('SELECT count(*) FROM search_metadata_vectors').fetchone()[0] == 12
    broken = False
    resumed = backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert (resumed['completed'],resumed['skipped'],resumed['failed']) == (1,4,0)
    assert len(calls) == 3 and calls[-1] == ['제3조']
    assert status(store)['completed'] == 5


def test_report_windows_sharing_violation_retries_and_preserves_old_export(tmp_path,monkeypatch):
    path = tmp_path/'report.json'
    path.write_text('previous report')
    replace, attempts = Path.replace, []
    def locked_then_ready(temporary,destination):
        attempts.append(temporary)
        if len(attempts) < 3:
            assert path.read_text() == 'previous report'
            error = PermissionError(errno.EACCES,'SECRET_MARKER')
            error.winerror = 32
            raise error
        return replace(temporary,destination)
    monkeypatch.setattr(Path,'replace',locked_then_ready)
    monkeypatch.setattr(enrich_metadata.time,'sleep',lambda _:None)
    enrich_metadata.write_report(path,{'completed':5550})
    assert json.loads(path.read_text()) == {'completed':5550}
    assert len(attempts) == 3 and not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('recover_during_run',[False,True])
def test_report_failure_does_not_abort_db_work_or_cause_repeat_api_calls(tmp_path,monkeypatch,recover_during_run):
    store, provider, notices = Store(tmp_path), MetadataProvider(), []
    store.ingest(xml('합성법',[(str(i),f'제목 {i}',BODY+str(i)) for i in range(1,12)]),FakeProvider(),archive_only=True)
    path = tmp_path/'report.json'
    path.write_text('previous report')
    writer, calls = enrich_metadata.write_report, []
    def fail_report(path,report):
        calls.append(len(report['items']))
        if not recover_during_run or len(calls)==1:
            error = PermissionError(errno.EACCES,'SECRET_MARKER')
            error.winerror = 32
            raise error
        writer(path,report)
    monkeypatch.setattr(enrich_metadata,'write_report',fail_report)
    result = backfill(store,provider,path,notify=notices.append)
    assert result['completed'] == 11 and status(store)['completed'] == 11
    warnings = [notice for notice in notices if '보고서 저장 경고' in notice]
    assert len(warnings) == 1 and 'winerror=32' in warnings[0] and 'SECRET_MARKER' not in warnings[0]
    if recover_during_run:
        assert 'report_error' not in result and json.loads(path.read_text()) == result
    else:
        assert 'report_error' in result and path.read_text() == 'previous report'
    api_counts = (len(provider.generations),len(provider.embeddings))
    monkeypatch.setattr(enrich_metadata,'write_report',writer)
    resumed = backfill(store,provider,path,notify=lambda _:None)
    assert resumed['skipped'] == 11 and not resumed['failed']
    assert (len(provider.generations),len(provider.embeddings)) == api_counts
    assert json.loads(path.read_text()) == resumed


def test_db_commit_failure_stops_new_api_work_and_resume_reuses_cache(tmp_path,monkeypatch):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(xml('합성법',[(str(i),f'제목 {i}',BODY+str(i)) for i in range(1,12)]),FakeProvider(),archive_only=True)
    bind = enrich_metadata.bind
    def fail_bind(*args):
        raise sqlite3.OperationalError('synthetic disk full')
    monkeypatch.setattr(enrich_metadata,'bind',fail_bind)
    path = tmp_path/'report.json'
    with pytest.raises(sqlite3.OperationalError):
        backfill(store,provider,path,notify=lambda _:None)
    assert len(provider.generations) == len(provider.embeddings) == 1
    assert status(store) == {'total':11,'completed':0,'failed':0,'pending':11}
    report = json.loads(path.read_text())
    assert report['completed'] == 0 and 'interrupted_at' in report
    monkeypatch.setattr(enrich_metadata,'bind',bind)
    resumed = backfill(store,provider,path,notify=lambda _:None)
    assert resumed['completed'] == 11
    assert [len(batch) for batch in provider.generations] == [5,5,1]
    assert len(provider.embeddings) == 3


def test_keyboard_interrupt_after_generation_resumes_without_regenerating(tmp_path,monkeypatch):
    store, provider = Store(tmp_path), MetadataProvider()
    store.ingest(RAW,FakeProvider(),archive_only=True)
    embed = provider.embed
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(provider,'embed',interrupt)
    with pytest.raises(KeyboardInterrupt):
        backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    monkeypatch.setattr(provider,'embed',embed)
    result = backfill(store,provider,tmp_path/'report.json',notify=lambda _:None)
    assert result['completed'] == 1 and len(provider.generations) == 1

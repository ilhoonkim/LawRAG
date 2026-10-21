from datetime import date
import json
from threading import Barrier, Lock

import pytest
from app.chat_pipeline import review, review_limit, run_chat
from app.config import Settings
from app.main import ChatRequest
from app.provider import Provider
from app.laws import SetupError
from test_chat_pipeline import row
from test_workflow import FakeProvider


class Store:
    def __init__(self):
        self.search_calls = []
        self.expanded = []
    def temporal_versions(self, cutoff):
        return {'versions':[], 'missing':[], 'note':'합성 테스트'}
    def search_enhanced(self, question, provider, **kwargs):
        self.search_calls.append(kwargs)
        # Unsorted, duplicate-containing test double: the review boundary also
        # enforces its limit even when a retrieval implementation misbehaves.
        offset = 0 if len(self.search_calls)==1 else 30
        rows = [row(i+offset,.99-i*.01) for i in range(1,31)]
        return list(reversed(rows))+[rows[0]]
    def expand_context(self, roots, **kwargs):
        self.expanded.append(roots)
        return roots + [{**row(100+i,1.),'relation':'references_article',
                         'referenced_by':r['number']} for i,r in enumerate(roots)], []


class ReviewProvider(FakeProvider):
    def __init__(self, extra=False):
        self.review_calls = []
        self.extra = extra
        self.assess_calls = 0
    def filter_evidence(self, question, candidates):
        self.review_calls.append(candidates)
        return candidates
    def assess_evidence(self, *args):
        self.assess_calls += 1
        return {'sufficient':not self.extra or self.assess_calls>1,
                'missing_issues':['추가 쟁점'] if self.extra and self.assess_calls==1 else [],
                'missing_facts':[],'search_queries':['다른 쟁점']}
    def answer(self, question, evidence, *args, **kwargs):
        self.context = evidence
        return '합성 답변 [1]'


@pytest.mark.parametrize('extra',[False,True])
def test_top_15_each_round_and_references_only_in_final_context(extra):
    store, provider = Store(), ReviewProvider(extra)
    result = run_chat(store,provider,Settings(candidate_limit=50),
                      ChatRequest(question='관련 서류',limit=10,threshold=.68))
    assert all(call['limit']==15 and call['threshold']==.68 for call in store.search_calls)
    assert len(provider.review_calls)==(2 if extra else 1)
    for batch in provider.review_calls:
        assert len(batch)==15 and all(not r.get('relation') for r in batch)
        assert [r['score'] for r in batch] == sorted((r['score'] for r in batch),reverse=True)
        assert len({r['number'] for r in batch})==15
    assert [r['number'] for r in provider.review_calls[0]] == [f'제{i}조' for i in range(1,16)]
    assert len(result['evidence'])==15
    assert any(r.get('relation') for r in provider.context)
    assert all(not r.get('relation') for r in result['evidence'])


def test_smaller_candidate_setting_and_fewer_hits_are_not_padded():
    assert review_limit(Settings(candidate_limit=20))==15
    assert review_limit(Settings(candidate_limit=8))==8
    provider, store = ReviewProvider(), Store()
    review(store,provider,Settings(), '질문', [row(1,.9),row(2,.8)],date(2026,10,21),set())
    assert len(provider.review_calls[0])==2


def response(documents, malformed=False):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'decisions':[] if malformed else [
        {'id':d['id'],'relevant':True,'role':'direct','conditions':[]} for d in documents]})}}]}


def test_15_short_candidates_one_structured_request_without_duplicate_spans(monkeypatch):
    provider = Provider(Settings(chat_model='gpt-5.4-mini',rerank_model='',api_key='test-only'))
    requests = []
    def post(path,payload):
        requests.append(payload)
        return response(json.loads(payload['messages'][-1]['content'])['candidates'])
    monkeypatch.setattr(provider,'post',post)
    candidates=[{**row(i,.9),'matched_units':[{'location':'제1항','text':'서류 제출 본문'}]} for i in range(1,16)]
    assert len(provider.filter_evidence('필요 서류',candidates))==15
    assert len(requests)==1
    req=requests[0]
    assert req['response_format']['type']=='json_schema' and req['response_format']['json_schema']['strict']
    assert req['reasoning_effort']=='low'
    docs=json.loads(req['messages'][-1]['content'])['candidates']
    assert all(d['body']=='서류 제출 본문' and d['matched_locations']==['제1항'] for d in docs)
    assert all('matched_units' not in d for d in docs)


def test_long_batches_run_two_at_a_time_keep_bodies_and_rank_order(monkeypatch):
    provider=Provider(Settings(api_key='test-only'))
    barrier, lock = Barrier(2), Lock()
    active, peak, seen = 0, 0, []
    def post(path,payload):
        nonlocal active, peak
        docs=json.loads(payload['messages'][-1]['content'])['candidates']
        with lock:
            active += 1
            peak=max(peak,active)
            seen.extend(docs)
        if docs[0]['number'] in ('제1조','제2조'):
            barrier.wait(timeout=5)
        with lock:
            active -= 1
        return response(docs)
    monkeypatch.setattr(provider,'post',post)
    candidates=[row(i,.9,'본문'*20000+f'단서{i}') for i in range(1,4)]
    result=provider.filter_evidence('질문',candidates)
    assert peak==2 and len(seen)==3
    assert [r['number'] for r in result]==['제1조','제2조','제3조']
    assert all(r['body']==c['body'] for r,c in zip(result,candidates))


def test_retry_only_malformed_batch_not_successful_batches(monkeypatch):
    import app.evidence_review as module
    monkeypatch.setattr(module,'BATCH_CHAR_LIMIT',1000)
    provider=Provider(Settings(api_key='test-only'))
    counts, lock = {}, Lock()
    def post(path,payload):
        data=json.loads(payload['messages'][-1]['content'])
        docs=data['candidates']
        number=docs[0]['number']
        with lock:
            counts[number]=counts.get(number,0)+1
            attempt=counts[number]
        if attempt==2:
            assert 'retry_requirements' in data
        return response(docs,malformed=number=='제2조' and attempt==1)
    monkeypatch.setattr(provider,'post',post)
    result=provider.filter_evidence('질문',[row(i,.9,'본문'*600) for i in range(1,4)])
    assert len(result)==3 and counts=={'제1조':1,'제2조':2,'제3조':1}


def test_persistent_format_failure_reports_reason_without_response_content(monkeypatch):
    provider=Provider(Settings(api_key='test-only'))
    calls=[]
    monkeypatch.setattr(provider,'post',lambda *args:(calls.append(args) or {'choices':[{'message':{'content':'private upstream content'}}]}))
    with pytest.raises(SetupError,match='JSON 형식 오류') as exc:
        provider.filter_evidence('질문',[row(1,.9)])
    assert len(calls)==2 and 'private' not in str(exc.value)


def test_length_failure_can_retry_and_fenced_json_is_read(monkeypatch):
    provider=Provider(Settings(api_key='test-only'))
    good=response([{'id':1}])
    good['choices'][0]['message']['content']='```json\n'+good['choices'][0]['message']['content']+'\n```'
    results=iter([{'choices':[{'finish_reason':'length','message':{'content':'{"decisions":['}}]},good])
    monkeypatch.setattr(provider,'post',lambda *args:next(results))
    assert len(provider.filter_evidence('질문',[row(1,.9)]))==1

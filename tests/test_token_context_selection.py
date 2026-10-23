"""Synthetic token budgets; verify full-article selection and citation consistency."""
import json

import httpx
import pytest

from app.chat_pipeline import run_chat
from app.config import Settings
from app.main import ChatRequest
from app.provider import Provider
from app.vllm_context import ContextBudgetExceeded
from test_ai_backends import settings, mock_http
from test_chat_pipeline import row
from test_chat_json import completion
from test_workflow import FakeProvider
from test_http_chat_stream import Chunks, event


def test_real_preflight_shrinks_review_prefix_before_any_generation(monkeypatch):
    generated, tokenized = [], []
    candidates = [row(i,1-i*.1,f'전체 본문 {i}. 단서와 예외를 보존한다.') for i in range(1,4)]
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200,json={'data':[{'id':'gemma','max_model_len':32768}]})
        body = json.loads(request.content)
        docs = json.loads(body['messages'][-1]['content'])['candidates']
        if request.url.path == '/tokenize':
            tokenized.append([d['number'] for d in docs])
            return httpx.Response(200,json={'count':40093 if len(docs)>2 else 25000})
        assert request.url.path == '/v1/chat/completions'
        assert len(docs) == 2 and body['max_tokens'] == 7768
        generated.append(docs)
        return httpx.Response(200,json=completion(json.dumps({'decisions':[
            {'id':d['id'],'relevant':True,'role':'direct','conditions':[],'reason':'요건을 규정함'} for d in docs]})))
    mock_http(monkeypatch,handle)
    provider = Provider(settings(chat_backend='company',company_chat_model='gemma',company_chat_max_tokens='auto'))
    selected = provider.filter_evidence('요건은?',candidates)
    assert [r['number'] for r in selected] == ['제1조','제2조']
    assert tokenized == [['제1조','제2조','제3조'],['제1조','제2조']]
    assert len(generated) == 1
    assert [d['body'] for d in generated[0]] == [r['body'] for r in candidates[:2]]
    excluded = provider.evidence_review_log[-1]
    assert excluded['number']=='제3조' and excluded['status']=='token_budget_excluded'
    assert excluded['relevant'] is None and excluded['input_tokens']==40093


def test_oversized_single_article_does_not_block_smaller_candidate(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    generated = []
    def post(path,payload):
        docs = json.loads(payload['messages'][-1]['content'])['candidates']
        if any(d['number']=='제1조' for d in docs):
            raise ContextBudgetExceeded(40093,32768,2048)
        generated.extend(d['number'] for d in docs)
        return completion(json.dumps({'decisions':[{'id':d['id'],'relevant':True} for d in docs]}))
    monkeypatch.setattr(provider,'post',post)
    selected = provider.filter_evidence('요건은?',[row(1,.9),row(2,.8)])
    assert [r['number'] for r in selected] == ['제2조']
    assert generated == ['제2조']
    assert provider.evidence_review_log[0]['status'] == 'token_budget_excluded'


def test_recheck_budget_exclusion_is_not_an_llm_rejection(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    def post(path,payload):
        data = json.loads(payload['messages'][-1]['content'])
        docs = data['candidates']
        if data['review_phase']=='recheck' and len(docs)>1:
            raise ContextBudgetExceeded(32000,32768,2048)
        return completion(json.dumps({'decisions':[
            {'id':d['id'],'relevant':data['review_phase']=='recheck'} for d in docs]}))
    monkeypatch.setattr(provider,'post',post)
    selected = provider.filter_evidence('요건은?',[row(1,.9),row(2,.8)])
    assert [r['number'] for r in selected] == ['제1조']
    assert provider.evidence_review_log[-1]['status']=='token_budget_excluded'
    assert provider.evidence_review_log[-1]['phase']=='recheck'


class Store:
    def temporal_versions(self, cutoff):
        return {'versions':[],'missing':[],'note':''}
    def search_enhanced(self, *args, **kwargs):
        return [row(1,.9),row(2,.8),row(3,.7)]
    def expand_context(self, roots, **kwargs):
        return roots + [{**row(4,.99),'relation':'references_annex'}], []


class BudgetProvider(FakeProvider):
    def __init__(self, assess_limit=3, answer_limit=2, history_limit=None):
        self.assess_limit, self.answer_limit, self.history_limit = assess_limit, answer_limit, history_limit
        self.delivered = None
    def rewrite_question(self, question):
        return question
    def assess_evidence(self, question, evidence, missing, as_of):
        if len(evidence)>self.assess_limit:
            raise ContextBudgetExceeded(40093,32768,2048)
        return {'sufficient':True,'missing_facts':[],'missing_issues':[],'search_queries':[]}
    def answer(self, question, evidence, history, **kwargs):
        if len(evidence)>self.answer_limit or (self.history_limit is not None and len(history)>self.history_limit):
            raise ContextBudgetExceeded(40093,32768,2048)
        self.delivered = (list(evidence),list(history),kwargs['assessment'])
        return '확인된 내용 [1][2]'


def test_assess_and_answer_trim_context_and_report_only_actual_sources():
    provider = BudgetProvider()
    trace = {}
    result = run_chat(Store(),provider,Settings(),ChatRequest(question='요건과 절차는?'),diagnostics=trace)
    assert result['generated']
    assert [r['number'] for r in result['evidence']] == ['제1조','제2조']
    assert result['linked_sources'] == {}
    assert [r['number'] for r in provider.delivered[0]] == ['제1조','제2조']
    assert provider.delivered[2]['sufficient'] is False
    excluded = result['review_trace']['token_budget_omissions']
    assert [(r['number'],r['stage']) for r in excluded] == [('제4조','assess'),('제3조','answer')]
    assert all(r['stage']=='token_budget_excluded' for r in trace['review_outcomes'] if r['number'] in ('제3조','제4조'))
    assert result['approved_count'] == 2


def test_old_history_is_removed_before_answer_evidence():
    provider = BudgetProvider(assess_limit=4,answer_limit=4,history_limit=0)
    result = run_chat(Store(),provider,Settings(),ChatRequest(question='요건은?',history=[
        {'role':'user','content':'과거 질문'},{'role':'assistant','content':'과거 답변'}]))
    assert provider.delivered[1] == []
    assert len(provider.delivered[0]) == 4
    assert result['review_trace']['history_messages_omitted'] == 2
    assert result['review_trace']['token_budget_omissions'] == []
    assert result['linked_sources']['C1']['number'] == '제4조'


@pytest.mark.parametrize('limits', [(0,0),(4,0)])
def test_no_whole_article_fits_gives_explicit_non_generated_result(limits):
    provider = BudgetProvider(*limits)
    result = run_chat(Store(),provider,Settings(),ChatRequest(question='요건은?'))
    assert result['generated'] is False
    assert result['evidence'] == [] and result['linked_sources'] == {}
    assert '토큰 한도' in result['answer']
    assert provider.delivered is None


def test_base_prompt_too_large_still_fails_without_truncating_question():
    provider = BudgetProvider(assess_limit=-1)
    with pytest.raises(ContextBudgetExceeded):
        run_chat(Store(),provider,Settings(),ChatRequest(question='유지할 원질의'))


@pytest.mark.parametrize('streaming',[False,True])
def test_answer_preflight_updates_visible_citations_before_generation(monkeypatch, streaming):
    class MarkedStore(Store):
        def search_enhanced(self, *args, **kwargs):
            return [row(i,1-i*.1,f'원문표식_{i}') for i in range(1,4)]
        def expand_context(self, roots, **kwargs):
            return roots + [{**row(4,.99,'원문표식_4'),'relation':'references_annex'}], []
    updates, generated = [], []
    config = settings(chat_backend='company',company_chat_model='gemma',company_chat_max_tokens='auto')
    provider = Provider(config)
    if streaming:
        provider = provider.for_stream(updates.append,lambda:False)
    monkeypatch.setattr(provider,'rewrite_question',lambda q:q)
    monkeypatch.setattr(provider,'filter_evidence',lambda q,rows:rows)
    monkeypatch.setattr(provider,'assess_evidence',lambda *args:{
        'sufficient':True,'missing_facts':[],'missing_issues':[],'search_queries':[]})
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200,json={'data':[{'id':'gemma','max_model_len':32768}]})
        body = json.loads(request.content)
        context = body['messages'][-1]['content']
        count = sum(f'원문표식_{i}' in context for i in range(1,5))
        if request.url.path == '/tokenize':
            return httpx.Response(200,json={'count':40093 if count>2 else 22000})
        generated.append(body)
        assert count == 2 and body['max_tokens'] == 10768
        assert '원문표식_1' in context and '원문표식_2' in context
        assert '[1] 합성법 제1조' in context and '[2] 합성법 제2조' in context
        answer = '확인된 내용 [1][2]'
        if streaming:
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks([
                event({'content':answer}),event(finish='stop'),'data: [DONE]\n\n']))
        return httpx.Response(200,json=completion(answer))
    mock_http(monkeypatch,handle)
    result = run_chat(MarkedStore(),provider,config,ChatRequest(question='요건은?'))
    assert len(generated) == 1
    assert [r['number'] for r in result['evidence']] == ['제1조','제2조']
    assert result['linked_sources'] == {}
    assert updates == (['확인된 내용 [1][2]'] if streaming else [])

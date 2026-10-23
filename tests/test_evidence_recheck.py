"""Test review mechanics with synthetic judgments, not model accuracy."""
import json

import pytest

from app.config import Settings
from app.evidence_review import parse_decisions
from app.laws import SetupError
from app.provider import Provider
from app.chat_pipeline import filter_once, run_chat
from app.main import ChatRequest
from test_chat_pipeline import row


def response(decisions):
    return {'choices':[{'message':{'content':json.dumps({'decisions':decisions})}}]}


def decision(index, relevant, reason, role=None):
    return {'id':index,'relevant':relevant,'role':role or ('direct' if relevant else 'irrelevant'),
            'conditions':[],'reason':reason}


@pytest.mark.parametrize('question,partial', [
    ('신청 대상과 제출 절차는?', '신청에는 별표의 서류를 제출한다.'),
    ('설치 대상과 적용 시점은?', '해당 시설에는 장치를 설치해야 한다.'),
    ('계산 대상과 방법은?', '계산 항목과 방법은 별표에 따른다.'),
])
def test_recheck_recovers_partial_support_but_preserves_final_rejection_and_cache(monkeypatch, question, partial):
    provider = Provider(Settings(api_key='test-only'))
    calls = []
    candidates = [row(1,.9), row(2,.8,'다른 대상에만 적용'), row(3,.7,partial), row(4,.6)]

    def post(path,payload):
        data = json.loads(payload['messages'][-1]['content'])
        calls.append(data)
        if data['review_phase'] == 'initial':
            return response([decision(4,True,'직접 근거'),decision(2,False,'명시된 대상과 충돌'),
                             decision(1,True,'직접 근거'),decision(3,False,'질문 전체에는 답할 수 없음')])
        assert [d['number'] for d in data['candidates']] == ['제2조','제3조']
        assert data['candidates'][1]['body'] == partial
        return response([decision(2,True,'질문의 일부에 필요한 전제를 규정함','supporting'),
                         decision(1,False,'명시된 대상과 충돌')])

    monkeypatch.setattr(provider,'post',post)
    checked, rejected = {}, set()
    accepted = filter_once(provider,question,candidates,checked,rejected)
    assert [r['number'] for r in accepted] == ['제1조','제3조','제4조']
    assert accepted[1]['evidence_role'] == 'supporting'
    assert len(calls) == 2
    assert len(provider.evidence_review_log) == 6
    assert [r['phase'] for r in provider.evidence_review_log] == ['initial']*4 + ['recheck']*2
    assert provider.evidence_review_log[-1]['number'] == '제3조'
    assert provider.evidence_review_log[-1]['reason'] == '질문의 일부에 필요한 전제를 규정함'
    assert all('body' not in r for r in provider.evidence_review_log)
    # Subsequent retrieval must not repeatedly retry a final rejection.
    assert filter_once(provider,question,candidates,checked,rejected) == accepted
    assert len(calls) == 2


def test_invalid_recheck_fails_after_one_format_retry(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    calls = []
    def post(path,payload):
        data = json.loads(payload['messages'][-1]['content'])
        calls.append(data)
        return response([decision(1,False,'기여 없음')] if data['review_phase']=='initial' else [])
    monkeypatch.setattr(provider,'post',post)
    with pytest.raises(SetupError,match='검증 실패'):
        provider.filter_evidence('절차는?', [row(1,.9)])
    assert [c['review_phase'] for c in calls] == ['initial','recheck','recheck']
    assert 'retry_requirements' in calls[-1]
    assert provider.evidence_review_log == []


@pytest.mark.parametrize('reason', [None, [], 'x'*601])
def test_reason_validation_does_not_leave_partial_audit(reason):
    audit = {}
    with pytest.raises(ValueError,match='판정 사유'):
        parse_decisions(response([decision(1,True,'유효'),decision(2,False,reason)]),2,outcomes=audit)
    assert audit == {}


def test_actual_answer_report_contains_recovery_and_final_stage_without_bodies(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    candidate = row(1,.8,'본문 비공개 표식')
    class Store:
        def temporal_versions(self, cutoff):
            return {'versions':[],'missing':[],'note':''}
        def search_enhanced(self, *args, **kwargs):
            return [candidate]
        def expand_context(self, roots, **kwargs):
            return roots, []
    monkeypatch.setattr(provider,'rewrite_question',lambda q:q)
    monkeypatch.setattr(provider,'assess_evidence',lambda *args:{
        'sufficient':True,'missing_facts':[],'missing_issues':[],'search_queries':[]})
    monkeypatch.setattr(provider,'answer',lambda *args,**kwargs:'확인된 부분 [1]')
    def post(path,payload):
        phase = json.loads(payload['messages'][-1]['content'])['review_phase']
        return response([decision(1,phase=='recheck','부분 근거를 재검토함')])
    monkeypatch.setattr(provider,'post',post)
    provider.evidence_review_log.append({'stale':'이전 요청'})
    trace = run_chat(Store(),provider,Settings(),ChatRequest(question='신청 절차는?'))['review_trace']
    assert trace['policy'] == 'rejection_recheck_v1'
    assert trace['review_outcomes'][0]['stage'] == 'visible_source'
    assert [r['relevant'] for r in trace['decisions']] == [False,True]
    assert len(trace['retrieved_candidates']) == 1
    assert '본문 비공개 표식' not in json.dumps(trace,ensure_ascii=False)
    assert 'stale' not in json.dumps(trace)

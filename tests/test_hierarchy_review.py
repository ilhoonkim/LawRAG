"""상하위라는 이유만으로 근거가 채택되거나 탈락 근거가 우회 복귀하지 않는지 검증합니다."""
from datetime import date
import json

import pytest

from app.chat_pipeline import review, run_chat, source_key
from app.config import Settings
from app.laws import SetupError
from app.main import ChatRequest
from app.provider import Provider
from test_chat_pipeline import row
from test_review_limits import ReviewProvider, Store, response


def linked(n):
    return {**row(n, .9), 'law_name':'합성법 시행령', 'version':'decree',
            'relation':'delegation_context', 'include_in_sources':True,
            'referenced_by_law':'합성법', 'referenced_by':'제1조',
            'reference_edges':[{'from_version':'v1', 'from_law':'합성법', 'from_article':'제1조',
                                'to_version':'decree', 'to_law':'합성법 시행령', 'to_article':f'제{n}조',
                                'provenance':'thdCmp knd=2'}]}


class HierarchyStore(Store):
    def search_enhanced(self, question, provider, **kwargs):
        self.search_calls.append(kwargs)
        # 추가 검색으로 직접 재발견되어도 탈락한 연결 근거는 다시 채택하지 않습니다.
        return [row(1,.9)] if len(self.search_calls)==1 else [
            {k:v for k,v in linked(2).items() if k not in ('relation','include_in_sources')}, row(4,.8)]

    def expand_context(self, roots, **kwargs):
        refs = [{'law_name':'합성법 시행령', 'version':'decree', 'number':f'제{n}조'} for n in (2,3)]
        return [{**r,'reference_titles':refs} for r in roots]+[linked(2),linked(3)], []


class SelectiveProvider(ReviewProvider):
    def __init__(self, reject=(2,), **kwargs):
        super().__init__(**kwargs)
        self.reject = {f'제{n}조' for n in reject}
        self.assessed = []

    def filter_evidence(self, question, candidates):
        self.review_calls.append(candidates)
        return [{**r,'evidence_role':'supporting','application_conditions':['상황 확인']} for r in candidates
                if r['number'] not in self.reject]

    def assess_evidence(self, question, evidence, *args):
        self.assessed.append(evidence)
        return super().assess_evidence(question,evidence,*args)


@pytest.mark.parametrize('reject', [(2,), (2,3)])
def test_only_relevant_connections_reach_assessment_answer_and_sources(reject):
    provider, stages = SelectiveProvider(reject=reject), []
    result = run_chat(HierarchyStore(),provider,Settings(),ChatRequest(question='서류는?'),stages.append)
    assert stages == ['rewrite','search','review','review_hierarchy','assess','answer']
    assert len(provider.review_calls)==2
    assert len(provider.review_calls[1])==2  # 한 조문씩 호출하지 않습니다.
    expected = {'제1조'} | ({'제3조'} if 3 not in reject else set())
    for evidence in [result['evidence'],provider.context,*provider.assessed]:
        assert {e['number'] for e in evidence} == expected
        assert all(e['relevance_checked'] for e in evidence)
        assert all(r['number'] not in provider.reject for e in evidence for r in e['reference_titles'])
    assert result['generated']


def test_additional_search_reuses_decisions_and_cannot_reintroduce_rejected_link():
    provider, stages = SelectiveProvider(extra=True), []
    result = run_chat(HierarchyStore(),provider,Settings(),ChatRequest(question='서류는?'),stages.append)
    keys = [source_key(e) for batch in provider.review_calls for e in batch]
    assert len(keys)==len(set(keys))
    assert stages.count('review_hierarchy')==1
    assert {e['number'] for e in result['evidence']}=={'제1조','제3조','제4조'}
    assert all(e['number']!='제2조' for e in provider.context)


def test_connection_cap_and_rejection_are_independent_of_inherited_scores():
    class Many(HierarchyStore):
        def expand_context(self, roots, **kwargs):
            # 최상위 검색 점수를 상속했어도 16번째 연결 조문을 무검토로 포함하지 않습니다.
            return roots + [linked(n) for n in range(2,22)], []
    provider = SelectiveProvider(reject=())
    evidence, missing = review(Many(),provider,Settings(candidate_limit=50), '질문',
                               [row(1,.9)],date(2026,10,22),set())
    assert [len(batch) for batch in provider.review_calls]==[1,15]
    assert len(evidence)==16 and len(missing)==5
    assert all(e['relevance_checked'] for e in evidence)
    assert not {m['number'] for m in missing} & {e['number'] for e in evidence}


def test_link_review_failure_does_not_generate_unchecked_answer():
    class Failed(SelectiveProvider):
        def filter_evidence(self, question, candidates):
            if candidates[0].get('relation'):
                raise SetupError('합성 연결 검증 오류')
            return super().filter_evidence(question,candidates)
    provider = Failed()
    with pytest.raises(SetupError,match='연결 검증'):
        run_chat(HierarchyStore(),provider,Settings(),ChatRequest(question='서류는?'))
    assert not provider.assessed and not hasattr(provider,'context')


def test_reviewer_receives_connection_provenance_and_full_body(monkeypatch):
    provider = Provider(Settings(api_key='test-only'))
    requests = []
    def post(path,payload):
        requests.append(payload)
        return response(json.loads(payload['messages'][-1]['content'])['candidates'])
    monkeypatch.setattr(provider,'post',post)
    candidate = linked(2)
    provider.filter_evidence('원질의', [candidate])
    document = json.loads(requests[0]['messages'][-1]['content'])['candidates'][0]
    assert document['body']==candidate['body']
    assert document['connection']['reference_edges']==candidate['reference_edges']
    assert '관계만으로 통과시키지 마세요' in requests[0]['messages'][0]['content']

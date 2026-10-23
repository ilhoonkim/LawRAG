"""Verified retrieval with bounded graph expansion and one gap-driven search."""
import re
from datetime import datetime, timezone
from .config import SEARCH_FUSION, SEARCH_THRESHOLD, SEARCH_RELEVANCE_THRESHOLD
from .temporal import resolve_date
from .retrieval import select_candidates
from .vllm_context import ContextBudgetExceeded
from .context_selection import evidence_units

# 채팅의 전체 흐름: 기준일 확정 → 원질의+재작성 검색 → 후보 검토 → 연결 조문 확장
# → 연결 조문 검토 → 근거 충분성 점검 → 필요 시 한 번 더 검색 → 확보한 본문으로 답변 생성.
# progress 호출 시점이 화면 단계 표시의 기준이며, 타이머로 진행 상황을 추정하지 않습니다.
STAGES = [
    ('rewrite', '질문 재조정 중'), ('search', '관련 법조항 검색 중'),
    ('review', '법조항 후보군 검토 중'), ('review_hierarchy', '상하위 연결 조문 검토 중'),
    ('assess', '근거 충분성 확인 중'),
    ('additional_search', '부족한 근거 추가 검색 중'), ('answer', '근거 법조항 기반 답변 생성 중'),
]


# 법령명과 조 번호가 같아도 개정 버전이 다르면 서로 다른 근거입니다.
def source_key(row):
    return row['version'], row['law_name'], row['number']


def review_limit(settings):
    # Existing .env values and a larger API limit must not bypass the chat cap.
    return min(15, max(1, settings.candidate_limit))


# 검색 단독 API는 참조 확장까지 수행할 수 있습니다. 채팅은 expand=False로 호출한 뒤
# 관련성 검토를 통과한 조문에 대해서만 참조를 확장합니다. 법령명 입력은 필수가 아닙니다.
def retrieve(store, provider, settings, body, *, variants=None, limit=None, expand=True, diagnostics=None):
    cutoff, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'clarification': clarification, 'as_of': None, 'missing_versions': []}
    timeline = store.temporal_versions(cutoff)
    named = [r['name'] for r in timeline['versions'] + timeline['missing'] if r['name'] in body.question]
    missing = [r for r in timeline['missing'] if r['name'] in named]
    evidence = store.search_enhanced(body.question,provider,variants=variants or (),cutoff=cutoff,
        threshold=SEARCH_THRESHOLD, limit=limit or body.limit,
        mode=body.mode,fusion=SEARCH_FUSION,rrf_threshold=SEARCH_RELEVANCE_THRESHOLD,
        **({'diagnostics':diagnostics} if diagnostics is not None else {}))
    evidence, missing_refs = store.expand_context(evidence,cutoff=cutoff,question=body.question,
        limit=min(80,max(0,settings.reference_limit))) if expand else (evidence,[])
    return {'evidence': evidence, 'missing_references': missing_refs, 'as_of': cutoff.isoformat(),
            'clarification': None, 'missing_versions': missing, 'coverage_note': timeline['note']}


# 길이 제한을 맞추려고 조문 중간을 자르면 단서·예외가 사라질 수 있습니다.
# 조문 전체를 포함하거나 제외하고, 제외한 근거는 부족한 자료 목록에 남깁니다.
def budgeted(evidence, max_chars):
    """Never cut a proviso off an article to fit a prompt."""
    selected, missing, size = [], [], 0
    def omit(rows):
        missing.extend({'law_name':r['law_name'],'number':r['number'],'version':r['version'],
                        'reason':'본문을 자르지 않기 위해 이번 입력 예산에서 제외되었습니다.'} for r in rows)
    for unit in evidence_units(evidence):
        length = sum(len(r['body']) for r in unit)
        if size+length<=max_chars and len(selected)+len(unit)<=40:
            selected.extend(unit)
            size += length
        elif length<=max_chars and len(unit)<=40:
            # Keep a higher-ranked article's annexes before a lower-ranked unit.
            omit(unit)
        else:
            # A unit cannot fit even by itself. Preserve its complete root and
            # any complete annexes that fit, explicitly reporting the remainder.
            if size+len(unit[0]['body'])>max_chars or len(selected)>=40:
                omit(unit)
                continue
            for row in unit:
                if size+len(row['body'])<=max_chars and len(selected)<40:
                    selected.append(row)
                    size += len(row['body'])
                else:
                    omit([row])
    return selected,missing


def trim_token_context(evidence, missing, omissions, error, stage):
    """Remove a low-priority unit, keeping cited annexes with retained roots."""
    if not evidence:
        raise error
    units = evidence_units(evidence)
    # If the last remaining article + annexes cannot fit, shed complete annexes
    # one at a time before giving up that article. Never truncate table rows.
    removed = units[-1] if len(units)>1 else [units[0][-1]]
    for row in removed:
        evidence.remove(row)
        gap = {k:row[k] for k in ('version','law_name','number')}
        gap.update(reason='토큰 한도로 전체 조문을 입력에서 제외함',stage=stage,**error.details())
        missing.append(gap)
        omissions.append(gap)
    valid = {source_key(e) for e in evidence}
    for e in evidence:
        e['reference_titles'] = [r for r in e.get('reference_titles',[]) if
            (r.get('version',e['version']),r['law_name'],r['number']) in valid]


def assess_with_budget(provider, question, evidence, missing, as_of, omissions):
    while True:
        try:
            return provider.assess_evidence(question,evidence,missing,as_of)
        except ContextBudgetExceeded as error:
            trim_token_context(evidence,missing,omissions,error,'assess')


def review_budget_gaps(provider):
    gaps = {}
    for entry in getattr(provider,'evidence_review_log',[]):
        if entry.get('status') == 'token_budget_excluded':
            gaps[source_key(entry)] = {k:entry[k] for k in (
                'version','law_name','number','reason','input_tokens','context_tokens','output_reserve')}
    return list(gaps.values())


def filter_once(provider, question, candidates, checked, rejected):
    """같은 질문·버전·조문은 한 번만 검토합니다. 검색 점수와 연결 경로는 캐시하지 않습니다."""
    pending = [e for e in candidates if source_key(e) not in checked and source_key(e) not in rejected]
    if pending:
        accepted = {source_key(e):e for e in provider.filter_evidence(question, pending)}
        for candidate in pending:
            key = source_key(candidate)
            if key in accepted:
                checked[key] = {k:accepted[key][k] for k in ('evidence_role','application_conditions') if k in accepted[key]}
            else:
                rejected.add(key)
    return [{**e, **checked[source_key(e)], 'relevance_checked':True} for e in candidates
            if source_key(e) in checked and source_key(e) not in rejected]


def review(store, provider, settings, question, candidates, cutoff, rejected, *, checked=None, progress=lambda stage: None):
    checked = checked if checked is not None else {}
    # 검색 단계의 임계치와 원질의 본문 핵심어 보호를 추가 검색 병합 후에도
    # 유지합니다. 참조에서 물려받은 점수는 직접 검색 후보에 사용하지 않습니다.
    ordered = sorted((e for e in candidates if not e.get('relation') and source_key(e) not in rejected),
                     key=lambda e:(-e.get('score',0),source_key(e)))
    unique = {}
    for e in ordered:
        unique.setdefault(source_key(e),e)
    selected = select_candidates(list(unique.values()),review_limit(settings))
    reviewed = filter_once(provider, question, selected, checked, rejected)
    # 탈락 기록은 추가 검색에도 유지됩니다. 같은 후보가 연결 조문으로 우회 복귀하지 못합니다.
    rejected.update({source_key(e) for e in selected} - {source_key(e) for e in reviewed})
    if not reviewed:
        return [], review_budget_gaps(provider)
    # 통과한 검색 근거에서만 확장합니다. 위임·상하위 연결 자체가 질의 관련성을
    # 보장하지 않으므로 표시 대상 연결 조문은 별도 묶음으로 검토합니다.
    expanded, missing = store.expand_context(reviewed,cutoff=cutoff,question=question,
        limit=min(80,max(0,settings.reference_limit)))
    expanded = [{**e,'evidence_role':'supporting'} if e.get('relation') else e
                for e in expanded if source_key(e) not in rejected]
    hierarchy = {}
    for e in expanded:
        if e.get('relation') and e.get('include_in_sources'):
            hierarchy.setdefault(source_key(e), e)
    # graph가 정한 문맥 우선순위를 유지합니다. 원문을 잘라 보내지 않으며 최대 15개만
    # 검토합니다. 너무 긴 묶음은 evidence_review에서 분할하고 최대 2개 병렬 처리합니다.
    selected_links = list(hierarchy.values())[:review_limit(settings)]
    deferred = [{'law_name':e['law_name'], 'number':e['number'], 'version':e['version'],
                 'reason':'상하위 연결 조문 검토 개수 제한으로 이번 답변에서 제외되었습니다.'}
                for e in list(hierarchy.values())[review_limit(settings):]]
    if any(source_key(e) not in checked for e in selected_links):
        progress('review_hierarchy')
    approved_links = {source_key(e):e for e in filter_once(provider, question, selected_links, checked, rejected)}
    expanded = [approved_links[source_key(e)] if source_key(e) in hierarchy else e for e in expanded
                if source_key(e) not in hierarchy or source_key(e) in approved_links]
    evidence, omitted = budgeted(expanded,max(1000,settings.context_max_chars))
    valid = {source_key(e) for e in evidence}
    for e in evidence:
        e.pop('parent_context',None)
        e['reference_titles'] = [r for r in e.get('reference_titles',[])
            if (r.get('version',e['version']),r['law_name'],r['number']) in valid]
        e['reference_edges'] = [r for r in e.get('reference_edges',[]) if not any(
            (r.get(side+'_version'),r.get(side+'_law'),r.get(side+'_article')) in rejected
            for side in ('from','to'))]
    # Missing refs from rejected parents cannot alone suppress the final answer.
    parent_keys = {(e['version'],e['number']) for e in evidence}
    missing = [r for r in missing if not r.get('from_article') or (r['version'],r['from_article']) in parent_keys]
    return evidence,missing+omitted+deferred+review_budget_gaps(provider)


def run_chat(store, provider, settings, body, progress=lambda stage: None, *, diagnostics=None):
    provider.evidence_review_log = []
    token_omissions = []
    cutoff, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'answer': clarification, 'clarification': clarification,
                'as_of': None, 'missing_versions': [], 'generated': False}
    # 이 요청의 모든 검색은 같은 기준일을 사용합니다. 재작성 문장이 날짜를 바꾸지 못합니다.
    search_body = body.model_copy(update={'as_of':cutoff})
    progress('rewrite')
    # rewrite는 원질의를 대체하지 않습니다. 검색은 둘 다 사용하고 검토·답변은 원질의로 합니다.
    rewritten = provider.rewrite_question(body.question)
    progress('search')
    search_trace = {} if diagnostics is not None else None
    if diagnostics is not None:
        diagnostics.update(searches=[search_trace], review_outcomes=[])
    result = retrieve(store,provider,settings,search_body,variants=[rewritten],
                      limit=review_limit(settings),expand=False,diagnostics=search_trace)
    candidates, rejected, checked = result['evidence'], set(), {}
    progress('review')
    evidence, missing = review(store,provider,settings,body.question,candidates,cutoff,rejected,checked=checked,progress=progress)
    catalog_gaps = [{'law_name':r['name'],'number':'기준일 적용 버전','reason':'질문에 명시된 법령의 적용 버전이 미수집입니다.'} for r in result['missing_versions']]
    missing += catalog_gaps
    progress('assess')
    assessment = assess_with_budget(provider,body.question,evidence,missing,result['as_of'],token_omissions)
    initial_assessment = assessment
    normalize = lambda text: re.sub(r'[\W_]+','',text).casefold()
    used = {normalize(body.question),normalize(rewritten)}
    extra_queries = []
    for q in assessment['search_queries']:
        if normalize(q) not in used:
            extra_queries.append(q)
            used.add(normalize(q))
    extra_queries = extra_queries[:2]
    # 추가 검색은 누락 쟁점에 대한 새 검색어로 한 번만 수행합니다.
    # 같은 질문 반복이나 끝없는 재검색을 피하고, 사용자 사실 부족은 질문으로 남깁니다.
    rounds = 0
    # User facts require clarification, not a search that invents those facts.
    if not assessment['sufficient'] and extra_queries and (assessment['missing_issues'] or (not evidence and not assessment['missing_facts'])):
        rounds = 1
        progress('additional_search')
        search_trace = {} if diagnostics is not None else None
        if diagnostics is not None:
            diagnostics['searches'].append(search_trace)
        additional = retrieve(store,provider,settings,search_body,variants=extra_queries,
            limit=review_limit(settings),expand=False,diagnostics=search_trace)['evidence']
        candidates = list({source_key(e):e for e in candidates+additional}.values())
        merged = {source_key(e):e for e in evidence if source_key(e) not in rejected}
        for e in additional:
            key = source_key(e)
            # A linked article found independently in the new search is now a
            # search hit with its own score, eligible for a recommendation.
            if key not in rejected and (key not in merged or merged[key].get('relation')):
                merged[key] = e
        progress('review')
        evidence, missing = review(store,provider,settings,body.question,list(merged.values()),cutoff,rejected,checked=checked,progress=progress)
        missing += catalog_gaps
        progress('assess')
        assessment = assess_with_budget(provider,body.question,evidence,missing,result['as_of'],token_omissions)
    # 검색 근거와 표시 대상으로 지정한 상하위 조문은 같은 순서로 숫자 인용을 받습니다.
    # 일반 참고 문맥은 LLM에만 전달합니다. Provider와 UI의 번호가 이 순서와 일치합니다.
    evidence.sort(key=lambda e:(-e.get('score',0),bool(e.get('relation')),source_key(e)))
    answer = None
    history = [m.model_dump() for m in body.history][-6:]
    omitted_history = 0
    if evidence:
        progress('answer')
        question = f'기준일: {result["as_of"]} (이 날짜에 시행 중인 수집 버전 기준)\n{body.question}'
        while evidence:
            try:
                answer = provider.answer(question,evidence,history,missing_references=missing,assessment=assessment)
                break
            except ContextBudgetExceeded as error:
                # The exception is only raised by the preflight, before any
                # answer token is streamed. Never restart a partially sent answer.
                if history:
                    history.pop(0)
                    omitted_history += 1
                    continue
                trim_token_context(evidence,missing,token_omissions,error,'answer')
                # An earlier sufficiency judgment covered a larger set. Do not
                # reuse its "sufficient" conclusion after excluding a source.
                assessment = {**assessment,'sufficient':False,
                    'missing_issues':list(dict.fromkeys(assessment['missing_issues'] +
                        ['입력 토큰 한도로 일부 근거가 제외되었습니다. 확보된 본문으로 답할 수 있는 부분만 답하세요.']))[:8]}
    # 상하위 연결은 출처에 포함하고 일반 연결 문맥만 숨깁니다.
    # approved_count에는 검토를 통과한 후보에서 확장한 연결 문맥도 포함됩니다.
    visible_evidence = [e for e in evidence if not e.get('relation') or e.get('include_in_sources')]
    # Preserve the exact C-label ordering used by Provider.answer. These are
    # context links, not independently ranked/reviewed search recommendations.
    linked_sources = {f'C{i}': {k:e.get(k,'') for k in
        ('version','law_name','number','title','html_url','effective','relation')}
        for i,e in enumerate((e for e in evidence if e.get('relation') and not e.get('include_in_sources')),1)}
    visible_keys = {source_key(e) for e in visible_evidence}
    context_keys = {source_key(e) for e in evidence}
    token_keys = {source_key(e) for e in review_budget_gaps(provider)+token_omissions}
    outcomes = [
        {'version':key[0], 'law_name':key[1], 'number':key[2],
         'stage':('visible_source' if key in visible_keys else 'llm_context_only' if key in context_keys
                  else 'token_budget_excluded' if key in token_keys else 'llm_rejected' if key in rejected
                  else 'approved_but_omitted')}
        for key in sorted(set(checked) | rejected | token_keys)]
    review_trace = {
        'policy':'rejection_recheck_v1','question':body.question,'as_of':result['as_of'],
        'created_at':datetime.now(timezone.utc).isoformat(),
        'rewritten_query':rewritten,'additional_queries':extra_queries if rounds else [],
        'fusion':SEARCH_FUSION,'threshold':SEARCH_THRESHOLD,
        'threshold_basis':'admission_score','relevance_threshold':SEARCH_RELEVANCE_THRESHOLD,
        'relevance_threshold_basis':'rrf_score',
        'candidate_note':'검색에서 반환된 후보 목록입니다. 전체 자료의 점수·순위는 diagnose_search로 확인합니다.',
        'retrieved_candidates':[{k:e.get(k) for k in (
            'version','law_name','number','title','score','rrf_score','admission_score',
            'body_semantic_score','body_keyword_score','title_semantic_score','matched_body_nouns')}
            for e in candidates],
        'decisions':list(provider.evidence_review_log), 'review_outcomes':outcomes,
        'answer_context':[{**{k:e.get(k,'') for k in ('version','law_name','number','relation')},
                           'body_chars':len(e['body']),
                           'image_placeholders':e['body'].count('[이미지: 원문 확인 필요]')}
                          for e in evidence],
        'token_budget_omissions':review_budget_gaps(provider)+token_omissions,
        'history_messages_omitted':omitted_history,
        'missing_references':missing,'assessment':assessment,
    }
    if diagnostics is not None:
        diagnostics['review_outcomes'] = outcomes
        diagnostics['review_decisions'] = list(provider.evidence_review_log)
        diagnostics['token_budget_omissions'] = review_budget_gaps(provider)+token_omissions
        diagnostics['answer_context'] = review_trace['answer_context']
        diagnostics['missing_references'] = missing
    result.update(evidence=visible_evidence,linked_sources=linked_sources,review_trace=review_trace,
        missing_references=missing,rewritten_query=rewritten,
        original_question=body.question,candidate_count=len(candidates),approved_count=len(evidence),
        linked_context_count=len(evidence)-len(visible_evidence),
        hierarchy_source_count=sum(bool(e.get('include_in_sources')) for e in visible_evidence),
        assessment=assessment,initial_assessment=initial_assessment,additional_search_rounds=rounds,
        additional_queries=extra_queries if rounds else [],fusion=SEARCH_FUSION)
    if not evidence:
        if result['missing_versions']:
            targets = ', '.join(dict.fromkeys(f'{r["name"]} (시행 {r["effective"][:4]}-{r["effective"][4:6]}-{r["effective"][6:8]})' for r in result['missing_versions']))
            answer = f'질문한 법령의 기준일({result["as_of"]}) 적용 버전이 미수집 상태입니다: {targets}. 법령관리에서 이 버전을 수집한 뒤 다시 질문해 주세요.'
        else:
            answer = ('모델의 입력 토큰 한도 안에 넣을 수 있는 근거 조문이 남지 않았습니다. 질문 범위를 좁히거나 더 큰 문맥을 지원하는 모델을 사용해 주세요.'
                      if token_keys else '수집된 법령에서 질문에 답할 만한 근거 조문을 찾지 못했습니다. 어떤 상황인지 조금 더 자세히 알려주세요.')
        if assessment['missing_facts']:
            answer += '\n확인이 필요한 정보: ' + ', '.join(assessment['missing_facts'])
        return {**result,'answer':answer,'generated':False}
    return {**result,'answer':answer,'generated':True}

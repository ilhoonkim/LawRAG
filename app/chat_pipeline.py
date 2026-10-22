"""Verified retrieval with bounded graph expansion and one gap-driven search."""
import re
from .config import SEARCH_FUSION, SEARCH_THRESHOLD
from .temporal import resolve_date
from .retrieval import select_candidates

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
        mode=body.mode,fusion=SEARCH_FUSION,
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
    for row in sorted(evidence,key=lambda e:(-e.get('score',0),bool(e.get('relation')),source_key(e))):
        if size+len(row['body']) > max_chars or len(selected)>=40:
            missing.append({'law_name':row['law_name'],'number':row['number'],'version':row['version'],
                            'reason':'본문을 자르지 않기 위해 이번 입력 예산에서 제외되었습니다.'})
        else:
            selected.append(row)
            size += len(row['body'])
    return selected,missing


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
        return [], []
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
    return evidence,missing+omitted+deferred


def run_chat(store, provider, settings, body, progress=lambda stage: None, *, diagnostics=None):
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
    assessment = provider.assess_evidence(body.question,evidence,missing,result['as_of'])
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
        assessment = provider.assess_evidence(body.question,evidence,missing,result['as_of'])
    # 검색 근거와 표시 대상으로 지정한 상하위 조문은 같은 순서로 숫자 인용을 받습니다.
    # 일반 참고 문맥은 LLM에만 전달합니다. Provider와 UI의 번호가 이 순서와 일치합니다.
    evidence.sort(key=lambda e:(-e.get('score',0),bool(e.get('relation')),source_key(e)))
    # 상하위 연결은 출처에 포함하고 일반 연결 문맥만 숨깁니다.
    # approved_count에는 검토를 통과한 후보에서 확장한 연결 문맥도 포함됩니다.
    visible_evidence = [e for e in evidence if not e.get('relation') or e.get('include_in_sources')]
    if diagnostics is not None:
        visible_keys = {source_key(e) for e in visible_evidence}
        context_keys = {source_key(e) for e in evidence}
        diagnostics['review_outcomes'] = [
            {'version':key[0], 'law_name':key[1], 'number':key[2],
             'stage':('llm_rejected' if key in rejected else 'visible_source' if key in visible_keys
                      else 'llm_context_only' if key in context_keys else 'approved_but_omitted')}
            for key in sorted(set(checked) | rejected)]
        diagnostics['missing_references'] = missing
    result.update(evidence=visible_evidence,missing_references=missing,rewritten_query=rewritten,
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
            answer = '수집된 법령에서 질문에 답할 만한 근거 조문을 찾지 못했습니다. 어떤 상황인지 조금 더 자세히 알려주세요.'
        if assessment['missing_facts']:
            answer += '\n확인이 필요한 정보: ' + ', '.join(assessment['missing_facts'])
        return {**result,'answer':answer,'generated':False}
    progress('answer')
    question = f'기준일: {result["as_of"]} (이 날짜에 시행 중인 수집 버전 기준)\n{body.question}'
    answer = provider.answer(question,evidence,[m.model_dump() for m in body.history],missing_references=missing,assessment=assessment)
    return {**result,'answer':answer,'generated':True}

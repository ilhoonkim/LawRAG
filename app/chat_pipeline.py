"""Verified retrieval with bounded graph expansion and one gap-driven search."""
import re
from .temporal import resolve_date

STAGES = [
    ('rewrite', '질문 재조정 중'), ('search', '관련 법조항 검색 중'),
    ('review', '법조항 후보군 검토 중'), ('assess', '근거 충분성 확인 중'),
    ('additional_search', '부족한 근거 추가 검색 중'), ('answer', '근거 법조항 기반 답변 생성 중'),
]


def source_key(row):
    return row['version'], row['law_name'], row['number']


def retrieve(store, provider, settings, body, *, variants=None, limit=None, expand=True):
    cutoff, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'clarification': clarification, 'as_of': None, 'missing_versions': []}
    timeline = store.temporal_versions(cutoff)
    named = [r['name'] for r in timeline['versions'] + timeline['missing'] if r['name'] in body.question]
    missing = [r for r in timeline['missing'] if r['name'] in named]
    evidence = store.search_enhanced(body.question,provider,variants=variants or (),cutoff=cutoff,
        threshold=settings.threshold if body.threshold is None else body.threshold, limit=limit or body.limit,
        mode=body.mode,fusion=body.fusion or settings.search_fusion)
    evidence, missing_refs = store.expand_context(evidence,cutoff=cutoff,question=body.question,
        limit=min(80,max(0,settings.reference_limit))) if expand else (evidence,[])
    return {'evidence': evidence, 'missing_references': missing_refs, 'as_of': cutoff.isoformat(),
            'clarification': None, 'missing_versions': missing, 'coverage_note': timeline['note']}


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


def review(store, provider, settings, question, candidates, cutoff, rejected):
    # Expand before filtering so necessary exceptions/definitions can establish relevance.
    expanded, missing = store.expand_context(candidates,cutoff=cutoff,question=question,
        limit=min(80,max(0,settings.reference_limit)))
    unique = {source_key(e):e for e in reversed(expanded) if source_key(e) not in rejected}
    reviewed = provider.filter_evidence(question,list(unique.values()))
    rejected.update(set(unique) - {source_key(e) for e in reviewed})
    evidence, omitted = budgeted(reviewed,max(1000,settings.context_max_chars))
    valid = {source_key(e) for e in evidence}
    for e in evidence:
        e.pop('parent_context',None)
        e['reference_titles'] = [r for r in e.get('reference_titles',[])
            if (r.get('version',e['version']),r['law_name'],r['number']) in valid]
    # Missing refs from rejected parents cannot alone suppress the final answer.
    parent_keys = {(e['version'],e['number']) for e in evidence}
    missing = [r for r in missing if not r.get('from_article') or (r['version'],r['from_article']) in parent_keys]
    return evidence,missing+omitted


def run_chat(store, provider, settings, body, progress=lambda stage: None):
    cutoff, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'answer': clarification, 'clarification': clarification,
                'as_of': None, 'missing_versions': [], 'generated': False}
    search_body = body.model_copy(update={'as_of':cutoff})
    progress('rewrite')
    rewritten = provider.rewrite_question(body.question)
    progress('search')
    result = retrieve(store,provider,settings,search_body,variants=[rewritten],
                      limit=max(body.limit,min(50,settings.candidate_limit)),expand=False)
    candidates, rejected = result['evidence'], set()
    progress('review')
    evidence, missing = review(store,provider,settings,body.question,candidates,cutoff,rejected)
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
    rounds = 0
    # User facts require clarification, not a search that invents those facts.
    if not assessment['sufficient'] and extra_queries and (assessment['missing_issues'] or (not evidence and not assessment['missing_facts'])):
        rounds = 1
        progress('additional_search')
        additional = retrieve(store,provider,settings,search_body,variants=extra_queries,
            limit=max(body.limit,min(50,settings.candidate_limit)),expand=False)['evidence']
        candidates = list({source_key(e):e for e in candidates+additional}.values())
        merged = {source_key(e):e for e in evidence if source_key(e) not in rejected}
        for e in additional:
            key = source_key(e)
            # A linked article found independently in the new search is now a
            # search hit with its own score, eligible for a recommendation.
            if key not in rejected and (key not in merged or merged[key].get('relation')):
                merged[key] = e
        progress('review')
        evidence, missing = review(store,provider,settings,body.question,list(merged.values()),cutoff,rejected)
        missing += catalog_gaps
        progress('assess')
        assessment = provider.assess_evidence(body.question,evidence,missing,result['as_of'])
    # Only search hits are recommended in the UI. Keep linked articles in the
    # LLM context; Provider assigns numeric citations to the visible hits only.
    evidence.sort(key=lambda e:(-e.get('score',0),bool(e.get('relation')),source_key(e)))
    visible_evidence = [e for e in evidence if not e.get('relation')]
    result.update(evidence=visible_evidence,missing_references=missing,rewritten_query=rewritten,
        original_question=body.question,candidate_count=len(candidates),approved_count=len(evidence),
        linked_context_count=len(evidence)-len(visible_evidence),
        assessment=assessment,initial_assessment=initial_assessment,additional_search_rounds=rounds,
        additional_queries=extra_queries if rounds else [],fusion=body.fusion or settings.search_fusion)
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

"""Verified retrieval; progress events describe actual work, not timed UI guesses."""
from .temporal import resolve_date

STAGES = [
    ('rewrite', '질문 재조정 중'),
    ('search', '관련 법조항 검색 중'),
    ('review', '법조항 후보군 검토 중'),
    ('answer', '근거 법조항 기반 답변 생성 중'),
]


def source_key(row):
    return row['version'], row['law_name'], row['number']


def retrieve(store, provider, settings, body, *, variants=None, limit=None, expand=True):
    cutoff, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'clarification': clarification, 'as_of': None, 'missing_versions': []}
    timeline = store.temporal_versions(cutoff)
    named = [r['name'] for r in timeline['versions'] + timeline['missing'] if r['name'] in body.question]
    named_law = max(named, key=len) if named else None
    missing = [r for r in timeline['missing'] if r['name'] == named_law]
    evidence = store.search(body.question, provider, settings.threshold if body.threshold is None else body.threshold,
                            limit or body.limit, body.mode, as_of=cutoff, title_weight=settings.title_weight,
                            min_title=settings.min_title_score, min_body=settings.min_body_score, query_variants=variants)
    evidence, missing_refs = store.expand_references(evidence) if expand else (evidence, [])
    return {'evidence': evidence, 'missing_references': missing_refs, 'as_of': cutoff.isoformat(),
            'clarification': None, 'missing_versions': missing, 'coverage_note': timeline['note']}


def run_chat(store, provider, settings, body, progress=lambda stage: None):
    # Dates and final answers always use the original question.
    _, clarification = resolve_date(body.question, body.as_of)
    if clarification:
        return {'evidence': [], 'answer': clarification, 'clarification': clarification,
                'as_of': None, 'missing_versions': [], 'generated': False}
    progress('rewrite')
    rewritten = provider.rewrite_question(body.question)
    progress('search')
    result = retrieve(store, provider, settings, body, variants=[rewritten],
                      limit=max(body.limit, min(50, settings.candidate_limit)), expand=False)
    candidates = result['evidence']
    progress('review')
    approved = provider.filter_evidence(body.question, candidates)
    approved_keys = {source_key(e) for e in approved}
    rejected = {source_key(e) for e in candidates} - approved_keys
    roots = approved[:body.limit]
    expanded, missing = store.expand_references(roots)
    root_keys = {source_key(e) for e in roots}
    extra = [e for e in expanded if source_key(e) not in root_keys | rejected]
    for e in extra:
        e['parent_context'] = next((r['body'] for r in roots if r['version'] == e['version'] and r['number'] == e.get('referenced_by')), '')
    accepted_extra = provider.filter_evidence(body.question, extra)
    for e in accepted_extra:
        e.pop('parent_context', None)
    evidence = [e for e in expanded if source_key(e) in root_keys] + accepted_extra
    valid = {source_key(e) for e in evidence}
    for e in evidence:
        e['reference_titles'] = [r for r in e.get('reference_titles', [])
                                 if (e['version'], r['law_name'], r['number']) in valid]
    # Sort BEFORE generation so [1], [2] agree with the visible source numbers.
    evidence.sort(key=lambda e: -e.get('score', 0))
    result.update(evidence=evidence, missing_references=missing, rewritten_query=rewritten,
                  candidate_count=len(candidates), approved_count=len(evidence))
    if not evidence:
        if result['missing_versions']:
            targets = ', '.join(dict.fromkeys(
                f'{r["name"]} (시행 {r["effective"][:4]}-{r["effective"][4:6]}-{r["effective"][6:8]})'
                for r in result['missing_versions']))
            answer = (f'질문한 법령의 기준일({result["as_of"]}) 적용 버전이 미수집 상태입니다: {targets}. '
                      '법령관리에서 이 버전을 수집한 뒤 다시 질문해 주세요.')
        else:
            answer = '수집된 법령에서 질문에 답할 만한 근거 조문을 찾지 못했습니다. 어떤 상황인지 조금 더 자세히 알려주세요.'
        return {**result, 'answer': answer, 'generated': False}
    progress('answer')
    question = f'기준일: {result["as_of"]} (이 날짜에 시행 중인 수집 버전 기준)\n{body.question}'
    answer = provider.answer(question, evidence, [m.model_dump() for m in body.history], missing_references=missing)
    return {**result, 'answer': answer, 'generated': True}

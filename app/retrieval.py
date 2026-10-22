"""Field-aware retrieval and rank fusion, using existing article embeddings."""
import re
import unicodedata
from urllib.parse import quote
from .temporal import today_in_korea
import sqlite_vec
from .laws import SetupError
from .store import grams, body_text, expanded_query
from .keywords import NounIndex


# 현재 검색 구현입니다. 기준일에 맞는 버전에서 원질의와 재작성 질의를 함께 비교하고,
# 법령명·조문 제목·본문의 검색 신호를 결합한 뒤 원래 법조항을 결과로 반환합니다.
# 아래 항·호·목 조각은 검색 위치를 찾는 용도이며, 답변 문맥에는 조 전체를 보존합니다.
def fragments(body):
    """Search paragraph/item spans, but always return the complete parent article."""
    markers = list(re.finditer(r'(?m)^\s*(?:([①-⑳])|(\d+)\.[ \t]+|([가-힣])\.[ \t]+)', body))
    paragraph, item, result = '', '', []
    for i, marker in enumerate(markers):
        if marker[1]:
            paragraph, item = f'제{ord(marker[1])-ord("①")+1}항', ''
            location = paragraph
        elif marker[2]:
            item = f'제{int(marker[2])}호'
            location = paragraph + item
        else:
            location = paragraph + item + marker[3] + '목'
        end = markers[i+1].start() if i+1 < len(markers) else len(body)
        result.append({'location':location, 'text':body[marker.start():end].strip(),
                       'start':marker.start(), 'end':end})
    return result


# 문자 비교는 한글·영숫자 2글자 집합의 겹침 비율입니다. 실제 키워드 점수는
# 아래 문자 비교와 keywords.py의 명사·복합어 IDF 일치 점수 중 큰 값을 씁니다.
# balanced는 질의 대비 일치율뿐 아니라 짧은 제목 쪽의 일치율도 반영합니다.
def overlap(query, text, balanced=False):
    q, t = grams(query), grams(text)
    coverage = len(q & t) / max(1,len(q))
    return .5*coverage + .5*len(q & t)/max(1,len(t)) if balanced else coverage


def content_overlap(query, text):
    q, t = grams(query), grams(text)
    # Reward a coherent paragraph/item match over terms scattered through a
    # long article, without making length alone disqualify a parent article.
    return .8*len(q&t)/max(1,len(q)) + .2*len(q&t)/max(1,len(t))


# 법령명은 기존 제목 벡터에도 포함되어 있지만, 조문 제목과 섞인 벡터만으로는
# '총톤수 10톤 미만 소형어선' 같은 적용 범위를 독립적으로 비교하기 어렵습니다.
# 저장된 원래 이름을 직접 비교하므로 새 임베딩이나 검색용 생성 문서가 필요 없습니다.
NAME_BOILERPLATE = set().union(*(grams(word) for word in
    ('기준', '규정', '관한', '등에', '대한', '법률', '시행령', '시행규칙', '행정규칙', '요령', '지침')))
NAME_MEASURE = re.compile(r'(\d+(?:\.\d+)?)(킬로미터|킬로와트|미터|톤)(미만|이하|이상|초과)?')


def compact_name(value):
    return re.sub(r'[^가-힣a-z0-9.]', '', unicodedata.normalize('NFKC', value).lower())


def law_name_score(question, name):
    """0~1 이름 일치율. 공백·전각을 무시하고 부분 명칭과 수치 조건을 비교합니다.

    '기준' 등 공통 말만 맞는 경우는 투표하지 않습니다. '어선'·'레저'처럼
    짧은 대상어도 점수를 주고, 일치하는 이름 조각이 늘수록 점수를 높입니다.
    긴 질문에서도 대상어가 희석되지 않도록 법령명 쪽 포함률을 씁니다.
    """
    query, title = compact_name(question), compact_name(name)
    if not title:
        return 0.
    if title in query:
        return 1.
    meaningful = grams(title) - NAME_BOILERPLATE
    matched = grams(query) & meaningful
    if not matched:
        return 0.
    score = len(matched) / max(1, len(meaningful))
    # 같은 단위의 명시된 수치·범위가 다르면 이름 일치 보너스를 낮춥니다.
    # 다른 법의 본문 검색 자체는 막지 않습니다. 숫자를 지우거나 10톤/20톤을
    # 같은 적용 범위로 취급하지 않기 위한 보조 신호입니다.
    query_measures = NAME_MEASURE.findall(query)
    for value, unit, bound in NAME_MEASURE.findall(title):
        comparable = [(v, b) for v, u, b in query_measures if u == unit]
        if comparable and not any(float(v) == float(value) and (not b or not bound or b == bound)
                                  for v, b in comparable):
            score *= .25
    return score


# RRF는 원점수 대신 각 목록의 순위로 1/(k+rank)를 합산합니다.
# 한 목록 안의 같은 문서는 한 번만 기여하고, 목록에 없는 문서는 그 목록에서 0점입니다.
def fuse(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking),1):
            scores[key] = scores.get(key,0.) + 1/(constant+rank)
    return scores


def field_rrf(per_query, mode, exact_ids=(), constant=60, depth=50):
    """Fuse title/body retrievers, then combine law-name, title and body ranks.

    A rewrite adds a retrieval path, not another vote at the final field level.
    Missing field hits contribute zero; the maximum is 3 / (constant + 1).
    Law names are ranked once per name; all its candidate articles share the
    rank. Article count/IDs must not arbitrarily determine the law-name vote.
    """
    # 1차: 각 필드 안에서 원질의·rewrite의 키워드/벡터 순위를 결합합니다.
    # 2차: 법령명·제목·본문 세 필드의 순위를 결합합니다.
    # 질의 변형이 많아도 최종 필드 투표 수가 늘어나지는 않습니다.
    fields = {'title': ('title', 'title_semantic'), 'body': ('lexical', 'semantic')}
    rankings, ranks = [], {}
    for field, signals in fields.items():
        lists = []
        for scores in per_query:
            for signal in signals if mode == 'hybrid' else signals[:1]:
                ordered = sorted(scores, key=lambda s: (-s[signal], s['row']['id']))
                lists.append([s['row']['id'] for s in ordered if s[signal] > 0][:depth])
        combined = fuse(lists, constant)
        ordered = sorted(combined, key=lambda key: (-combined[key], key))
        # 각 검색기의 상위 depth 합집합을 보존합니다. 결합 뒤 다시 depth로
        # 자르면 키워드에서 1위여도 벡터 목록 밖이라는 이유로 사라집니다.
        ranking = list(dict.fromkeys([*exact_ids, *ordered]))
        rankings.append(ranking)
        ranks[field] = {key: rank for rank, key in enumerate(ranking, 1)}
    combined = fuse(rankings, constant)
    names, members = {}, {}
    for scores in per_query:
        for score in scores:
            if score.get('law', 0) <= 0:
                continue
            name = score['row']['law_name']
            names[name] = max(names.get(name, 0.), score['law'])
            members.setdefault(name, set()).add(score['row']['id'])
    ranks['law_name'] = {}
    for rank, name in enumerate(sorted(names, key=lambda n: (-names[n], n))[:depth], 1):
        for key in members[name]:
            ranks['law_name'][key] = rank
            combined[key] = combined.get(key, 0.) + 1/(constant + rank)
    return combined, ranks


def select_candidates(rows, limit):
    """RRF 순서를 유지하되 최대 1/3은 원질의의 핵심 본문 일치에 배정합니다.

    총 검토 수는 늘리지 않습니다. 임계치를 통과한 후보만 입력하며, 나머지는
    RRF로 채웁니다. 추가 검색 결과를 합칠 때도 같은 규칙을 사용합니다.
    """
    key = lambda row: (-row.get('score', 0), -row.get('weighted_score', 0),
                       row.get('id', ''), row.get('version', ''), row.get('number', ''))
    ordered = sorted(rows, key=key)
    # 사용자가 직접 법령+조번호를 지정한 조회는 키워드 보호보다 먼저 보존합니다.
    chosen = {(r['version'], r['number']): r for r in ordered if r.get('exact_identifier')}
    focused = sorted((r for r in ordered if r.get('body_focus_score', 0) > 0),
                     key=lambda r: (-r['body_focus_score'], *key(r)))
    for row in focused[:max(1, limit // 3)]:
        if len(chosen) >= limit:
            break
        chosen.setdefault((row['version'], row['number']), row)
    for row in ordered:
        if len(chosen) >= limit:
            break
        chosen.setdefault((row['version'], row['number']), row)
    return sorted(chosen.values(), key=key)[:limit]


def search(store, question, provider, *, variants=(), cutoff=None, threshold=.48,
           limit=20, mode='hybrid', fusion='weighted', compare=False, diagnostics=None):
    if fusion not in ('weighted','rrf') or not 0 <= threshold <= 1:
        raise ValueError('검색 결합 방식 또는 임계치가 올바르지 않습니다.')
    cutoff = cutoff or today_in_korea()
    selected = store.temporal_versions(cutoff)['versions']
    queries = list(dict.fromkeys([question, *variants]))
    fusion_metadata = {'queries': queries, 'threshold_basis': 'admission_score',
                       'rrf_constant': 60, 'rrf_fields': ['law_name', 'title', 'body'],
                       'rrf_strategy': 'field_hierarchical', 'rrf_max_score': 3/61,
                       'law_name_weight': .20, 'law_name_method': 'partial_keyword_coverage',
                       'keyword_method': 'noun_idf_with_ngram_fallback',
                       'candidate_selection': 'rrf_with_original_body_noun_reserve'}
    if diagnostics is not None:
        diagnostics.update(**fusion_metadata, threshold=threshold, limit=limit, candidates=[])
    # 이름 비교는 원질의와 법령당 한 번만 수행합니다. rewrite가 만든 법령명으로
    # 특정 법을 밀어 올리지 않으며, 이름이 안 맞는 법도 제목·본문으로 계속 검색합니다.
    name_scores = {law['name']: law_name_score(question, law['name']) for law in selected}
    explicit_names = {name for name in name_scores if compact_name(name) in compact_name(question)}
    rows = []
    with store.connect() as db:
        for law in selected:
            for r in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position',(law['version'],)):
                # 이동 등으로 조번호만 남은 행은 의도적으로 벡터가 없습니다.
                # 실제 본문이 있는 행의 벡터 누락 검사는 아래에서 그대로 수행합니다.
                if not body_text(r).strip():
                    continue
                row = {**dict(r), 'law_name':law['name'], 'law_id':law['law_id'],
                             'effective':law['effective'], 'promulgated':law['promulgated'],
                             'source_url':law['source_url'], 'id':law['version']+':'+str(r['position'])}
                rows.append(row)
        if not rows:
            return {'weighted':[], 'rrf':[], **fusion_metadata} if compare else []
        bodies = [body_text(row) for row in rows]
        noun_index = NounIndex([row['title'] for row in rows], bodies)
        # 제목·본문 벡터는 동일 모델/차원이어야 합니다. 다른 모델의 벡터를 섞어 비교하지 않습니다.
        # 질의 벡터는 한 번 생성해 모든 필드와 조문 비교에 재사용합니다.
        if mode == 'hybrid':
            if any(r['model'] != provider.fingerprint or r['vector'] is None or r['title_vector'] is None for r in rows):
                raise SetupError('제목·본문 벡터가 없거나 임베딩 모델이 다릅니다. 저장 자료를 재임베딩하거나 키워드 검색을 선택하세요.')
            vectors = provider.embed(queries)
            if len(vectors) != len(queries) or any(len(r[field]) != 4*len(v)
                    for r in rows for v in vectors for field in ('vector', 'title_vector')):
                raise SetupError('검색 임베딩 차원이 저장된 자료와 다릅니다. 재임베딩을 확인하세요.')
            packed = [sqlite_vec.serialize_float32(v) for v in vectors]
        else:
            packed = [None] * len(queries)
        per_query = []
        for query, vector in zip(queries,packed):
            scored = []
            for position, row in enumerate(rows):
                # 법령명은 독립적인 가산 신호이며 검색 범위를 제한하는 필터가 아닙니다.
                clean = expanded_query(query.replace(row['law_name'], ''))
                clean = re.sub(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일', '', clean)
                clean = re.sub(r'제\s*\d+\s*조(?:의\s*\d+)?', '', clean)
                noun_plan = noun_index.plan(clean)
                title_nouns = noun_plan.matches(position, 'title')
                body_nouns = noun_plan.matches(position, 'body')
                title_noun_score = noun_plan.score(title_nouns)
                body_noun_score = noun_plan.score(body_nouns)
                title_ngram = overlap(clean,row['title'],True)
                title = max(title_ngram, title_noun_score)
                units = fragments(row['body']) if row['number'].startswith('제') else []
                # A focused item can match even when its parent has a generic heading.
                matches = [{**u,'ngram':content_overlap(clean,u['text'])} for u in units]
                for match in matches:
                    match['score'] = max(match['ngram'], noun_plan.score_text(match['text']))
                matches.sort(key=lambda u:-u['score'])
                lexical_ngram = max([content_overlap(clean,bodies[position])] + [m['ngram'] for m in matches])
                lexical = max(lexical_ngram, body_noun_score)
                semantic, title_semantic = (0., 0.)
                if vector:
                    distances = db.execute('SELECT 1-vec_distance_cosine(?,?), 1-vec_distance_cosine(?,?)',
                        (row['vector'],vector,row['title_vector'],vector)).fetchone()
                    semantic, title_semantic = (max(0., min(1., value)) for value in distances)
                content = .65*semantic + .35*lexical if vector else lexical
                law = name_scores[row['law_name']]
                exact = row['law_name'] in explicit_names and any(re.sub(r'\s+','',m.group()) == row['number']
                    for m in re.finditer(r'제\s*\d+\s*조(?:의\s*\d+)?',question))
                # 이름 일치율은 최대 0.20을 더합니다. 기존 본문·제목 점수를 낮추지
                # 않으므로 다른 법의 관련 조문도 계속 후보가 됩니다. 이름만 맞아도
                # 본문 최소 적합도(0.20)를 통과하지 못하면 후보로 사용하지 않습니다.
                quality = min(1., .80*content + .20*title + .20*law)
                if exact:
                    quality = max(quality,.95)
                # 정확한 본문 명사가 약한 임베딩의 35% 가중치에 희석되지 않게
                # 독립적인 명사 검색 경로도 허용합니다. 원질의의 희소 핵심어가
                # 본문에 실제로 있어야 하며, 이름/제목만으로 이 경로를 타지 않습니다.
                focus = noun_plan.focus_score(body_nouns) if query == question else 0.
                noun_quality = min(1., .8*body_noun_score + .2*title_noun_score + .2*law) if focus else 0.
                noun_eligible = noun_quality >= threshold and body_noun_score >= .20
                admission = max(quality, noun_quality)
                eligible = (quality >= threshold and (exact or content >= .20)) or noun_eligible
                scored.append({'row':row, 'quality':quality,
                    'semantic':semantic, 'title':title, 'title_semantic':title_semantic,
                    'body':content, 'lexical':lexical,
                    'body_ngram':lexical_ngram, 'title_ngram':title_ngram,
                    'body_noun_score':body_noun_score, 'title_noun_score':title_noun_score,
                    'body_nouns':body_nouns, 'title_nouns':title_nouns,
                    'law':law, 'exact':bool(exact), 'query':query, 'eligible':eligible,
                    'admission':admission, 'noun_quality':noun_quality,
                    'body_focus_score':focus if noun_eligible else 0.,
                    'focus_nouns':sorted(noun_plan.focus_terms.intersection(body_nouns)) if query == question else [],
                    'matches':[{k:m[k] for k in ('location','text','start','end')} for m in matches[:3] if m['score'] > 0]})
            per_query.append(scored)
    exact_ids = [s['row']['id'] for s in per_query[0] if s['exact']]
    eligible_ids = {s['row']['id'] for scores in per_query for s in scores if s['eligible']}
    # 이름이 같은 무관한 조문 수백 개가 필드별 상위 50개 자리를 차지하지 않게
    # 임계치·본문 조건을 통과한 후보들 안에서만 순위를 결합합니다.
    rrf, field_ranks = field_rrf([[s for s in scores if s['row']['id'] in eligible_ids]
                                for scores in per_query], mode, exact_ids)
    # RRF 원점수를 법령명·제목·본문 세 필드의 최대값 3/(60+1)로 나눕니다.
    # 이는 이론상 최대값 대비 비율이며 법적 정확도나 답변 정답 확률이 아닙니다.
    normalizer = fusion_metadata['rrf_max_score']
    result = []
    for i,row in enumerate(rows):
        observations = [scores[i] for scores in per_query]
        # 통과한 검색 경로 중 최고값을 사용합니다. 제목만 맞는 경로가 본문까지
        # 조건을 통과한 다른 질의의 결과를 덮어쓰지 않게 합니다.
        best = max(observations,key=lambda s:(s['eligible'],s['quality']))
        if diagnostics is None and (not best['eligible'] or row['id'] not in rrf):
            continue
        out = {k:v for k,v in row.items() if k not in ('vector','title_vector','model')}
        out.update(score=round(best['quality'],4), weighted_score=round(best['quality'],4),
            admission_score=round(max(s['admission'] for s in observations),4),
            noun_admission_score=round(observations[0]['noun_quality'],4),
            body_focus_score=round(observations[0]['body_focus_score'],4),
            matched_focus_nouns=observations[0]['focus_nouns'],
            rrf_score=round(rrf.get(row['id'],0.)/normalizer,6), title_score=round(best['title'],4),
            body_score=round(best['body'],4), law_name_score=round(best['law'],4),
            body_keyword_score=round(best['lexical'],4), body_semantic_score=round(best['semantic'],4),
            body_ngram_score=round(best['body_ngram'],4), title_ngram_score=round(best['title_ngram'],4),
            body_noun_score=round(best['body_noun_score'],4), title_noun_score=round(best['title_noun_score'],4),
            matched_body_nouns=best['body_nouns'], matched_title_nouns=best['title_nouns'],
            keyword_method=fusion_metadata['keyword_method'],
            title_keyword_score=round(best['title'],4), exact_identifier=best['exact'],
            title_semantic_score=round(best['title_semantic'],4),
            rrf_raw_score=rrf.get(row['id'],0.), rrf_max_score=normalizer,
            rrf_field_ranks={field:ranks.get(row['id']) for field,ranks in field_ranks.items()},
            matched_query=best['query'], matched_units=best['matches'], context_restored=True,
            retrieval_queries=[s['query'] for s in observations if s['eligible']],
            html_url='/documents/'+row['version']+'.html?as_of='+cutoff.isoformat()+'#'+quote(row['section_id'],safe=''))
        if diagnostics is not None:
            diagnostic = {k:v for k,v in out.items() if k not in
                          ('body','matched_units','reference_titles','section_id','position')}
            diagnostic['eligible'] = best['eligible']
            diagnostic['stage'] = ('below_threshold' if not best['eligible'] and max(s['admission'] for s in observations) < threshold
                                   else 'below_body_minimum' if not best['eligible']
                                   else 'outside_field_pool' if row['id'] not in rrf
                                   else 'outside_candidate_limit')
            diagnostics['candidates'].append(diagnostic)
        if best['eligible'] and row['id'] in rrf:
            result.append(out)
    # UI는 계산 방식과 무관하게 score를 '관련도 점수'로 표시합니다.
    # compare도 같은 후보·같은 계산 결과에서 정렬 방식만 달리해 비교합니다.
    def ranked(method):
        ordered = sorted(result,key=lambda r:(-r[method+'_score'],-r['weighted_score'],r['id']))
        ranked_rows = [{**r,'score':r[method+'_score'],'fusion':method,'retrieval_rank':i}
                       for i,r in enumerate(ordered,1)]
        return select_candidates(ranked_rows,limit) if method == 'rrf' else ranked_rows[:limit]
    selected_rows = ranked(fusion)
    if diagnostics is not None:
        selected_ids = {row['id'] for row in selected_rows}
        ranks = {row['id']:i for i,row in enumerate(sorted(result,
                 key=lambda r:(-r[fusion+'_score'],-r['weighted_score'],r['id'])),1)}
        for diagnostic in diagnostics['candidates']:
            diagnostic['retrieval_rank'] = ranks.get(diagnostic['id'])
            if diagnostic['id'] in selected_ids:
                diagnostic['stage'] = 'selected'
    if compare:
        return {'weighted':ranked('weighted'),'rrf':ranked('rrf'), **fusion_metadata}
    return selected_rows

"""Field-aware retrieval and rank fusion, using existing article embeddings."""
import re
from urllib.parse import quote
from .temporal import today_in_korea
import sqlite_vec
from .laws import SetupError
from .store import grams, body_text, expanded_query
from .search_metadata import load as load_metadata, source_hash


# 현재 검색 구현입니다. 기준일에 맞는 버전에서 원질의와 재작성 질의를 함께 비교하고,
# 제목·본문·검색 메타데이터의 신호를 결합한 뒤 원래 법조항을 결과로 반환합니다.
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


# 키워드 비교는 BM25가 아니라 한글·영숫자 2글자 집합의 겹침 비율입니다.
# balanced는 질의 대비 일치율뿐 아니라 짧은 제목/메타데이터 쪽의 일치율도 반영합니다.
def overlap(query, text, balanced=False):
    q, t = grams(query), grams(text)
    coverage = len(q & t) / max(1,len(q))
    return .5*coverage + .5*len(q & t)/max(1,len(t)) if balanced else coverage


def content_overlap(query, text):
    q, t = grams(query), grams(text)
    # Reward a coherent paragraph/item match over terms scattered through a
    # long article, without making length alone disqualify a parent article.
    return .8*len(q&t)/max(1,len(q)) + .2*len(q&t)/max(1,len(t))


# RRF는 원점수 대신 각 목록의 순위로 1/(k+rank)를 합산합니다.
# 한 목록 안의 같은 문서는 한 번만 기여하고, 목록에 없는 문서는 그 목록에서 0점입니다.
def fuse(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking),1):
            scores[key] = scores.get(key,0.) + 1/(constant+rank)
    return scores


def field_rrf(per_query, mode, exact_ids=(), constant=60, depth=50, metadata_fields=()):
    """Fuse query/retriever ranks within each field, then fuse the two fields.

    A rewrite adds a retrieval path, not another vote at the final field level.
    Missing field hits contribute zero; the maximum is field_count / (constant + 1).
    Original explicit law/article identifiers are pinned in each field instead
    of being counted as a third, unrelated RRF dimension.
    """
    # 1차: 각 필드 안에서 원질의·rewrite의 키워드/벡터 순위를 결합합니다.
    # 2차: 제목·본문 필드 순위를 결합하며, 메타데이터가 있으면 요약·질문 필드도 추가합니다.
    # 질의 변형이나 예상 질문 개수가 많다고 최종 필드 투표 수가 늘어나지는 않습니다.
    fields = {'title': ('title', 'title_semantic'), 'body': ('lexical', 'semantic')}
    fields.update({field:(field+'_keyword', field+'_semantic') for field in metadata_fields})
    rankings, ranks = [], {}
    for field, signals in fields.items():
        lists = []
        for scores in per_query:
            for signal in signals if mode == 'hybrid' else signals[:1]:
                ordered = sorted(scores, key=lambda s: (-s[signal], s['row']['id']))
                lists.append([s['row']['id'] for s in ordered if s[signal] > 0][:depth])
        combined = fuse(lists, constant)
        ordered = sorted(combined, key=lambda key: (-combined[key], key))
        ranking = list(dict.fromkeys([*exact_ids, *ordered]))[:depth]
        rankings.append(ranking)
        ranks[field] = {key: rank for rank, key in enumerate(ranking, 1)}
    return fuse(rankings, constant), ranks


def metadata_scores(db, metadata, query, vector):
    scores = {'summary':0., 'questions':0., 'summary_keyword':0., 'summary_semantic':0.,
              'questions_keyword':0., 'questions_semantic':0., 'matched_metadata':None}
    best = 0.
    for piece in metadata.get('pieces',[]):
        field = 'questions' if piece['field'] == 'question' else 'summary'
        lexical = overlap(query,piece['text'],True)
        semantic = 0.
        compatible = vector and piece['vector'] and len(piece['vector']) == len(vector)
        if compatible:
            semantic = max(0.,min(1.,db.execute('SELECT 1-vec_distance_cosine(?,?)',
                (piece['vector'],vector)).fetchone()[0]))
        # Either retrieval channel may discover a parent. Averaging a good
        # semantic match with near-zero literal overlap recreates the original
        # vocabulary gate. RRF combines their ranks later; the LLM checks source.
        value = max(semantic,lexical)
        # Multiple generated questions are alternate paths to ONE parent, not
        # separate documents/votes. Duplicating questions cannot raise its score.
        scores[field] = max(scores[field],value)
        scores[field+'_keyword'] = max(scores[field+'_keyword'],lexical)
        scores[field+'_semantic'] = max(scores[field+'_semantic'],semantic)
        if value > best:
            best = value
            scores['matched_metadata'] = {'field':field,'text':piece['text']}
    return scores


def search(store, question, provider, *, variants=(), cutoff=None, threshold=.48,
           limit=20, mode='hybrid', fusion='weighted', compare=False):
    if fusion not in ('weighted','rrf') or not 0 <= threshold <= 1:
        raise ValueError('검색 결합 방식 또는 임계치가 올바르지 않습니다.')
    cutoff = cutoff or today_in_korea()
    selected = store.temporal_versions(cutoff)['versions']
    queries = list(dict.fromkeys([question, *variants]))
    fusion_metadata = {'queries': queries, 'threshold_basis': 'weighted_score',
                       'rrf_constant': 60, 'rrf_fields': ['title', 'body'],
                       'rrf_strategy': 'field_hierarchical', 'rrf_max_score': 2/61}
    rows = []
    with store.connect() as db:
        for law in selected:
            metadata = load_metadata(db,law['version'],provider.fingerprint)
            for r in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position',(law['version'],)):
                row = {**dict(r), 'law_name':law['name'], 'law_id':law['law_id'],
                             'effective':law['effective'], 'promulgated':law['promulgated'],
                             'source_url':law['source_url'], 'id':law['version']+':'+str(r['position'])}
                item = metadata.get(r['position'])
                # 원문이 달라진 오래된 요약을 새 조문에 연결하지 않도록 내용 해시를 확인합니다.
                row['_metadata'] = item if item and item['source_hash']==source_hash(row) else {}
                rows.append(row)
        if not rows:
            return {'weighted':[], 'rrf':[], **fusion_metadata} if compare else []
        metadata_fields = [field for field in ('summary','questions') if any(r['_metadata'].get(field) for r in rows)]
        fusion_metadata['rrf_fields'] += metadata_fields
        fusion_metadata['rrf_max_score'] = len(fusion_metadata['rrf_fields'])/61
        fusion_metadata['metadata_articles'] = sum(bool(r['_metadata']) for r in rows)
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
            for row in rows:
                # Law names are a small original-query-only bonus, never a scope filter.
                clean = expanded_query(query.replace(row['law_name'], ''))
                clean = re.sub(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일', '', clean)
                clean = re.sub(r'제\s*\d+\s*조(?:의\s*\d+)?', '', clean)
                title = overlap(clean,row['title'],True)
                units = fragments(row['body']) if row['number'].startswith('제') else []
                # A focused item can match even when its parent has a generic heading.
                matches = sorted([{**u,'score':content_overlap(clean,u['text'])} for u in units], key=lambda u:-u['score'])
                lexical = max([content_overlap(clean,body_text(row))] + [m['score'] for m in matches])
                semantic, title_semantic = (0., 0.)
                if vector:
                    distances = db.execute('SELECT 1-vec_distance_cosine(?,?), 1-vec_distance_cosine(?,?)',
                        (row['vector'],vector,row['title_vector'],vector)).fetchone()
                    semantic, title_semantic = (max(0., min(1., value)) for value in distances)
                content = .65*semantic + .35*lexical if vector else lexical
                law = 1. if row['law_name'] in question else 0.
                exact = law and any(re.sub(r'\s+','',m.group()) == row['number']
                    for m in re.finditer(r'제\s*\d+\s*조(?:의\s*\d+)?',question))
                # Preserve the weighted/content gate. The law-name/article-title
                # vector participates in field RRF, but cannot bypass body quality.
                quality = min(1., .80*content + .20*title + .03*law)
                if exact:
                    quality = max(quality,.95)
                meta = metadata_scores(db,row['_metadata'],clean,vector)
                metadata_quality = max(meta['summary'],meta['questions'])
                # 통과 여부와 정렬은 구분합니다. RRF를 선택해도 순위만 높다는 이유로
                # 무관한 후보를 통과시키지 않으며, 원문 또는 메타데이터의 임계치를 먼저 요구합니다.
                original_eligible = quality >= threshold and (exact or content >= .20)
                metadata_eligible = bool(meta['matched_metadata']) and metadata_quality >= threshold
                combined = max(quality if original_eligible else 0., metadata_quality if metadata_eligible else 0.)
                scored.append({'row':row, 'quality':quality,
                    'semantic':semantic, 'title':title, 'title_semantic':title_semantic,
                    'body':content, 'lexical':lexical,
                    'law':law, 'exact':bool(exact), 'query':query, **meta,
                    'combined_quality':combined, 'metadata_quality':metadata_quality,
                    'original_eligible':original_eligible, 'metadata_eligible':metadata_eligible,
                    'matches':[{k:m[k] for k in ('location','text','start','end')} for m in matches[:3] if m['score'] > 0]})
            per_query.append(scored)
    exact_ids = [s['row']['id'] for s in per_query[0] if s['exact']]
    rrf, field_ranks = field_rrf(per_query, mode, exact_ids, metadata_fields=metadata_fields)
    # RRF 원점수를 활성 필드 수/(60+1)로 나눠 표시용 0~1 점수로 만듭니다.
    # 이는 이론상 최대값 대비 비율이며 법적 정확도나 답변 정답 확률이 아닙니다.
    normalizer = fusion_metadata['rrf_max_score']
    result = []
    for i,row in enumerate(rows):
        observations = [scores[i] for scores in per_query]
        best = max(observations,key=lambda s:s['combined_quality'])
        # Metadata can discover an otherwise missed parent. The verifier still
        # receives only its original full text, never generated search material.
        if not best['original_eligible'] and not best['metadata_eligible']:
            continue
        if row['id'] not in rrf:
            continue
        out = {k:v for k,v in row.items() if k not in ('vector','title_vector','model','_metadata')}
        out.update(score=round(best['combined_quality'],4), weighted_score=round(best['combined_quality'],4),
            rrf_score=round(rrf.get(row['id'],0.)/normalizer,6), title_score=round(best['title'],4),
            body_score=round(best['body'],4), law_name_score=best['law'],
            body_keyword_score=round(best['lexical'],4), body_semantic_score=round(best['semantic'],4),
            title_keyword_score=round(best['title'],4), exact_identifier=best['exact'],
            title_semantic_score=round(best['title_semantic'],4),
            rrf_raw_score=rrf[row['id']], rrf_max_score=normalizer,
            rrf_field_ranks={field:ranks.get(row['id']) for field,ranks in field_ranks.items()},
            source_weighted_score=round(best['quality'],4), metadata_score=round(best['metadata_quality'],4),
            summary_score=round(best['summary'],4), questions_score=round(best['questions'],4),
            metadata_field_scores={key:round(best[key],4) for key in (
                'summary_keyword','summary_semantic','questions_keyword','questions_semantic')},
            matched_metadata=best['matched_metadata'],
            retrieval_route='metadata' if not best['original_eligible'] else 'source',
            matched_query=best['query'], matched_units=best['matches'], context_restored=True,
            retrieval_queries=[s['query'] for s in observations if s['original_eligible'] or s['metadata_eligible']],
            html_url='/documents/'+row['version']+'.html?as_of='+cutoff.isoformat()+'#'+quote(row['section_id'],safe=''))
        result.append(out)
    # UI는 계산 방식과 무관하게 score를 '관련도 점수'로 표시합니다.
    # compare도 같은 후보·같은 계산 결과에서 정렬 방식만 달리해 비교합니다.
    def ranked(method):
        ordered = sorted(result,key=lambda r:(-r[method+'_score'],-r['weighted_score'],r['id']))[:limit]
        return [{**r,'score':r[method+'_score'],'fusion':method} for r in ordered]
    if compare:
        return {'weighted':ranked('weighted'),'rrf':ranked('rrf'), **fusion_metadata}
    return ranked(fusion)

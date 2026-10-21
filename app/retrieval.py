"""Field-aware retrieval and rank fusion, using existing article embeddings."""
import re
from urllib.parse import quote
import sqlite_vec
from .laws import SetupError
from .store import grams, body_text, expanded_query


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


def overlap(query, text, balanced=False):
    q, t = grams(query), grams(text)
    coverage = len(q & t) / max(1,len(q))
    return .5*coverage + .5*len(q & t)/max(1,len(t)) if balanced else coverage


def content_overlap(query, text):
    q, t = grams(query), grams(text)
    # Reward a coherent paragraph/item match over terms scattered through a
    # long article, without making length alone disqualify a parent article.
    return .8*len(q&t)/max(1,len(q)) + .2*len(q&t)/max(1,len(t))


def fuse(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking),1):
            scores[key] = scores.get(key,0.) + 1/(constant+rank)
    return scores


def search(store, question, provider, *, variants=(), cutoff=None, threshold=.48,
           limit=20, mode='hybrid', fusion='weighted', compare=False):
    if fusion not in ('weighted','rrf') or not 0 <= threshold <= 1:
        raise ValueError('검색 결합 방식 또는 임계치가 올바르지 않습니다.')
    selected = store.temporal_versions(cutoff)['versions']
    queries = list(dict.fromkeys([question, *variants]))
    rows = []
    with store.connect() as db:
        for law in selected:
            for r in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position',(law['version'],)):
                rows.append({**dict(r), 'law_name':law['name'], 'law_id':law['law_id'],
                             'effective':law['effective'], 'promulgated':law['promulgated'],
                             'source_url':law['source_url'], 'id':law['version']+':'+str(r['position'])})
        if not rows:
            return {'weighted':[], 'rrf':[], 'queries':queries} if compare else []
        if mode == 'hybrid':
            if any(r['model'] != provider.fingerprint or r['vector'] is None for r in rows):
                raise SetupError('벡터가 없거나 임베딩 모델이 다릅니다. 저장 자료를 재임베딩하거나 키워드 검색을 선택하세요.')
            vectors = provider.embed(queries)
            if len(vectors) != len(queries) or any(len(r['vector']) != 4*len(v) for r in rows for v in vectors):
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
                semantic = max(0.,min(1.,db.execute('SELECT 1-vec_distance_cosine(?,?)',(row['vector'],vector)).fetchone()[0])) if vector else 0.
                content = .65*semantic + .35*lexical if vector else lexical
                law = 1. if row['law_name'] in question else 0.
                exact = law and any(re.sub(r'\s+','',m.group()) == row['number']
                    for m in re.finditer(r'제\s*\d+\s*조(?:의\s*\d+)?',question))
                # Title vectors in legacy DBs include law names; deliberately don't
                # use them as a separate semantic topic score. No reindex is needed.
                quality = min(1., .80*content + .20*title + .03*law)
                if exact:
                    quality = max(quality,.95)
                scored.append({'row':row, 'quality':quality, 'keyword':.80*lexical+.20*title,
                    'semantic':semantic, 'title':title, 'body':content, 'lexical':lexical,
                    'law':law, 'exact':bool(exact), 'query':query,
                    'matches':[{k:m[k] for k in ('location','text','start','end')} for m in matches[:3] if m['score'] > 0]})
            per_query.append(scored)
    # Independent keyword and semantic lists for each query, not a blend's rank.
    rankings = []
    for scores in per_query:
        for field in (('keyword','semantic') if mode=='hybrid' else ('keyword',)):
            rankings.append([s['row']['id'] for s in sorted(scores,key=lambda s:(-s[field],s['row']['id']))[:50] if s[field] > 0])
    exact_ids = [s['row']['id'] for s in per_query[0] if s['exact']]
    if exact_ids:
        rankings.append(exact_ids)
    rrf = fuse(rankings)
    normalizer = len(rankings)/61 if rankings else 1
    result = []
    for i,row in enumerate(rows):
        observations = [scores[i] for scores in per_query]
        best = max(observations,key=lambda s:s['quality'])
        # A title/law-name hit alone cannot qualify; RRF never bypasses this gate.
        if not best['exact'] and (best['body'] < .20 or best['quality'] < threshold):
            continue
        if best['quality'] < threshold:
            continue
        if row['id'] not in rrf:
            continue
        out = {k:v for k,v in row.items() if k not in ('vector','title_vector','model')}
        out.update(score=round(best['quality'],4), weighted_score=round(best['quality'],4),
            rrf_score=round(rrf.get(row['id'],0.)/normalizer,6), title_score=round(best['title'],4),
            body_score=round(best['body'],4), law_name_score=best['law'],
            body_keyword_score=round(best['lexical'],4), body_semantic_score=round(best['semantic'],4),
            title_keyword_score=round(best['title'],4), exact_identifier=best['exact'],
            matched_query=best['query'], matched_units=best['matches'], context_restored=True,
            retrieval_queries=[s['query'] for s in observations if s['quality'] >= threshold],
            html_url='/documents/'+row['version']+'.html#'+quote(row['section_id'],safe=''))
        result.append(out)
    def ranked(method):
        ordered = sorted(result,key=lambda r:(-r[method+'_score'],-r['weighted_score'],r['id']))[:limit]
        return [{**r,'score':r[method+'_score'],'fusion':method} for r in ordered]
    if compare:
        return {'weighted':ranked('weighted'),'rrf':ranked('rrf'),'queries':queries,
                'threshold_basis':'weighted_score', 'rrf_constant':60}
    return ranked(fusion)

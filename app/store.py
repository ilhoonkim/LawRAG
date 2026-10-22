"""Persistent SQLite vector storage (sqlite-vec cosine distance) and hybrid retrieval."""
from datetime import date, datetime, timezone
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import quote
import sqlite_vec
from .temporal import today_in_korea
from .references import annex_references, article_links
from .laws import parse_law, parse_admrul, render_html, SetupError


# 저장소는 원문 XML/HTML 파일과 SQLite를 함께 관리합니다.
# 현재 검색 경로는 search_enhanced() → retrieval.py, 문맥 확장은 expand_context() → graph.py입니다.
# 아래 search()/expand_references()는 기존 구현이므로 현재 검색 경로와 구분해서 읽어 주세요.
def grams(value):
    value = re.sub(r'[^가-힣a-z0-9]', '', value.lower())
    return {value[i:i+2] for i in range(len(value)-1)}


def expanded_query(question):
    if any(word in question for word in ['적용되지', '적용되지 않는', '적용 안', '적용 제외', '적용제외', '적용하지']):
        return question + ' 적용범위 적용 제외 아니한다'
    return question


def body_text(row):
    # API body often repeats the heading. Remove it from body scoring/embedding.
    return re.sub(r'^\s*제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]*\))?\s*', '', row['body'])


def article_vectors(rows, law_name, provider, *, keyword_only=False):
    """본문 없는 이동 조문은 보관만 하고, 본문 있는 조문의 제목·내용만 색인합니다."""
    bodies = [body_text(r) for r in rows]
    positions = [i for i, body in enumerate(bodies) if body.strip()]
    vectors, title_vectors = [None]*len(rows), [None]*len(rows)
    if keyword_only or not positions:
        return vectors, title_vectors
    texts = [bodies[i] for i in positions]
    titles = [f'{law_name} - {rows[i]["number"]}({rows[i]["title"]})' for i in positions]
    # 위치를 따로 보존하므로 중간 조문을 제외해도 다음 조문의 벡터가 밀리지 않습니다.
    embeddings = provider.embed(texts + titles)
    if len(embeddings) != 2*len(positions):
        raise ValueError('임베딩 개수가 조문 제목·본문 입력 개수와 다릅니다.')
    if len({len(v) for v in embeddings}) != 1:
        raise ValueError('임베딩 차원이 일치하지 않습니다.')
    for offset, position in enumerate(positions):
        vectors[position] = embeddings[offset]
        title_vectors[position] = embeddings[len(positions)+offset]
    return vectors, title_vectors


def lexical_components(question, row):
    query = expanded_query(question.replace(row['law_name'], ''))
    query = re.sub(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일', '', query)
    q = grams(query)
    if not q:
        return 0., 0.
    title = grams(row['title'])
    title_score = .5 * len(q & title) / len(q) + .5 * len(q & title) / max(len(title), 1)
    body_score = len(q & grams(body_text(row))) / len(q)
    return min(1., title_score), min(1., body_score)


def combined_score(title, body, title_weight=.35, min_title=.15, min_body=.20):
    if title < min_title or body < min_body:
        return None
    return title_weight * title + (1-title_weight) * body


class Store:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        (directory / 'html').mkdir(exist_ok=True)
        (directory / 'raw').mkdir(exist_ok=True)
        self.db = directory / 'laws.sqlite3'
        # law_versions/version_articles는 모든 수집 버전, laws/articles는 활성 버전용 테이블입니다.
        # revision_catalog는 API에서 조회한 연혁일 뿐 본문 수집 완료를 뜻하지 않습니다.
        # 검색 벡터는 조문 제목·본문만 저장하고, 참조·위임 관계는 별도 테이블로 관리합니다.
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS laws (id TEXT PRIMARY KEY, name TEXT, effective TEXT, version TEXT, html TEXT, source_url TEXT);
              CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, law_id TEXT REFERENCES laws(id), law_name TEXT,
                number TEXT, title TEXT, body TEXT, section_id TEXT, vector BLOB, model TEXT);
              CREATE INDEX IF NOT EXISTS articles_law ON articles(law_id);
              CREATE TABLE IF NOT EXISTS annex_nodes (
                version TEXT, number TEXT, title TEXT, body TEXT, section_id TEXT, source_url TEXT,
                PRIMARY KEY(version,number));
              CREATE TABLE IF NOT EXISTS article_references (
                version TEXT, article TEXT, target TEXT, target_law TEXT, relation TEXT,
                PRIMARY KEY(version,article,target,target_law));
              CREATE TABLE IF NOT EXISTS revision_catalog (
                kind TEXT, mst TEXT, name TEXT, effective TEXT, promulgated TEXT,
                PRIMARY KEY(kind,mst,effective));
              CREATE TABLE IF NOT EXISTS law_versions (
                version TEXT PRIMARY KEY, law_id TEXT, name TEXT, kind TEXT, mst TEXT,
                promulgated TEXT, effective TEXT, collected_at TEXT, html TEXT, source_url TEXT);
              CREATE INDEX IF NOT EXISTS versions_source ON law_versions(kind, mst);
              CREATE TABLE IF NOT EXISTS delegation_snapshots (
                mst TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '', fetched_at TEXT,
                status TEXT, reason TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS delegation_sources (
                version TEXT PRIMARY KEY, mst TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS delegation_edges (
                snapshot_mst TEXT, from_version TEXT, from_article TEXT,
                to_version TEXT, to_article TEXT, relation TEXT, provenance TEXT,
                PRIMARY KEY(snapshot_mst,from_version,from_article,to_version,to_article,relation));
              CREATE INDEX IF NOT EXISTS delegation_from ON delegation_edges(from_version,from_article);
              CREATE INDEX IF NOT EXISTS delegation_to ON delegation_edges(to_version,to_article);
              CREATE TABLE IF NOT EXISTS delegation_unresolved (
                snapshot_mst TEXT, position INTEGER, payload TEXT, reason TEXT,
                PRIMARY KEY(snapshot_mst,position));
              CREATE TABLE IF NOT EXISTS version_articles (
                version TEXT REFERENCES law_versions(version), position INTEGER, number TEXT, title TEXT,
                body TEXT, section_id TEXT, chapter TEXT, vector BLOB, model TEXT,
                PRIMARY KEY(version, position));
              INSERT OR IGNORE INTO law_versions
                SELECT version, id, name, 'law', '', '', effective, '', html, source_url FROM laws;
              INSERT OR IGNORE INTO version_articles(version,position,number,title,body,section_id,chapter,vector,model)
                SELECT l.version, a.rowid, a.number, a.title, a.body, a.section_id, '', a.vector, a.model
                FROM articles a JOIN laws l ON a.law_id=l.id
                WHERE NOT EXISTS (SELECT 1 FROM version_articles v WHERE v.version=l.version);

            ''')
            # 동일 MST라도 시행일별 버전이 다를 수 있어 연혁의 복합 키에 시행일을 포함합니다.
            # 기존 DB는 필요한 스키마만 보완하며 저장된 법령을 다시 다운로드하지 않습니다.
            catalog_pk = [r['name'] for r in db.execute('PRAGMA table_info(revision_catalog)') if r['pk']]
            if 'effective' not in catalog_pk:
                db.executescript('''
                    BEGIN IMMEDIATE;
                    ALTER TABLE revision_catalog RENAME TO revision_catalog_legacy;
                    CREATE TABLE revision_catalog (kind TEXT,mst TEXT,name TEXT,effective TEXT,promulgated TEXT,
                        PRIMARY KEY(kind,mst,effective));
                    INSERT INTO revision_catalog SELECT kind,mst,name,coalesce(effective,''),promulgated FROM revision_catalog_legacy;
                    DROP TABLE revision_catalog_legacy;
                    COMMIT;
                ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(version_articles)')}
            if 'title_vector' not in columns:
                db.execute('ALTER TABLE version_articles ADD COLUMN title_vector BLOB')

    # 연결을 스레드 사이에서 공유하지 않습니다. 각 with 블록은 정상 종료 시 commit,
    # 예외 시 rollback 후 연결을 닫습니다. 외부 API 호출은 가능한 한 쓰기 트랜잭션 밖에서 합니다.
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function('compact_name', 1, lambda value: re.sub(r'\s+', '', value or ''))
        db.execute('PRAGMA foreign_keys=ON')
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        try:
            with db:
                yield db
        finally:
            db.close()

    # 외부 MST와 달리 내부 version은 응답 원문 해시입니다.
    # 이 해시로 DB 버전과 보관 XML/HTML을 연결해 과거 답변의 출처를 다시 열 수 있습니다.
    def archive_raw(self, raw):
        filename = hashlib.sha256(raw).hexdigest()[:20] + '.xml'
        (self.directory / 'raw' / filename).write_bytes(raw)
        return filename

    def ingest(self, raw, provider, keyword_only=False, *, mst='', kind='law', archive_only=False):
        law = parse_admrul(raw) if kind == 'admrul' else parse_law(raw)
        with self.connect() as db:
            existing = {r[0] for r in db.execute('SELECT DISTINCT model FROM articles WHERE vector IS NOT NULL AND law_id != ?', (law.id,))}
        if not archive_only and not keyword_only and existing - {provider.fingerprint}:
            raise SetupError('임베딩 모델이 기존 DB와 다릅니다. 별도 DATA_DIR로 전체 법령을 재색인하세요.')
        # 수집 단계에서는 원문 제목·본문만 임베딩합니다. 채팅용 LLM은 호출하지 않습니다.
        # 별표·별지 서식은 law.articles에 포함되지 않으므로 여기서 임베딩하지 않습니다.
        vectors, title_vectors = article_vectors([vars(a) for a in law.articles], law.name, provider,
                                                 keyword_only=keyword_only)
        filename = f'{law.version}.html'
        # Immutable artifacts written before the DB transaction. Old citations survive refresh.
        self.archive_raw(raw)
        (self.directory / 'html' / filename).write_text(
            render_html(law, reference_pending=kind=='law'), encoding='utf-8')
        packed = [(i, a, sqlite_vec.serialize_float32(v) if v is not None else None,
                   provider.fingerprint if v is not None else None)
                  for i, (a, v) in enumerate(zip(law.articles, vectors))]
        # 한 버전의 조문·벡터·참조 관계는 같은 DB 트랜잭션으로 갱신합니다.
        # 위에서 보관한 파일은 DB 트랜잭션에 포함되지 않으며 실패 시 진단용으로 남을 수 있습니다.
        with self.connect() as db:
            db.execute('DELETE FROM version_articles WHERE version=?', (law.version,))
            db.execute('INSERT OR REPLACE INTO law_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (law.version, law.id, law.name, kind, mst, law.promulgated, law.effective,
                 datetime.now(timezone.utc).isoformat(), filename, law.source_url))
            db.executemany('INSERT INTO version_articles(version,position,number,title,body,section_id,chapter,vector,model) VALUES(?,?,?,?,?,?,?,?,?)', [
                (law.version, i, a.number, a.title, a.body, a.section_id, a.chapter, v, model)
                for i, a, v, model in packed])
            db.executemany('UPDATE version_articles SET title_vector=? WHERE version=? AND position=?',
                [(sqlite_vec.serialize_float32(v) if v is not None else None, law.version, i) for i,v in enumerate(title_vectors)])
            db.execute('DELETE FROM annex_nodes WHERE version=?', (law.version,))
            db.execute('DELETE FROM article_references WHERE version=?', (law.version,))
            db.executemany('INSERT INTO annex_nodes VALUES(?,?,?,?,?,?)',
                [(law.version, a.number, a.title, a.body, a.section_id, a.source_url) for a in law.annexes])
            db.executemany('INSERT INTO article_references VALUES(?,?,?,?,?)',
                [(law.version, a.number, target, target_law, 'references_annex')
                 for a in law.articles for target, target_law in annex_references(a.body, law.name)])
            db.executemany('INSERT INTO article_references VALUES(?,?,?,?,?)',
                [(law.version, a.number, target, target_law, 'references_article')
                 for a in law.articles for target, target_law in article_links(a.body, law.name, a.number)])
            # Refresh an already active version, but do not activate newly collected revisions.
            active = db.execute('SELECT 1 FROM laws WHERE version=?', (law.version,)).fetchone()
            if not archive_only or active:
                self._activate(db, law.version)
        return {'name': law.name, 'articles': len(law.articles), 'version': law.version,
                'mode': 'keyword' if keyword_only else 'hybrid', 'html_url': '/documents/' + filename}

    def _activate(self, db, version):
        law = db.execute('SELECT * FROM law_versions WHERE version=?', (version,)).fetchone()
        if law is None:
            raise ValueError('수집한 버전이 없습니다.')
        models = {r[0] for r in db.execute('SELECT DISTINCT model FROM version_articles WHERE version=? AND vector IS NOT NULL', (version,))}
        existing = {r[0] for r in db.execute('SELECT DISTINCT model FROM articles WHERE law_id!=? AND vector IS NOT NULL', (law['law_id'],))}
        if models and existing and models != existing:
            raise SetupError('기존 채팅 색인과 임베딩 모델이 다릅니다. 동일 모델로 재수집하세요.')
        db.execute('DELETE FROM articles WHERE law_id=?', (law['law_id'],))
        db.execute('INSERT OR REPLACE INTO laws VALUES(?,?,?,?,?,?)',
                   (law['law_id'], law['name'], law['effective'], version, law['html'], law['source_url']))
        db.execute('''INSERT INTO articles
            SELECT ? || ':' || number, ?, ?, number, title, body, section_id, vector, model
            FROM version_articles WHERE version=?''', (law['law_id'], law['law_id'], law['name'], version))

    def activate_version(self, version):
        with self.connect() as db:
            self._activate(db, version)


    @staticmethod
    def _date(value):
        try:
            return datetime.strptime(value, '%Y%m%d').date()
        except (ValueError, TypeError):
            return None

    # 시행일이 기준일 이하인 것 중 가장 최신 버전을 선택합니다.
    # 다음 개정의 시행 전까지는 마지막 버전이 계속 적용되므로 별도 종료일은 요구하지 않습니다.
    # 단, API 연혁상 이미 시행된 더 최신 버전이 미수집이면 오래된 버전으로 대신 답하지 않습니다.
    def temporal_versions(self, as_of=None):
        cutoff = as_of or today_in_korea()
        with self.connect() as db:
            versions = [dict(r) for r in db.execute('SELECT * FROM law_versions')]
            catalog = [dict(r) for r in db.execute('SELECT * FROM revision_catalog')]
        def eligible(row):
            effective = self._date(row['effective'])
            promulgated = self._date(row['promulgated'])
            return effective is not None and effective <= cutoff and (promulgated is None or promulgated <= cutoff)
        def order(row):
            return (row['effective'], row['promulgated'] or '', (row.get('mst') or '').zfill(20), row.get('collected_at',''))
        chosen, known = {}, {}
        for row in versions:
            key = (row['kind'], row['name'])
            if eligible(row) and (key not in chosen or order(row) > order(chosen[key])):
                chosen[key] = row
        for row in catalog:
            key = (row['kind'], row['name'])
            if eligible(row) and (key not in known or order(row) > order(known[key])):
                known[key] = row
        missing = []
        for key, row in known.items():
            selected = chosen.get(key)
            if selected is None or (row['effective'], row['promulgated'] or '') > (selected['effective'], selected['promulgated'] or ''):
                chosen.pop(key, None)
                missing.append(row)
        return {'as_of': cutoff.isoformat(), 'versions': list(chosen.values()), 'missing': missing,
                'note': '수집한 버전과 조회한 API 연혁을 기준으로 선택합니다. 미조회 개정의 존재 여부는 별도 확인이 필요합니다.'}

    # 현재 기준일에 선택 가능한 버전 목록입니다. 수집된 모든 과거 버전의 총목록은
    # 관리 API의 revisions(source='collected')에서 조회합니다.
    def list_laws(self):
        selected = self.temporal_versions()['versions']
        result = []
        with self.connect() as db:
            for law in selected:
                count = db.execute('SELECT count(*),count(vector) FROM version_articles WHERE version=?', (law['version'],)).fetchone()
                result.append({**law, 'id': law['law_id'], 'article_count': count[0], 'embedded_count': count[1]})
        return sorted(result, key=lambda r: r['name'])

    def expand_references(self, evidence):
        expanded = [dict(e) for e in evidence]
        missing = []
        seen = {(e['version'],e['number'],e['law_name']) for e in evidence}
        with self.connect() as db:
            for article in list(expanded):
                stored = db.execute('SELECT target,target_law,relation FROM article_references WHERE version=? AND article=?',
                                    (article['version'],article['number'])).fetchall()
                refs = [tuple(r) for r in stored] or [
                    (number, law, relation) for relation, links in [
                        ('references_annex',annex_references(article['body'],article['law_name'])),
                        ('references_article',article_links(article['body'],article['law_name'],article['number']))]
                    for number,law in links]
                titles = []
                for number, target_law, relation in refs:
                    is_annex = relation == 'references_annex'
                    table = 'annex_nodes' if is_annex else 'version_articles'
                    node = db.execute(f'SELECT * FROM {table} WHERE version=? AND number=?', (article['version'],number)).fetchone() if target_law == article['law_name'] else None
                    if node and node['title']:
                        titles.append({'number':number,'title':node['title'],'law_name':target_law})
                    key = (article['version'],number,target_law)
                    if key in seen:
                        continue
                    seen.add(key)
                    if node is None or not node['body'].strip():
                        missing.append({'law_name':target_law,'number':number,'from_article':article['number'],
                            'version':article['version'],'reason':'다른 법령의 참조 연결은 확인이 필요합니다.' if target_law!=article['law_name'] else '참조 본문이 미수집 상태이거나 파일 추출이 필요합니다.'})
                        continue
                    expanded.append({**article,'id':article['version']+':'+number,'number':number,'title':node['title'],
                        'body':node['body'],'section_id':node['section_id'],'kind':'annex' if is_annex else 'article',
                        'relation':relation,'referenced_by':article['number'],
                        'source_url':(node['source_url'] if is_annex else '') or article['source_url'],
                        'html_url':'/documents/'+article['version']+'.html#'+quote(node['section_id'],safe='')})
                article['reference_titles'] = titles
        return expanded, missing

    def search_enhanced(self, question, provider, **kwargs):
        from .retrieval import search
        return search(self, question, provider, **kwargs)

    def expand_context(self, evidence, **kwargs):
        from .graph import expand
        return expand(self, evidence, **kwargs)

    # 기존 검색 경로: 제목/본문 최소값과 가중합을 사용합니다.
    # 채팅의 제목·본문 검색·필드별 RRF를 수정하려면 retrieval.py를 확인하세요.
    def search(self, question, provider, threshold=.48, limit=5, mode='hybrid', as_of=None, title_weight=.35, min_title=.15, min_body=.20, query_variants=None):
        if not 0 <= threshold <= 1:
            raise ValueError('임계치는 0~1이어야 합니다.')
        if not all(0 <= n <= 1 for n in [title_weight,min_title,min_body]):
            raise ValueError('검색 가중치와 최소 점수는 0~1이어야 합니다.')
        selected = self.temporal_versions(as_of)['versions']
        with self.connect() as db:
            rows = []
            for law in selected:
                for item in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position', (law['version'],)):
                    row = dict(item)
                    if not body_text(row).strip():
                        continue
                    row.update(id=law['version']+':'+str(row['position']),law_id=law['law_id'],law_name=law['name'],
                        effective=law['effective'],promulgated=law['promulgated'],html=law['html'],source_url=law['source_url'])
                    rows.append(row)
            if not rows:
                return []
            # A named law unavailable on the date must not fall back to a different law.
            names = [r[0] for r in db.execute('SELECT name FROM law_versions UNION SELECT name FROM revision_catalog')]
            named_laws = [name for name in names if name in question]
            if named_laws:
                longest = max(named_laws, key=len)
                rows = [r for r in rows if r['law_name'] == longest]
            similarities = {}
            queries = list(dict.fromkeys([expanded_query(question)] + [expanded_query(q) for q in (query_variants or [])]))
            if mode == 'hybrid' and rows:
                if any(r['model'] != provider.fingerprint or r['vector'] is None or r['title_vector'] is None for r in rows):
                    raise SetupError('벡터가 없거나 임베딩 모델이 변경되었습니다. 버전 상세에서 제목·본문 재색인을 실행하거나 키워드 모드를 선택하세요.')
                vectors = provider.embed(queries)
                if len(vectors) != len(queries) or any(
                    len(r['vector']) != 4 * len(vector) or len(r['title_vector']) != 4 * len(vector)
                    for r in rows for vector in vectors):
                    raise SetupError('임베딩 차원이 다릅니다. 새 DATA_DIR에서 재색인하세요.')
                def compare(vector, row):
                    return tuple(db.execute('SELECT 1 - vec_distance_cosine(?,?), 1 - vec_distance_cosine(?,?)',
                        (row['title_vector'], vector, row['vector'], vector)).fetchone())
                packed = [sqlite_vec.serialize_float32(v) for v in vectors]
                similarities = {row['id']:[compare(v,row) for v in packed] for row in rows}
                if query_variants is None and callable(getattr(provider, 'search_queries', None)):
                    # A short semantic shortlist supplies the actual vocabulary
                    # of this corpus without requiring a user-provided law name.
                    shortlist = sorted(rows, key=lambda r: title_weight*similarities[r['id']][0][0]
                                       + (1-title_weight)*similarities[r['id']][0][1], reverse=True)[:12]
                    candidates = [{k:r[k] for k in ('law_name','number','title')} | {'body':body_text(r)[:600]}
                                  for r in shortlist]
                    additions = [q for q in provider.search_queries(question,candidates=candidates) if q not in queries]
                    if additions:
                        extra_vectors = provider.embed(additions)
                        if len(extra_vectors) != len(additions) or any(len(v) != len(vectors[0]) for v in extra_vectors):
                            raise ValueError('검색어 임베딩 응답의 개수 또는 차원이 다릅니다.')
                        queries.extend(additions)
                        packed = [sqlite_vec.serialize_float32(v) for v in extra_vectors]
                        for row in rows:
                            similarities[row['id']].extend(compare(vector,row) for vector in packed)
        results = []
        for row in rows:
            matches = []
            for query, semantic in zip(queries, similarities.get(row['id'], [(0.,0.)] * len(queries))):
                title_lexical, body_lexical = lexical_components(query, row)
                title_semantic, body_semantic = [max(0.,min(1.,v)) for v in semantic]
                title_score = .65*title_semantic + .35*title_lexical if mode == 'hybrid' else title_lexical
                body_score = .65*body_semantic + .35*body_lexical if mode == 'hybrid' else body_lexical
                score = combined_score(title_score,body_score,title_weight,min_title,min_body)
                if score is not None and score >= threshold:
                    matches.append((score,title_score,body_score,title_lexical,body_lexical,title_semantic,body_semantic,query))
            if not matches:
                continue
            # Keep title/body scores from the SAME query, not unrelated maxima.
            score,title_score,body_score,title_lexical,body_lexical,title_semantic,body_semantic,query = max(matches, key=lambda m:m[0])
            row.pop('vector'); row.pop('title_vector'); row.pop('model')
            row.update(score=round(score,4),title_score=round(title_score,4),body_score=round(body_score,4),
                title_keyword_score=round(title_lexical,4),body_keyword_score=round(body_lexical,4),
                title_semantic_score=round(title_semantic,4) if mode=='hybrid' else None,
                body_semantic_score=round(body_semantic,4) if mode=='hybrid' else None,
                matched_query=query,
                html_url='/documents/' + row.pop('html') + '#' + quote(row['section_id'],safe=''))
            results.append(row)
        return sorted(results,key=lambda r:(-r['score'],r['id']))[:limit]

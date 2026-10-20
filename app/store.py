"""Persistent SQLite vector storage (sqlite-vec cosine distance) and hybrid retrieval."""
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import quote
import sqlite_vec
from .laws import parse_law, render_html, SetupError


def grams(value):
    value = re.sub(r'[^가-힣a-z0-9]', '', value.lower())
    return {value[i:i+2] for i in range(len(value)-1)}


def expanded_query(question):
    if any(word in question for word in ['적용되지', '적용되지 않는', '적용 안', '적용 제외', '적용제외', '적용하지']):
        return question + ' 적용범위 적용 제외 아니한다'
    return question


def lexical_score(question, row):
    # Remove an explicitly named law before matching substance, so its name alone
    # cannot turn every article of that law into good evidence.
    query = expanded_query(question.replace(row['law_name'], ''))
    q = grams(query)
    if not q:
        return 0.0
    body = grams(row['title'] + ' ' + row['body'])
    coverage = len(q & body) / len(q)
    title = grams(row['title'])
    title_match = len(q & title) / max(len(title), 1)
    score = .7 * coverage + .3 * title_match
    if row['law_name'] in question:
        score += .08 * score
    return min(1., score)


class Store:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        (directory / 'html').mkdir(exist_ok=True)
        (directory / 'raw').mkdir(exist_ok=True)
        self.db = directory / 'laws.sqlite3'
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS laws (id TEXT PRIMARY KEY, name TEXT, effective TEXT, version TEXT, html TEXT, source_url TEXT);
              CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, law_id TEXT REFERENCES laws(id), law_name TEXT,
                number TEXT, title TEXT, body TEXT, section_id TEXT, vector BLOB, model TEXT);
              CREATE INDEX IF NOT EXISTS articles_law ON articles(law_id);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, raw, provider, keyword_only=False):
        law = parse_law(raw)
        with self.connect() as db:
            existing = {r[0] for r in db.execute('SELECT DISTINCT model FROM articles WHERE vector IS NOT NULL AND law_id != ?', (law.id,))}
        if not keyword_only and existing - {provider.fingerprint}:
            raise SetupError('임베딩 모델이 기존 DB와 다릅니다. 별도 DATA_DIR로 전체 법령을 재색인하세요.')
        texts = [f'{law.name} {a.number} {a.title}\n{a.body}' for a in law.articles]
        vectors = [None] * len(texts) if keyword_only else provider.embed(texts)
        if len(vectors) != len(texts):
            raise ValueError('임베딩 개수가 조문 개수와 다릅니다.')
        if not keyword_only and len({len(v) for v in vectors}) != 1:
            raise ValueError('임베딩 차원이 일치하지 않습니다.')
        filename = f'{law.version}.html'
        # Immutable artifacts written before the DB transaction. Old citations survive refresh.
        (self.directory / 'raw' / f'{law.version}.xml').write_bytes(raw)
        (self.directory / 'html' / filename).write_text(render_html(law), encoding='utf-8')
        with self.connect() as db:
            db.execute('DELETE FROM articles WHERE law_id=?', (law.id,))
            db.execute('INSERT OR REPLACE INTO laws VALUES(?,?,?,?,?,?)',
                       (law.id, law.name, law.effective, law.version, filename, law.source_url))
            db.executemany('INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?)', [
                (f'{law.id}:{a.number}', law.id, law.name, a.number, a.title, a.body, a.section_id,
                 sqlite_vec.serialize_float32(v) if v is not None else None,
                 provider.fingerprint if v is not None else None)
                for a, v in zip(law.articles, vectors)])
        return {'name': law.name, 'articles': len(law.articles), 'mode': 'keyword' if keyword_only else 'hybrid', 'html_url': '/documents/' + filename}

    def list_laws(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT l.*, count(a.id) article_count,
                count(a.vector) embedded_count FROM laws l JOIN articles a ON l.id=a.law_id GROUP BY l.id ORDER BY l.name''')]

    def search(self, question, provider, threshold=.48, limit=5, mode='hybrid'):
        if not 0 <= threshold <= 1:
            raise ValueError('임계치는 0~1이어야 합니다.')
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('''SELECT a.*, l.effective, l.html, l.source_url
                FROM articles a JOIN laws l ON a.law_id=l.id''')]
            if not rows:
                return []
            named_laws = [r['law_name'] for r in rows if r['law_name'] in question]
            if named_laws:
                longest = max(named_laws, key=len)
                rows = [r for r in rows if r['law_name'] == longest]
            similarities = {}
            if mode == 'hybrid':
                if any(r['model'] != provider.fingerprint or r['vector'] is None for r in rows):
                    raise SetupError('벡터가 없거나 임베딩 모델이 변경되었습니다. 법령을 임베딩 포함으로 다시 수집하거나 키워드 모드를 선택하세요.')
                vector = provider.embed([expanded_query(question)])[0]
                if any(len(r['vector']) != 4 * len(vector) for r in rows):
                    raise SetupError('임베딩 차원이 다릅니다. 새 DATA_DIR에서 재색인하세요.')
                similarities = dict(db.execute('''SELECT id, 1 - vec_distance_cosine(vector, ?)
                    FROM articles WHERE vector IS NOT NULL AND model=?''',
                    (sqlite_vec.serialize_float32(vector), provider.fingerprint)))
        results = []
        for row in rows:
            lexical = lexical_score(question, row)
            semantic = max(0., min(1., similarities.get(row['id'], 0.)))
            score = .65 * semantic + .35 * lexical if mode == 'hybrid' else lexical
            if score < threshold:
                continue
            row.pop('vector'); row.pop('model')
            row.update(score=round(score, 4), lexical_score=round(lexical, 4), semantic_score=round(semantic, 4) if mode == 'hybrid' else None,
                html_url='/documents/' + row.pop('html') + '#' + quote(row['section_id'], safe=''))
            results.append(row)
        return sorted(results, key=lambda r: (-r['score'], r['id']))[:limit]

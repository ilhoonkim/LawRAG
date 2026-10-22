"""Category, version and explicit collection plans, including official law families."""
from datetime import datetime, timezone, timedelta
import json
import threading
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field
from .laws import SetupError, parse_law, parse_admrul
from .temporal import today_in_korea
from .catalog import register_catalog, normalize_name

Kind = Literal['law', 'admrul']


def now():
    return datetime.now(timezone.utc).isoformat()


class CategoryInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    kind: Kind = 'law'


class CollectionItem(BaseModel):
    mst: str = Field(pattern=r'^\d{1,20}$')
    kind: Kind = 'law'
    name: str = Field(min_length=1, max_length=200)
    promulgated: str = Field(default='', max_length=20)
    effective: str = Field(default='', max_length=20)


# 관리 화면의 수집은 본문 저장부터 임베딩까지 한 작업으로 취급합니다.
# keyword_only는 요청 호환성을 위해 남아 있지만 이 경로에서는 False만 허용됩니다.
class CollectionInput(BaseModel):
    items: list[CollectionItem] = Field(min_length=1, max_length=30)
    keyword_only: Literal[False] = False


class PlannedCollectionInput(BaseModel):
    plan_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class Management:
    def __init__(self, store, provider, api, *, recover_pending=True):
        self.store, self.provider, self.api = store, provider, api
        # 이 잠금은 한 프로세스 안에서 수집을 직렬화합니다. 분산 작업 큐가 아니므로
        # serve.py는 worker를 하나로 실행합니다. DB의 이력은 진행 중에도 조회할 수 있습니다.
        self.worker_lock = threading.Lock()
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY, name TEXT, kind TEXT, UNIQUE(name,kind));
                CREATE TABLE IF NOT EXISTS collection_history (
                    id TEXT PRIMARY KEY, batch TEXT, mst TEXT, kind TEXT, name TEXT,
                    promulgated TEXT, effective TEXT, status TEXT, requested_at TEXT,
                    started_at TEXT, finished_at TEXT, reason TEXT, version TEXT, keyword_only INTEGER);
                CREATE INDEX IF NOT EXISTS collection_source ON collection_history(kind,mst,requested_at);
                CREATE TABLE IF NOT EXISTS collection_plans (
                    id TEXT PRIMARY KEY, created_at TEXT, payload TEXT, batch TEXT);
                CREATE TABLE IF NOT EXISTS collection_batch_plans (
                    batch TEXT PRIMARY KEY, plan_id TEXT);
                CREATE TABLE IF NOT EXISTS law_hierarchy_snapshots (
                    mst TEXT, effective TEXT, fetched_at TEXT, payload TEXT,
                    PRIMARY KEY(mst,effective));
            ''')
            register_catalog(db)
            for row in db.execute('SELECT DISTINCT name,kind FROM law_versions').fetchall():
                if not db.execute('SELECT 1 FROM categories WHERE kind=? AND compact_name(name)=?', (row['kind'],normalize_name(row['name']))).fetchone():
                    db.execute('INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)', (row['name'],row['kind']))
            # 서버 재시작 시 이전 프로세스의 대기/실행 작업을 자동 재실행하지 않습니다.
            # 중단으로 기록해 사용자가 재수집할 수 있게 하고 조회 전용 CLI에서는 이 처리를 끕니다.
            if recover_pending:
                db.execute("UPDATE collection_history SET status='interrupted', reason='서버가 종료되어 수집이 중단되었습니다. 다시 수집해 주세요.', finished_at=? WHERE status IN ('queued','running')", (now(),))

    def categories(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM categories ORDER BY kind DESC, name')]

    def add_category(self, name, kind):
        name = name.strip()
        if len(name) < 2:
            raise ValueError('법령명을 두 글자 이상 입력하세요.')
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)', (name, kind))
        return self.categories()

    # API 연혁 한 건에 DB의 수집 버전과 마지막 작업 상태를 붙여 화면에 전달합니다.
    # '현재 적용'은 단순 활성 플래그가 아니라 기준일 버전 선택 결과로 판단합니다.
    def decorate(self, item):
        item = dict(item)
        with self.store.connect() as db:
            version = db.execute('''SELECT v.*, EXISTS(SELECT 1 FROM laws l WHERE l.version=v.version) active
                FROM law_versions v WHERE kind=? AND mst=? AND (?='' OR effective=?) ORDER BY collected_at DESC LIMIT 1''', (item['kind'], item['id'],item.get('effective',''),item.get('effective',''))).fetchone()
            event = db.execute("SELECT status,reason FROM collection_history WHERE kind=? AND mst=? AND (?='' OR effective=?) ORDER BY requested_at DESC LIMIT 1", (item['kind'], item['id'],item.get('effective',''),item.get('effective',''))).fetchone()
        active_versions = {r['version'] for r in self.store.temporal_versions()['versions']}
        item.update(version=version['version'] if version else None,
                    collected_at=version['collected_at'] if version else '',
                    active=bool(version and version['version'] in active_versions),
                    collection_status=event['status'] if event else ('completed' if version else 'uncollected'),
                    reason=event['reason'] if event else '')
        return item

    # API 검색 결과에는 시행령·시행규칙 같은 유사 이름도 섞입니다. 전체 페이지를 확인한 뒤
    # 정확한 법령명과 시행일로 걸러야 건수와 UI 페이지가 실제 개정 버전을 나타냅니다.
    def revisions(self, query, kind, page, source):
        if source == 'api':
            # Filter the full API result before pagination, so 시행령/시행규칙
            # cannot inflate this law's version count or create empty pages.
            rows, seen = {}, set()
            for remote_page in range(1, 201):
                result = self.api.search(query, remote_page, kind=kind, revisions=True, display=100)
                items = result['laws']
                signature = tuple((r['id'],r['effective']) for r in items)
                if (not items and (remote_page-1)*100 < int(result['total'])) or (items and signature in seen):
                    raise ValueError('API 연혁 목록의 페이지가 누락되거나 반복되었습니다.')
                seen.add(signature)
                for row in items:
                    if normalize_name(row['name']) == normalize_name(query) and self.collectible_date(row['effective']):
                        rows[(row['id'],row['effective'])] = row
                if remote_page*100 >= int(result['total']):
                    break
            else:
                raise ValueError('연혁 조회 범위를 초과했습니다.')
            ordered = sorted(rows.values(), key=lambda r:(r['effective'],r.get('promulgated',''),r['id']), reverse=True)
            result = {'laws': ordered, 'total': len(ordered)}
            with self.store.connect() as db:
                db.executemany('INSERT OR REPLACE INTO revision_catalog VALUES(?,?,?,?,?)',
                    [(kind, r['id'], r['name'], r['effective'], r.get('promulgated','')) for r in result['laws']])
            return {'total': int(result['total']), 'page': page,
                    'items': [self.decorate(item) for item in result['laws'][(page-1)*10:page*10]]}
        with self.store.connect() as db:
            where = 'kind=? AND instr(compact_name(name),?)>0'
            args = (kind, normalize_name(query))
            total = db.execute(f'SELECT count(*) FROM law_versions WHERE {where}', args).fetchone()[0]
            rows = db.execute(f'''SELECT v.*, EXISTS(SELECT 1 FROM laws l WHERE l.version=v.version) active
                FROM law_versions v WHERE {where} ORDER BY effective DESC, promulgated DESC, collected_at DESC
                LIMIT 10 OFFSET ?''', (*args, (page-1)*10)).fetchall()
            items = [dict(id=r['mst'], kind=r['kind'], name=r['name'], promulgated=r['promulgated'],
                          effective=r['effective'], status='', version=r['version'], collected_at=r['collected_at'],
                          active=bool(r['active']), collection_status='completed', reason='') for r in rows]
        active_versions = {r['version'] for r in self.store.temporal_versions()['versions']}
        for item in items:
            item['active'] = item['version'] in active_versions
        return {'total': total, 'page': page, 'items': items}

    def collectible_date(self, effective):
        if not effective or len(effective) != 8 or not effective.isdigit():
            return False
        value = self.store._date(effective)
        return value is not None and value <= today_in_korea()

    def require_collection_keys(self):
        if hasattr(self.api, 'oc') and not self.api.oc:
            raise SetupError('.env의 LAW_API_OC를 입력하고 서버를 재시작하세요.')
        settings = getattr(self.provider, 'settings', None)
        if settings is not None and not settings.api_key:
            raise SetupError('.env의 LLM_API_KEY를 입력하고 서버를 재시작하세요. 수집은 임베딩까지 완료해야 합니다.')

    def plan_collection(self, request):
        from .hierarchy import build_plan
        plan = build_plan(self, request.items)
        plan_id = uuid4().hex
        with self.store.connect() as db:
            db.execute('INSERT INTO collection_plans VALUES(?,?,?,?)',
                       (plan_id, now(), json.dumps(plan, ensure_ascii=False), ''))
            for family in plan['families']:
                for member in [family, *family['members']]:
                    db.execute('INSERT OR REPLACE INTO law_hierarchy_snapshots VALUES(?,?,?,?)',
                        (member['mst'], member['effective'], now(), json.dumps(family, ensure_ascii=False)))
        return {'plan_id': plan_id, **plan}

    def queue_plan(self, plan_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM collection_plans WHERE id=?', (plan_id,)).fetchone()
        if row is None or datetime.fromisoformat(row['created_at']) < datetime.now(timezone.utc) - timedelta(hours=1):
            raise ValueError('수집 계획이 만료되었습니다. 상하위법을 다시 확인해 주세요.')
        plan = json.loads(row['payload'])
        # 서버가 만든 계획만 확장된 크기를 허용합니다. 각 항목은 다시 검증하고,
        # 실제 중복 여부는 queue의 쓰기 트랜잭션 안에서 재확인합니다.
        request = CollectionInput.model_construct(items=[CollectionItem(**r) for r in plan['items']], keyword_only=False)
        result = self.queue(request)
        with self.store.connect() as db:
            db.execute('UPDATE collection_plans SET batch=? WHERE id=?', (result['batch'], plan_id))
            db.execute('INSERT INTO collection_batch_plans VALUES(?,?)', (result['batch'], plan_id))
        return {**result, 'plan_id': plan_id, 'warnings': plan['warnings']}

    def queue(self, request, *, reindex=False):
        self.require_collection_keys()
        batch = uuid4().hex
        ids = []
        with self.store.connect() as db:
            # 중복 확인과 대기 이력 등록을 한 쓰기 트랜잭션으로 묶어 동시 요청 중복을 막습니다.
            db.execute('BEGIN IMMEDIATE')
            for item in request.items:
                if item.effective and not self.collectible_date(item.effective):
                    raise ValueError('오늘 이후 시행 예정이거나 시행일이 잘못된 버전은 수집할 수 없습니다.')
                duplicate = db.execute("SELECT 1 FROM collection_history WHERE kind=? AND mst=? AND effective=? AND status IN ('queued','running')", (item.kind, item.mst,item.effective)).fetchone()
                collected = db.execute("SELECT 1 FROM law_versions WHERE kind=? AND mst=? AND (?='' OR effective=?)", (item.kind, item.mst,item.effective,item.effective)).fetchone()
                if duplicate or (collected and not reindex):
                    continue
                event_id = uuid4().hex
                db.execute('INSERT INTO collection_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (event_id, batch, item.mst, item.kind, item.name, item.promulgated, item.effective,
                     'queued', now(), '', '', '', '', request.keyword_only))
                ids.append(event_id)
        return {'batch': batch, 'queued': len(ids), 'ids': ids}

    def run(self, ids):
        # One API/embedding request stream at a time; queue/status remains readable.
        with self.worker_lock:
            for event_id in ids:
                with self.store.connect() as db:
                    event = dict(db.execute('SELECT * FROM collection_history WHERE id=?', (event_id,)).fetchone())
                    db.execute("UPDATE collection_history SET status='running',started_at=? WHERE id=?", (now(), event_id))
                raw_filename = ''
                try:
                    raw = self.api.fetch(event['mst'], kind=event['kind'], effective=event['effective'])
                    # 파싱 실패도 원문을 조사할 수 있게 먼저 보관합니다. 저장 파일명은 원문 해시입니다.
                    raw_filename = self.store.archive_raw(raw)
                    law = parse_admrul(raw) if event['kind'] == 'admrul' else parse_law(raw)
                    if not self.collectible_date(law.effective):
                        raise ValueError('오늘 이후 시행 예정이거나 시행일이 잘못된 본문은 저장하지 않았습니다.')
                    # API가 다른 시행 버전의 본문을 반환하면 그대로 저장하지 않습니다.
                    # 법령 본문 조회 시 MST 외에 시행일도 전달하는 이유가 이 버전 일치 조건입니다.
                    if normalize_name(law.name) != normalize_name(event['name']) or (event['effective'] and law.effective != event['effective']):
                        raise ValueError(f'본문 응답 불일치: 요청 {event["name"]} / 시행 {event["effective"]}, 응답 {law.name} / 시행 {law.effective}.')
                    result = self.store.ingest(raw, self.provider, bool(event['keyword_only']),
                                              mst=event['mst'], kind=event['kind'], archive_only=True)
                    with self.store.connect() as db:
                        if not db.execute('SELECT 1 FROM categories WHERE kind=? AND compact_name(name)=?', (event['kind'],normalize_name(law.name))).fetchone():
                            db.execute('INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)', (law.name,event['kind']))
                        db.execute("UPDATE collection_history SET status='completed',finished_at=?,version=?,name=?,promulgated=?,effective=? WHERE id=?",
                                   (now(), result['version'], law.name, law.promulgated, law.effective, event_id))
                except Exception as exc:
                    # Never persist arbitrary upstream exceptions containing credentials/URLs.
                    if isinstance(exc, SetupError):
                        reason = str(exc)
                    elif isinstance(exc, ValueError):
                        reason = '응답 파싱 또는 입력 검증 실패: ' + str(exc)[:300]
                        if raw_filename:
                            reason += f' 원문 XML: raw/{raw_filename} (DATA_DIR 기준)'
                    else:
                        reason = '내부 저장 또는 처리 실패. 서버 상태를 확인한 후 다시 수집하세요.'
                    with self.store.connect() as db:
                        db.execute("UPDATE collection_history SET status='failed',finished_at=?,reason=? WHERE id=?", (now(), reason, event_id))

    def history(self, page, batch=None):
        where, args = ('WHERE batch=?', (batch,)) if batch else ('', ())
        with self.store.connect() as db:
            total = db.execute(f'SELECT count(*) FROM collection_history {where}', args).fetchone()[0]
            rows = [dict(r) for r in db.execute(f'''SELECT * FROM collection_history {where}
                ORDER BY requested_at DESC LIMIT 10 OFFSET ?''', (*args, (page-1)*10))]
            pending = db.execute(f"SELECT count(*) FROM collection_history {where + ' AND' if where else 'WHERE'} status IN ('queued','running')", args).fetchone()[0]
            last = db.execute("SELECT max(finished_at) FROM collection_history WHERE status='completed'").fetchone()[0]
            # 본문 완료와 동반 법령 완료를 구별합니다. 일부 실패를 전체 체계도 완료로
            # 표시하지 않고, 재계획하면 저장된 항목은 건너뛰고 빠진 항목만 재시도합니다.
            batches = list({r['batch'] for r in rows})
            plans = {r['batch']: json.loads(r['payload']) for r in db.execute(
                'SELECT b.batch,p.payload FROM collection_batch_plans b JOIN collection_plans p ON p.id=b.plan_id WHERE b.batch IN (' + ','.join('?' for _ in batches) + ')',
                batches)} if batches else {}
            states = {}
            for row in rows:
                plan = plans.get(row['batch'])
                if plan is None:
                    continue
                if row['batch'] not in states:
                    states[row['batch']] = sum(not bool(db.execute(
                        'SELECT 1 FROM law_versions WHERE kind=? AND mst=? AND effective=?',
                        (p['kind'], p['mst'], p['effective'])).fetchone()) for p in plan['items'])
                row['hierarchy_status'] = 'incomplete' if states[row['batch']] or plan['warnings'] else 'completed'
                row['hierarchy_missing'] = states[row['batch']]
                row['hierarchy_warnings'] = plan['warnings']
        return {'items': rows, 'total': total, 'page': page, 'pending': pending, 'last_completed': last}

    def articles(self, version, page):
        with self.store.connect() as db:
            law = db.execute('SELECT *, EXISTS(SELECT 1 FROM laws l WHERE l.version=v.version) active FROM law_versions v WHERE version=?', (version,)).fetchone()
            if law is None:
                raise HTTPException(404, '수집된 버전이 없습니다.')
            total = db.execute('SELECT count(*) FROM version_articles WHERE version=?', (version,)).fetchone()[0]
            rows = [dict(r) for r in db.execute('''SELECT number,title,body,section_id,chapter,position FROM version_articles
                WHERE version=? ORDER BY position LIMIT 10 OFFSET ?''', (version, (page-1)*10))]
            for row in rows:
                metadata = db.execute('''SELECT b.status,b.error,m.summary,m.questions,m.generator_model,m.created_at
                    FROM article_metadata b LEFT JOIN search_metadata m ON m.key=b.metadata_key
                    WHERE b.version=? AND b.position=?''', (version,row['position'])).fetchone()
                row['search_metadata'] = ({**dict(metadata),'questions':json.loads(metadata['questions'] or '[]'),
                    'generated':bool(metadata['summary'])} if metadata else None)
        for row in rows:
            row['html_url'] = '/documents/' + law['html'] + '#' + quote(row['section_id'], safe='')
        metadata = dict(law)
        metadata['active'] = version in {r['version'] for r in self.store.temporal_versions()['versions']}
        return {'law': metadata, 'total': total, 'page': page, 'items': rows}


def management_router(manager):
    router = APIRouter(prefix='/api/management')

    @router.get('/categories')
    def categories():
        return {'items': manager.categories()}

    @router.post('/categories')
    def add_category(body: CategoryInput):
        return {'items': manager.add_category(body.name, body.kind)}

    @router.get('/names')
    def names(query: str = Query(min_length=2, max_length=100), kind: Kind = 'law',
              page: int = Query(default=1, ge=1)):
        result = manager.api.search(query.strip(), page, kind=kind, revisions=False, display=10)
        return {'items': result['laws'], 'total': int(result['total']), 'page': page}

    @router.get('/revisions')
    def revisions(query: str = Query(default='', max_length=100), kind: Kind = 'law',
                  page: int = Query(default=1, ge=1), source: Literal['api', 'collected'] = 'collected'):
        if source == 'api' and len(query.strip()) < 2:
            raise ValueError('API 검색에는 두 글자 이상의 법령명이 필요합니다.')
        return manager.revisions(query.strip(), kind, page, source)

    @router.post('/revision-status')
    def revision_status(body: CollectionInput):
        return {'items': [manager.decorate({'id': item.mst, **item.model_dump()}) for item in body.items]}

    # 202는 수집 완료가 아니라 대기 이력 등록 성공입니다.
    # 실제 완료/실패는 백그라운드 작업이 기록하며 /history에서 확인합니다.
    @router.post('/collections', status_code=202)
    def collect(body: PlannedCollectionInput | CollectionInput, background: BackgroundTasks):
        result = manager.queue_plan(body.plan_id) if isinstance(body, PlannedCollectionInput) else manager.queue(body)
        if result['ids']:
            background.add_task(manager.run, result['ids'])
        return result

    @router.post('/collection-plan')
    def collection_plan(body: CollectionInput):
        return manager.plan_collection(body)

    @router.get('/history')
    def history(page: int = Query(default=1, ge=1), batch: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$')):
        return manager.history(page, batch)

    @router.post('/versions/{version}/reindex')
    def reindex(version: str):
        with manager.store.connect() as db:
            row = db.execute('SELECT * FROM law_versions WHERE version=?', (version,)).fetchone()
        if row is None:
            raise HTTPException(404, '수집한 버전이 없습니다.')
        raw = manager.store.directory / 'raw' / (row['version'] + '.xml')
        if not raw.is_file():
            raise HTTPException(409, '원본 XML이 없어 재색인할 수 없습니다.')
        with manager.worker_lock:
            return manager.store.ingest(raw.read_bytes(), manager.provider, mst=row['mst'], kind=row['kind'], archive_only=True)

    @router.get('/versions/{version}/articles')
    def articles(version: str, page: int = Query(default=1, ge=1)):
        return manager.articles(version, page)

    return router

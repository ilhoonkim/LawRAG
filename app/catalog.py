"""User-requested real estate catalog and explicit, resumable-by-item batch intake."""
import json
from pathlib import Path
import re
from uuid import uuid4
from .laws import SetupError
from .temporal import today_in_korea

CATALOG = json.loads((Path(__file__).parent / 'catalogs' / 'real_estate.json').read_text(encoding='utf-8'))


def normalize_name(name):
    return re.sub(r'\s+', '', name).strip('「」')


def register_catalog(db):
    # The user supplied these exact names; registration is not collection.
    db.execute('CREATE TABLE IF NOT EXISTS catalog_registrations (id TEXT PRIMARY KEY)')
    if db.execute('SELECT 1 FROM catalog_registrations WHERE id=?', (CATALOG['id'],)).fetchone():
        return
    db.executemany('INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)',
                   [(item['name'],item['kind']) for item in CATALOG['items']])
    db.execute('INSERT INTO catalog_registrations VALUES(?)', (CATALOG['id'],))


def snapshot(manager):
    result = []
    with manager.store.connect() as db:
        for item in CATALOG['items']:
            versions = db.execute('SELECT count(*) FROM law_versions WHERE kind=? AND compact_name(name)=?', (item['kind'],normalize_name(item['name']))).fetchone()[0]
            history = db.execute('''SELECT status,reason,finished_at FROM collection_history
                WHERE kind=? AND compact_name(name)=? ORDER BY requested_at DESC LIMIT 1''', (item['kind'],normalize_name(item['name']))).fetchone()
            result.append({**item, 'collected_versions': versions,
                'status': history['status'] if history else ('completed' if versions else 'uncollected'),
                'reason': history['reason'] if history else ''})
    return {**CATALOG, 'items': result}


def queue_catalog(manager, keyword_only):
    from .management import now
    manager.require_collection_keys()
    batch, ids = uuid4().hex, []
    with manager.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in CATALOG['items']:
            if db.execute("SELECT 1 FROM collection_history WHERE compact_name(name)=? AND kind=? AND status IN ('queued','running')", (normalize_name(item['name']),item['kind'])).fetchone():
                continue
            event_id = uuid4().hex
            db.execute('INSERT INTO collection_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id,batch,'',item['kind'],item['name'],'','','queued',now(),'','','','',keyword_only))
            ids.append(event_id)
    return {'batch':batch,'queued':len(ids),'ids':ids}


def resolve_catalog_item(manager, event):
    """Scan complete search pages; only exact names and dates through today qualify."""
    from .management import now
    matches, seen = {}, set()
    today = today_in_korea()
    for page in range(1,201):
        result = manager.api.search(event['name'],page,kind=event['kind'],revisions=True,display=100)
        rows = result['laws']
        if not rows and (page-1)*100 < int(result['total']):
            raise ValueError('API 연혁 목록이 중간에 비어 있어 최신 버전을 확정하지 못했습니다.')
        page_keys = tuple((r['id'],r['name']) for r in rows)
        if page_keys and page_keys in seen:
            raise ValueError('API가 같은 페이지를 반복 반환하여 연혁 조회를 중단했습니다.')
        seen.add(page_keys)
        for row in rows:
            if normalize_name(row['name']) != normalize_name(event['name']):
                continue
            effective = manager.store._date(row['effective'])
            promulgated = manager.store._date(row.get('promulgated',''))
            if effective and effective <= today and (not promulgated or promulgated <= today) and re.fullmatch(r'\d{1,20}',row['id']):
                matches[row['id']] = row
        if page*100 >= int(result['total']):
            break
    else:
        raise ValueError('연혁 조회가 200페이지를 초과했습니다. 범위를 나누어 조회하세요.')
    if not matches:
        raise ValueError('요청한 정확한 명칭으로 시행일이 오늘 이하인 버전을 찾지 못했습니다. 구명칭·폐지 여부 또는 해당 API의 수록 여부를 확인하세요.')
    chosen = max(matches.values(),key=lambda r:(r['effective'],r.get('promulgated',''),int(r['id'])))
    with manager.store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO revision_catalog VALUES(?,?,?,?,?)',
            [(event['kind'],r['id'],r['name'],r['effective'],r.get('promulgated','')) for r in matches.values()])
        db.execute('UPDATE collection_history SET mst=?,promulgated=?,effective=? WHERE id=?',
            (chosen['id'],chosen.get('promulgated',''),chosen['effective'],event['id']))
        previous = db.execute('SELECT version FROM law_versions WHERE kind=? AND mst=?', (event['kind'],chosen['id'])).fetchone()
        pending = db.execute("SELECT 1 FROM collection_history WHERE kind=? AND mst=? AND id!=? AND status IN ('queued','running')", (event['kind'],chosen['id'],event['id'])).fetchone()
        if previous or pending:
            db.execute("UPDATE collection_history SET status='skipped',finished_at=?,reason=?,version=? WHERE id=?",
                (now(),'이미 수집된 버전입니다.' if previous else '동일 버전의 수집 작업이 진행 중입니다.',previous['version'] if previous else '',event['id']))
            return None
    return {**event,'mst':chosen['id'],'expected_name':chosen['name'],'expected_effective':chosen['effective']}

"""One-off full-history intake. Run with the web server stopped; restart-safe via DB."""
import json
from uuid import uuid4

from .catalog import CATALOG, normalize_name
from .config import Settings
from .laws import LawAPI, SetupError
from .management import Management, CollectionInput, now
from .provider import Provider
from .store import Store


def discover_versions(manager, item):
    """Resolve current and requested historical titles; retain every API version."""
    matches = {}
    names = [item['name'], *item.get('aliases', [])]
    accepted = {normalize_name(name) for name in names}
    for query in names:
        seen = set()
        for page in range(1, 201):
            result = manager.api.search(query, page, kind=item['kind'], revisions=True, display=100)
            rows = result['laws']
            signature = tuple(row['id'] for row in rows)
            if (not rows and (page-1)*100 < int(result['total'])) or (rows and signature in seen):
                raise ValueError('API 연혁 페이지가 누락되거나 반복되었습니다.')
            seen.add(signature)
            for row in rows:
                if normalize_name(row['name']) in accepted:
                    if not row['id'].isdigit() or len(row['id']) > 20:
                        raise ValueError('버전 식별자가 없는 연혁이 반환되었습니다.')
                    matches[row['id']] = {**row, 'kind': item['kind']}
            if page*100 >= int(result['total']):
                break
        else:
            raise ValueError('연혁 조회가 200페이지를 초과했습니다.')
    if not matches:
        raise ValueError('현행 명칭과 과거 명칭으로 조회된 버전이 없습니다. API 수록 여부를 확인하세요.')
    rows = sorted(matches.values(), key=lambda row:(row['effective'],row.get('promulgated',''),int(row['id'])))
    with manager.store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO revision_catalog VALUES(?,?,?,?,?)',
            [(item['kind'],r['id'],r['name'],r['effective'],r.get('promulgated','')) for r in rows])
    return rows


def indexed(manager, row):
    with manager.store.connect() as db:
        stored = db.execute('SELECT version FROM law_versions WHERE kind=? AND mst=?', (row['kind'],row['id'])).fetchone()
        if not stored:
            return False
        counts = db.execute('''SELECT count(*), sum(CASE WHEN vector IS NULL OR title_vector IS NULL
            OR model IS NULL OR model!=? THEN 1 ELSE 0 END)
            FROM version_articles WHERE version=?''', (manager.provider.fingerprint,stored['version'])).fetchone()
        return counts[0] > 0 and not counts[1]


def record_discovery_failure(manager, item, reason):
    with manager.store.connect() as db:
        db.execute('INSERT INTO collection_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex,uuid4().hex,'',item['kind'],item['name'],'','', 'failed',now(),now(),now(),reason,'',False))


def preload(manager, report_progress=print):
    manager.require_collection_keys()
    report = {'started_at':now(),'targets':len(CATALOG['items']),'items':[]}
    for number, item in enumerate(CATALOG['items'], 1):
        entry = {'name':item['name'],'kind':item['kind'],'found':0,'completed':0,'already_indexed':0,'failed':0,'pending':0,'errors':[]}
        report_progress(f'[{number}/{report["targets"]}] {item["name"]}: 전체 버전 조회')
        try:
            rows = discover_versions(manager, item)
            entry['found'] = len(rows)
            missing = []
            for row in rows:
                if indexed(manager, row):
                    entry['already_indexed'] += 1
                else:
                    missing.append(row)
            for start in range(0, len(missing), 30):
                request = CollectionInput(items=[{'mst':r['id'],'kind':r['kind'],'name':r['name'],
                    'effective':r['effective'],'promulgated':r.get('promulgated','')} for r in missing[start:start+30]])
                # Also repair previously collected versions lacking either embedding.
                queued = manager.queue(request, reindex=True)
                entry['pending'] += len(request.items) - queued['queued']
                manager.run(queued['ids'])
                with manager.store.connect() as db:
                    for event_id in queued['ids']:
                        event = db.execute('SELECT mst,status,reason FROM collection_history WHERE id=?',(event_id,)).fetchone()
                        if event['status'] == 'completed':
                            entry['completed'] += 1
                        else:
                            entry['failed'] += 1
                            entry['errors'].append({'mst':event['mst'],'reason':event['reason']})
                report_progress(f'  {entry["found"]}개 중 완료 {entry["completed"]}, 기존 임베딩 {entry["already_indexed"]}, 실패 {entry["failed"]}')
        except Exception as exc:
            reason = str(exc)[:300] if isinstance(exc,(SetupError,ValueError)) else '조회 또는 저장 처리 실패. 서버 설정과 저장 공간을 확인하세요.'
            entry['errors'].append({'reason':reason})
            record_discovery_failure(manager, item, reason)
            report_progress(f'  실패: {reason}')
        report['items'].append(entry)
        report['updated_at'] = now()
        (manager.store.directory / 'preload-real-estate-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    report['finished_at'] = now()
    report['ok'] = all(not r['errors'] and not r['pending'] for r in report['items'])
    (manager.store.directory / 'preload-real-estate-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    settings = Settings()
    missing = [key for key,value in [('LAW_API_OC',settings.law_oc),('LLM_API_KEY',settings.api_key)] if not value]
    if missing:
        print('수집을 시작하지 않았습니다. 프로젝트 .env에 필요한 설정: ' + ', '.join(missing))
        return 2
    manager = Management(Store(settings.data_dir), Provider(settings), LawAPI(settings.law_oc))
    report = preload(manager)
    print('전체 수집·임베딩 완료' if report['ok'] else '일부 작업이 완료되지 않았습니다. 수집 이력과 결과 파일을 확인하세요.')
    print('결과 파일:',settings.data_dir / 'preload-real-estate-report.json')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

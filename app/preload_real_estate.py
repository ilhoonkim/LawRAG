"""Intake the latest three eligible versions per law; restart-safe via DB."""
import argparse
import json
from uuid import uuid4

from .catalog import CATALOG, normalize_name
from .config import Settings
from .laws import LawAPI, SetupError
from .management import Management, CollectionInput, now
from .provider import Provider
from .store import Store, body_text
from .temporal import today_in_korea

# 지정 목록의 일괄 수집 CLI: python -m app.preload_real_estate
# 화면 수집과 같은 Management 경로를 사용하므로 완료·실패 내역도 동일한 DB에 남습니다.
RECENT_VERSION_LIMIT = 3


# '최근 3개'는 오늘까지 시행된 연혁 중 세 개입니다. 이미 수집된 건을 제외하기 전에
# 범위를 정해야 반복 실행할 때마다 더 오래된 버전을 추가로 수집하지 않습니다.
def recent_versions(rows):
    # discover_versions returns oldest first. Limit BEFORE skipping indexed rows,
    # so repeated runs cannot gradually backfill older versions.
    return rows[-RECENT_VERSION_LIMIT:][::-1]


# 현행 명칭과 등록된 과거 명칭을 모두 조회하고 정확히 일치하는 결과만 모읍니다.
# 조회 연혁은 DB에 남기지만 본문 수집 여부와 구분합니다. 실제 수집 범위는 호출자가 줄입니다.
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
            signature = tuple((row['id'],row['effective']) for row in rows)
            if (not rows and (page-1)*100 < int(result['total'])) or (rows and signature in seen):
                raise ValueError('API 연혁 페이지가 누락되거나 반복되었습니다.')
            seen.add(signature)
            for row in rows:
                if normalize_name(row['name']) in accepted and manager.collectible_date(row['effective']):
                    if not row['id'].isdigit() or len(row['id']) > 20:
                        raise ValueError('버전 식별자가 없는 연혁이 반환되었습니다.')
                    matches[(row['id'],row['effective'])] = {**row, 'kind': item['kind']}
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


# 이 CLI의 완료 판정은 제목·본문 벡터와 현재 임베딩 모델을 기준으로 합니다.
def indexed(manager, row):
    with manager.store.connect() as db:
        stored = db.execute('SELECT version FROM law_versions WHERE kind=? AND mst=? AND effective=?', (row['kind'],row['id'],row['effective'])).fetchone()
        if not stored:
            return False
        rows = list(db.execute('SELECT body,vector,title_vector,model FROM version_articles WHERE version=?', (stored['version'],)))
        return bool(rows) and all(not body_text(r).strip() or
            (r['vector'] is not None and r['title_vector'] is not None and r['model'] == manager.provider.fingerprint)
            for r in rows)


def record_discovery_failure(manager, item, reason):
    with manager.store.connect() as db:
        db.execute('INSERT INTO collection_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex,uuid4().hex,'',item['kind'],item['name'],'','', 'failed',now(),now(),now(),reason,'',False))


def preload(manager, report_progress=print, *, items=None):
    manager.require_collection_keys()
    items = CATALOG['items'] if items is None else items
    report = {'started_at':now(),'targets':len(items),'version_limit':RECENT_VERSION_LIMIT,'items':[]}
    for number, item in enumerate(items, 1):
        entry = {'name':item['name'],'kind':item['kind'],'discovered':0,'found':0,'completed':0,'already_indexed':0,'failed':0,'pending':0,'errors':[]}
        report_progress(f'[{number}/{report["targets"]}] {item["name"]}: 전체 버전 조회')
        try:
            discovered = discover_versions(manager, item)
            entry['discovered'] = len(discovered)
            rows = recent_versions(discovered)
            entry['found'] = len(rows)
            report_progress(f'  전체 연혁 {len(discovered)}개 / 최근 수집 대상 {len(rows)}개'
                            f' (시행 {rows[-1]["effective"]} ~ {rows[0]["effective"]}, 예정 제외)')
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
                        event = db.execute('SELECT mst,effective,status,reason FROM collection_history WHERE id=?',(event_id,)).fetchone()
                        if event['status'] == 'completed':
                            entry['completed'] += 1
                        else:
                            entry['failed'] += 1
                            entry['errors'].append({'mst':event['mst'],'effective':event['effective'],'reason':event['reason']})
                            report_progress(f'  실패 MST {event["mst"]} / 시행 {event["effective"]}: {event["reason"]}')
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='법령별 오늘까지 시행된 최근 3개 버전 수집·임베딩')
    parser.add_argument('--law', help='지정 목록의 법령 하나만 처리 (예: 소득세법)')
    parser.add_argument('--list-only', action='store_true', help='연혁 건수·시행일만 확인, 본문 수집·임베딩 없음')
    args = parser.parse_args(argv)
    items = CATALOG['items']
    if args.law:
        items = [item for item in items if normalize_name(args.law) in
                 {normalize_name(name) for name in [item['name'], *item.get('aliases', [])]}]
        if not items:
            parser.error('지정 목록에 없는 법령명입니다.')
    settings = Settings()
    required = [('LAW_API_OC', settings.law_oc)]
    if not args.list_only:
        required.append(('LLM_API_KEY', settings.api_key))
    missing = [key for key, value in required if not value]
    if missing:
        print('수집을 시작하지 않았습니다. 프로젝트 .env에 필요한 설정: ' + ', '.join(missing))
        return 2
    manager = Management(Store(settings.data_dir), Provider(settings), LawAPI(settings.law_oc), recover_pending=not args.list_only)
    print(f'한국 날짜 {today_in_korea().isoformat()} 기준: 법령별 최근 {RECENT_VERSION_LIMIT}개, 시행 예정 제외')
    # 조회 전용도 발견한 연혁은 revision_catalog에 저장합니다.
    # 본문 다운로드·임베딩과 이전 실행의 대기 작업 상태 변경은 하지 않습니다.
    if args.list_only:
        failed = False
        for item in items:
            try:
                discovered = discover_versions(manager, item)
                rows = recent_versions(discovered)
                print(f'{item["name"]}: 전체 연혁 {len(discovered)}개 / 수집 대상 {len(rows)}개 시행 버전'
                      f' / 시행일 {rows[-1]["effective"]} ~ {rows[0]["effective"]}')
                for row in rows:
                    print(f'  MST {row["id"]} / 시행 {row["effective"]} / {row["name"]}')
            except (SetupError, ValueError) as exc:
                failed = True
                print(f'{item["name"]}: 조회 실패: {exc}')
        print('조회만 완료했습니다. 본문 수집·임베딩은 실행하지 않았습니다.')
        return 1 if failed else 0
    report = preload(manager, items=items)
    print('전체 수집·임베딩 완료' if report['ok'] else '일부 작업이 완료되지 않았습니다. 수집 이력과 결과 파일을 확인하세요.')
    print('결과 파일:',settings.data_dir / 'preload-real-estate-report.json')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

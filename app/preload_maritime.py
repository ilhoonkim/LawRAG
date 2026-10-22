"""지정한 해양·환경 법률의 현행 버전과 공식 하위법을 한 세트씩 수집합니다.

python -m app.preload_maritime [--list-only] [--law 법령명]
완료 여부는 DB로 판단합니다. 재실행해도 최근 과거 버전으로 범위를 늘리지 않습니다.
"""
import argparse
from pathlib import Path
import sqlite3

from .backfill_hierarchy import write_report
from .catalog import normalize_name
from .config import Settings
from .delegation import status as delegation_status
from .hierarchy import current_revision
from .laws import LawAPI, SetupError
from .management import CollectionInput, Management, now
from .preload_real_estate import record_discovery_failure
from .provider import Provider
from .store import Store
from .temporal import today_in_korea

LAWS = (
    '가축분뇨의 관리 및 이용에 관한 법률', '낚시 관리 및 육성법', '내수면어업법',
    '대기환경보전법', '물환경보전법', '배타적 경제수역 및 대륙붕에 관한 법률',
    '석유 및 석유대체연료 사업법', '선박법', '선박안전법', '선박직원법', '선원법',
    '수산업법', '수산자원관리법', '수상레저기구의 등록 및 검사에 관한 법률',
    '수상레저안전법', '양식산업발전법', '어선법', '어선안전조업법', '영해 및 접속수역법',
    '유선 및 도선 사업법', '전파법', '중대재해 처벌 등에 관한 법률', '폐기물관리법',
    '하수도법', '항만법', '항만지역등 대기질 개선에 관한 특별법', '해사안전법',
    '해양폐기물 및 해양오염퇴적물 관리법', '해양환경관리법', '해운법',
    '환경친화적 선박의 개발 및 보급 촉진에 관한 법률',
)
# 입력의 '법령' 표현만 정확한 법률명으로 보완합니다. 폐지법의 후속법은 임의 대체하지 않습니다.
NAME_CORRECTIONS = {normalize_name('수상레저기구의 등록 및 검사에 관한 법령'): '수상레저기구의 등록 및 검사에 관한 법률'}


def preload(manager, names=LAWS, *, list_only=False, notify=print):
    names = list(dict.fromkeys(NAME_CORRECTIONS.get(normalize_name(n), n.strip()) for n in names))
    if not list_only:
        manager.require_collection_keys()
    report = {'started_at':now(), 'as_of':today_in_korea().isoformat(), 'targets':len(names),
              'version_limit':1, 'list_only':list_only, 'items':[], 'ok':False, 'interrupted':False}
    path = manager.store.directory / 'preload-maritime-report.json'
    attempted, relation_attempted = set(), set()
    batch, warned = None, False

    def checkpoint():
        nonlocal warned
        try:
            write_report(path, report)
        except OSError:
            if not warned:
                notify('결과 JSON 저장 실패. 완료된 작업은 DB에 저장됩니다. 경로·공간을 확인하세요.')
                warned = True

    try:
        for index, name in enumerate(names, 1):
            entry = {'name':name, 'status':'checking', 'completed':0, 'already_collected':0, 'errors':[], 'warnings':[]}
            report['items'].append(entry)
            batch = None
            notify(f'[{index}/{len(names)}] {name}: 현행 법률 및 하위 시행령·시행규칙 조회')
            checkpoint()
            try:
                row = current_revision(manager, {'name':name})
                plan = manager.plan_collection(CollectionInput(items=[dict(mst=row['id'], name=row['name'],
                    effective=row['effective'], promulgated=row.get('promulgated',''))]), current_only=True)
                entry.update(plan_id=plan['plan_id'], items=plan['items'], warnings=plan['warnings'],
                             already_collected=sum(r['collected'] for r in plan['items']), status='planned')
                for item in plan['items']:
                    notify(f'  {item["name"]} · 시행 {item["effective"]} · MST {item["mst"]} · '+('저장됨' if item['collected'] else '수집 대상'))
                for warning in plan['warnings']:
                    notify('  확인 필요: '+warning)
                if list_only:
                    continue
                queued = manager.queue_plan(plan['plan_id'], skip_sources=attempted)
                batch = queued['batch']
                for event_id in queued['ids']:
                    with manager.store.connect() as db:
                        event = dict(db.execute('SELECT * FROM collection_history WHERE id=?', (event_id,)).fetchone())
                    attempted.add((event['kind'],event['mst'],event['effective']))
                    notify(f'  수집·메타데이터·임베딩: {event["name"]}')
                    manager.run([event_id], with_relations=False)
                    with manager.store.connect() as db:
                        event = dict(db.execute('SELECT * FROM collection_history WHERE id=?', (event_id,)).fetchone())
                    if event['status'] == 'completed':
                        entry['completed'] += 1
                    else:
                        entry['errors'].append({'name':event['name'], 'reason':event['reason']})
                    notify('  '+event['status']+(' · '+event['reason'] if event['reason'] else ''))
                    checkpoint()
                notify('  위임조문 3단비교 조회·버전 매핑')
                manager.run([], plan_id=plan['plan_id'], delegation_attempted=relation_attempted)
                entry['delegation'], missing = [], []
                with manager.store.connect() as db:
                    for item in plan['items']:
                        stored = db.execute("SELECT version FROM law_versions WHERE kind='law' AND mst=? AND effective=?", (item['mst'],item['effective'])).fetchone()
                        if stored:
                            entry['delegation'].append({'name':item['name'], **delegation_status(manager.store, stored['version'])})
                        else:
                            missing.append(item['name'])
                entry['missing'] = missing
                incomplete_relations = any(r['status'] not in ('completed','unavailable') for r in entry['delegation'])
                entry['status'] = 'incomplete' if missing or entry['warnings'] or incomplete_relations else 'completed'
                notify(f'  본문 신규 완료 {entry["completed"]} · 기존 {entry["already_collected"]} · 묶음 상태 {entry["status"]}')
            except (SetupError, ValueError) as exc:
                entry.update(status='failed')
                entry['errors'].append({'reason':str(exc)})
                notify('  확인 필요: '+str(exc))
                if not list_only:
                    record_discovery_failure(manager, {'name':name,'kind':'law'}, str(exc))
            finally:
                checkpoint()
        report['ok'] = all(r['status'] in ('completed','planned') and not r['warnings'] and not r['errors'] for r in report['items'])
    except KeyboardInterrupt:
        report['interrupted'] = True
        if report['items']:
            report['items'][-1]['status'] = 'interrupted'
        if batch:
            with manager.store.connect() as db:
                db.execute("UPDATE collection_history SET status='interrupted',finished_at=?,reason=? WHERE batch=? AND status IN ('queued','running')",
                    (now(),'수집이 중단되었습니다. 같은 명령으로 재개하세요.',batch))
        notify('중단했습니다. 같은 명령을 실행하면 완료된 본문은 건너뜁니다.')
    finally:
        report['finished_at'] = now()
        checkpoint()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='해양·환경 법률 31개 및 공식 하위법의 현재 시행 버전 1세트씩 수집')
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument('--law', help='정확한 법령명 하나만 지정 (기본 목록 외 법령도 가능)')
    targets.add_argument('--names-file', type=Path, help='UTF-8 텍스트, 한 줄에 법령명 하나')
    parser.add_argument('--list-only', action='store_true', help='대상 조회만 수행, 본문 수집·임베딩 없음')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args(argv)
    names = [args.law] if args.law else LAWS
    if args.names_file:
        try:
            names = [n.strip() for n in args.names_file.read_text(encoding='utf-8-sig').splitlines() if n.strip()]
        except (OSError, UnicodeError):
            print('법령명 파일 경로와 UTF-8 인코딩을 확인하세요.')
            return 2
    if not names:
        print('수집할 법령명을 입력하세요.')
        return 2
    settings = Settings()
    if not settings.law_oc or (not args.list_only and not settings.api_key):
        print('.env의 LAW_API_OC'+(' 및 LLM_API_KEY(또는 OPENAI_API_KEY)' if not args.list_only else '')+'를 확인하세요.')
        return 2
    print('앱 서버를 종료한 뒤 실행하세요. 한국 날짜 '+today_in_korea().isoformat()+' 기준 현재 시행 버전만 수집합니다.')
    try:
        manager = Management(Store(args.data_dir or settings.data_dir), Provider(settings), LawAPI(settings.law_oc), recover_pending=not args.list_only)
        report = preload(manager, names, list_only=args.list_only)
    except (OSError, sqlite3.Error):
        print('DB/파일 처리 실패. 저장 공간·경로를 확인한 뒤 같은 명령으로 재개하세요.')
        return 1
    print('결과:', manager.store.directory / 'preload-maritime-report.json')
    print('완료' if report['ok'] else '일부 확인/보완이 필요합니다. 결과 파일과 수집 이력을 확인하세요.')
    return 130 if report['interrupted'] else (0 if report['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())

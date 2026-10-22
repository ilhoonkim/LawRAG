"""기존 수집 버전 전체의 상하위법을 조회하고 빠진 본문만 수집·임베딩합니다.

실행: python -m app.backfill_hierarchy
앱 서버를 먼저 종료합니다. 완료 판정과 재개 기준은 JSON 보고서가 아닌 DB입니다.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4

from .catalog import normalize_name
from .config import Settings
from .hierarchy import revision_rows
from .laws import LawAPI, SetupError
from .management import CollectionInput, Management, now
from .provider import Provider
from .store import Store


def source_item(manager, row):
    """과거 저장 자료에 MST가 없으면 동일 시행 버전을 유일하게 식별한 경우만 보완합니다."""
    mst = row['mst'] or ''
    if not re.fullmatch(r'\d{1,20}', mst):
        matches = [r for r in revision_rows(manager, row) if r['effective'] == row['effective'] and
                   (not row['promulgated'] or r.get('promulgated') == row['promulgated'])]
        if len(matches) != 1:
            raise ValueError('기존 자료의 법령일련번호(MST)가 없고, 같은 시행 버전을 유일하게 확인하지 못했습니다.')
        mst = matches[0]['id']
        with manager.store.connect() as db:
            db.execute('UPDATE law_versions SET mst=? WHERE version=?', (mst, row['version']))
    return dict(mst=mst, name=row['name'], effective=row['effective'],
                promulgated=row['promulgated'] or '', kind='law')


def write_report(path, report):
    """Windows에서도 완성된 임시 파일을 닫은 뒤 교체해 중간 JSON 노출을 방지합니다."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.stem+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def backfill(manager, *, law_name=None, list_only=False, notify=print):
    with manager.store.connect() as db:
        # 시작 시 대상 목록을 고정합니다. 이번에 추가한 하위법을 즉시 다시 출발점으로
        # 삼아 수집 범위가 재귀적으로 늘어나지 않습니다. 행정규칙은 제외합니다.
        sources = [dict(r) for r in db.execute("SELECT * FROM law_versions WHERE kind='law' ORDER BY name,effective DESC,version")
                   if not law_name or normalize_name(r['name']) == normalize_name(law_name)]
    if not list_only and sources:
        manager.require_collection_keys()
    report = {'started_at': now(), 'list_only': list_only, 'sources': len(sources), 'items': [],
              'collected': 0, 'failed': 0, 'interrupted': False, 'ok': False}
    path = manager.store.directory / 'hierarchy-backfill-report.json'
    attempted, active_batch, report_warning = set(), None, False
    delegation_attempted = set()

    def save():
        nonlocal report_warning
        report['updated_at'] = now()
        try:
            write_report(path, report)
        except OSError:
            if not report_warning:
                notify('주의: 결과 JSON 저장 실패. 수집 결과는 DB에 저장되며 작업은 계속합니다. 파일 잠금·저장 공간을 확인하세요.')
            report_warning = True

    save()
    notify(f'기존 법령 {len(sources)}개 버전의 상하위법 확인 · 법률/시행령/시행규칙만 대상')
    try:
        for index, row in enumerate(sources, 1):
            entry = {'version': row['version'], 'name': row['name'], 'effective': row['effective'],
                     'status': 'checking', 'errors': [], 'warnings': [], 'items': []}
            report['items'].append(entry)
            active_batch = None
            notify(f'[{index}/{len(sources)}] {row["name"]} · 시행 {row["effective"]}: 상하위법 조회')
            save()
            try:
                plan = manager.plan_collection(CollectionInput(items=[source_item(manager, row)]))
                entry.update(plan_id=plan['plan_id'], warnings=plan['warnings'], items=plan['items'], status='planned')
                notify(f'  함께 확인한 버전 {len(plan["items"])}개 · 저장됨 {len(plan["items"])-plan["new_count"]} · 미수집 {plan["new_count"]}')
                for warning in plan['warnings']:
                    notify('  확인 필요: '+warning)
                if list_only:
                    for item in plan['items']:
                        notify(f'  {item["name"]} / 시행 {item["effective"]} / MST {item["mst"]} / '+
                               ('저장됨' if item['collected'] else '수집 대상'))
                    continue
                # 같은 버전이 여러 체계도에 나타나도 이번 실행에서는 한 번만 시도합니다.
                # 실패 항목은 다음 실행 때 재시도하며, 완료 항목은 queue_plan이 DB로 거릅니다.
                queued = manager.queue_plan(plan['plan_id'], skip_sources=attempted)
                active_batch = queued['batch']
                entry['batch'] = active_batch
                for event_id in queued['ids']:
                    with manager.store.connect() as db:
                        event = dict(db.execute('SELECT * FROM collection_history WHERE id=?', (event_id,)).fetchone())
                    attempted.add((event['kind'], event['mst'], event['effective']))
                    notify(f'  수집·임베딩: {event["name"]} / 시행 {event["effective"]}')
                    manager.run([event_id], delegation_attempted=delegation_attempted)
                    with manager.store.connect() as db:
                        event = dict(db.execute('SELECT * FROM collection_history WHERE id=?', (event_id,)).fetchone())
                    if event['status'] == 'completed':
                        report['collected'] += 1
                        notify('  완료')
                    else:
                        report['failed'] += 1
                        entry['errors'].append({'mst': event['mst'], 'effective': event['effective'], 'reason': event['reason']})
                        notify('  실패: '+event['reason'])
                    save()
                # 공유 의존 자료가 이전 체계도에서 실패했거나 다른 작업에서 대기 중이어도
                # 이번 큐가 비었다는 이유만으로 완료로 표시하면 안 됩니다.
                with manager.store.connect() as db:
                    missing = [item for item in plan['items'] if not db.execute(
                        'SELECT 1 FROM law_versions WHERE kind=? AND mst=? AND effective=?',
                        (item['kind'], item['mst'], item['effective'])).fetchone()]
                entry.update(status='incomplete' if missing or plan['warnings'] else 'completed', missing=missing)
                # 이번에 새 본문이 없더라도 기존 가족의 관계를 보완합니다. 본문 저장 결과와
                # 위임관계 매핑 결과는 별도 보고해 관계 실패 때문에 재임베딩하지 않습니다.
                manager.run([], plan_id=plan['plan_id'], delegation_attempted=delegation_attempted)
                from .delegation import status as delegation_status
                entry['delegation'] = delegation_status(manager.store, row['version'])
                notify('  위임관계: '+entry['delegation']['status']+' '+entry['delegation'].get('reason',''))
            except (SetupError, ValueError) as exc:
                entry.update(status='failed')
                reason = str(exc)[:400]
                entry['errors'].append({'reason': reason})
                if not list_only:
                    # 체계도/버전 식별 실패도 보고서에만 남기지 않고 화면 이력에 기록합니다.
                    with manager.store.connect() as db:
                        db.execute('INSERT INTO collection_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (uuid4().hex, uuid4().hex, row['mst'] or '', 'law', row['name'],
                             row['promulgated'] or '', row['effective'], 'failed', now(), now(), now(),
                             '상하위법 조회/계획 실패: '+reason, row['version'], False))
                notify('  조회/계획 실패: '+reason)
            finally:
                save()
        report['ok'] = all(e['status'] in ('completed', 'planned') and not e['errors'] and not e['warnings']
                           and e.get('delegation',{}).get('status','completed') in ('completed','unavailable')
                           for e in report['items'])
    except KeyboardInterrupt:
        report['interrupted'] = True
        if report['items']:
            report['items'][-1]['status'] = 'interrupted'
        if active_batch:
            with manager.store.connect() as db:
                db.execute("UPDATE collection_history SET status='interrupted',finished_at=?,reason=? WHERE batch=? AND status IN ('queued','running')",
                           (now(), '상하위법 보완 작업이 중단되었습니다. 같은 명령으로 재개할 수 있습니다.', active_batch))
        notify('작업을 중단했습니다. 같은 명령을 다시 실행하면 DB에 완료된 버전은 건너뜁니다.')
    finally:
        report['finished_at'] = now()
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='기존 수집 법령 전체의 상하위법 조회 및 누락 버전 수집·임베딩')
    parser.add_argument('--law', help='기존에 수집한 특정 법령명만 처리 (정확한 명칭)')
    parser.add_argument('--list-only', action='store_true', help='대상 조회만 수행, 본문 수집·임베딩 없음')
    parser.add_argument('--data-dir', type=Path, help='생략하면 .env의 DATA_DIR 사용')
    args = parser.parse_args(argv)
    settings = Settings()
    directory = args.data_dir or settings.data_dir
    if not (directory / 'laws.sqlite3').is_file():
        print('기존 수집 DB가 없습니다. DATA_DIR 또는 --data-dir 경로를 확인하세요.')
        return 2
    if not settings.law_oc or (not args.list_only and not settings.ai_configured('embedding')):
        print('프로젝트 .env의 LAW_API_OC'+(' 및 임베딩 서버 설정(OpenAI: LLM_API_KEY / 사내: COMPANY_EMBEDDING_BASE_URL·MODEL)' if not args.list_only else '')+'를 확인하세요.')
        return 2
    print('앱 서버를 종료한 상태에서 실행하세요. 기존 본문은 유지하고 누락 자료만 수집합니다.')
    try:
        manager = Management(Store(directory), Provider(settings), LawAPI(settings.law_oc), recover_pending=not args.list_only)
        report = backfill(manager, law_name=args.law, list_only=args.list_only)
    except (OSError, sqlite3.Error):
        print('DB 또는 저장소 처리 실패로 중단했습니다. 저장 경로·공간을 확인한 뒤 같은 명령을 다시 실행하세요.')
        return 1
    if not report['sources']:
        print('대상 법령이 없습니다. 저장 경로와 --law의 법령명을 확인하세요.')
        return 2
    print(f'신규 수집 완료 {report["collected"]}개 · 수집 실패 {report["failed"]}개 · '
          f'확인/보완 미완료 {sum(e["status"] not in ("completed","planned") or bool(e["warnings"]) for e in report["items"])}개 묶음')
    print('결과 파일:', directory / 'hierarchy-backfill-report.json')
    print('수집 이력은 앱의 법령관리 → 수집 이력에서도 확인할 수 있습니다.')
    return 130 if report['interrupted'] else (0 if report['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())

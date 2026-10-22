"""기존 자료의 위임관계만 보완합니다. 본문 다운로드·임베딩·LLM 호출은 없습니다."""
import argparse
from pathlib import Path
import sqlite3

from .backfill_hierarchy import write_report
from .catalog import normalize_name
from .config import Settings
from .delegation import now, refresh
from .laws import LawAPI, SetupError
from .store import Store


def backfill(store, api, *, law_name=None, force=False, notify=print):
    with store.connect() as db:
        versions = [dict(r) for r in db.execute("SELECT version,name,effective FROM law_versions WHERE kind='law' ORDER BY name,effective DESC")
                    if not law_name or normalize_name(r['name']) == normalize_name(law_name)]
        # 완료된 API 응답은 재사용하되 DB 매핑은 다시 확인합니다. --refresh는 원격 응답도 갱신합니다.
        attempted = set() if force else {r['mst'] for r in db.execute("SELECT mst FROM delegation_snapshots WHERE status IN ('completed','unavailable')")}
    report = {'started_at':now(), 'sources':len(versions), 'items':[], 'interrupted':False, 'ok':False}
    path = store.directory / 'delegation-backfill-report.json'
    warned = False

    def checkpoint():
        nonlocal warned
        try:
            write_report(path, report)
        except OSError:
            if not warned:
                notify('결과 파일 저장 실패. 완료된 위임관계는 DB에 저장되어 있습니다. 저장 경로·공간을 확인하세요.')
                warned = True

    try:
        for index, row in enumerate(versions, 1):
            notify(f'[{index}/{len(versions)}] {row["name"]} · 시행 {row["effective"]}: 위임관계 확인')
            try:
                result = refresh(store, api, row['version'], attempted)
            except SetupError as exc:
                result = {'status':'failed','reason':str(exc)}
            report['items'].append({**row, **(result or {})})
            notify('  '+str(result.get('status'))+' · '+result.get('reason',''))
            checkpoint()
        report['ok'] = bool(versions) and all(r['status'] in ('completed','unavailable') for r in report['items'])
    except KeyboardInterrupt:
        report['interrupted'] = True
        notify('중단했습니다. 같은 명령으로 다시 실행하면 완료된 응답을 재사용합니다.')
    finally:
        report['finished_at'] = now()
        checkpoint()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='저장된 법령의 위임조문 3단비교 조회·매핑 (재임베딩 없음)')
    parser.add_argument('--law', help='정확한 법령명으로 범위 제한')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--refresh', action='store_true', help='완료된 API 응답도 다시 조회')
    args = parser.parse_args(argv)
    settings = Settings()
    directory = args.data_dir or settings.data_dir
    if not (directory / 'laws.sqlite3').is_file() or not settings.law_oc:
        print('기존 수집 DB 경로와 .env의 LAW_API_OC를 확인하세요. AI 키는 필요하지 않습니다.')
        return 2
    print('위임관계만 보완합니다. 미수집 상하위 본문은 app.backfill_hierarchy로 먼저 수집하세요.')
    try:
        report = backfill(Store(directory), LawAPI(settings.law_oc), law_name=args.law, force=args.refresh)
    except (OSError, sqlite3.Error):
        print('DB/파일 처리 실패. 저장 공간과 경로를 확인한 뒤 같은 명령으로 재개하세요.')
        return 1
    print('결과:', directory / 'delegation-backfill-report.json')
    return 130 if report['interrupted'] else (0 if report['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())

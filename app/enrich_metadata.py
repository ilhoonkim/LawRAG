"""Backfill searchable summaries/questions from stored articles without law API calls."""
import argparse
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from .config import Settings
from .laws import SetupError
from .provider import Provider
from .store import Store
from .search_metadata import MetadataBatchError, batches, bind, identity, now, prepare, vectors_ready


def storage_error(exc):
    """Preserve OS diagnostics without printing exception payloads or secrets."""
    codes = [f'{key}={getattr(exc,key)}' for key in ('errno','winerror','sqlite_errorname')
             if getattr(exc,key,None) is not None]
    if getattr(exc,'errno',None) == errno.ENOSPC or getattr(exc,'winerror',None) == 112:
        detail = '저장 공간이 부족합니다'
    elif getattr(exc,'winerror',None) in (32,33):
        detail = '다른 프로그램이 파일을 사용하거나 잠그고 있습니다'
    elif isinstance(exc,PermissionError):
        detail = '파일 접근이 거부되었습니다. 권한 또는 파일 잠금을 확인하세요'
    elif isinstance(exc,sqlite3.Error):
        detail = 'DB 저장 오류입니다. 파일 잠금·권한·저장 공간을 확인하세요'
    else:
        detail = '파일 경로·권한·저장 공간을 확인하세요'
    return detail + (f' [{", ".join(codes)}]' if codes else f' [{type(exc).__name__}]')


def write_report(path, report):
    """An independent export, never the resume checkpoint. Close before Windows rename."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
        for attempt in range(3):
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                transient = exc.errno in (errno.EACCES,errno.EBUSY) or getattr(exc,'winerror',None) in (32,33)
                if not transient or attempt == 2:
                    raise
                time.sleep(.1 * (attempt+1))
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def status(store):
    with store.connect() as db:
        total = db.execute('SELECT count(*) FROM version_articles').fetchone()[0]
        counts = dict(db.execute('SELECT status,count(*) FROM article_metadata GROUP BY status').fetchall())
    return {'total':total, 'completed':counts.get('completed',0), 'failed':counts.get('failed',0),
            'pending':total-sum(counts.values())}


def backfill(store, provider, report_path, law=None, notify=print):
    with store.connect() as db:
        rows = [dict(r) for r in db.execute('''SELECT a.version,a.position,a.number,a.title,a.body,v.name law_name FROM version_articles a
            JOIN law_versions v ON v.version=a.version ORDER BY v.name,v.effective,a.position''')
            if law is None or ''.join(r['law_name'].split()) == ''.join(law.split())]
    if law and not rows:
        raise ValueError('해당 법령의 수집된 조문이 없습니다.')
    report = {'started_at':now(), 'generator_model':provider.settings.metadata_model,
              'embedding_model':provider.settings.embedding_model, 'total':len(rows),
              'completed':0, 'skipped':0, 'failed':0, 'items':[]}

    last_save, last_count, last_error = 0., 0, ''

    def save(force=False):
        nonlocal last_save, last_count, last_error
        # DB is committed per batch. Avoid rewriting a growing report every five rows.
        if not force and len(report['items'])-last_count < 100 and time.monotonic()-last_save < 30:
            return
        report['updated_at'] = now()
        report.pop('report_error',None)
        try:
            write_report(report_path,report)
            if last_error:
                notify('결과 보고서 저장을 재개했습니다.')
            last_error = ''
        except OSError as exc:
            error = storage_error(exc)
            report['report_error'] = error
            if error != last_error:
                notify(f'보고서 저장 경고: {report_path} · {error}. DB의 완료 기록은 유지하며 작업을 계속합니다.')
            last_error = error
        last_save, last_count = time.monotonic(), len(report['items'])

    def record(row, result, error=''):
        report[result] += 1
        report['items'].append({'version':row['version'],'position':row['position'],
                               'name':row['law_name'],'number':row['number'],'status':result,'error':error})

    def fail(rows, error):
        with store.connect() as db:
            for row in rows:
                # Retain a previously usable binding on a failed refresh.
                db.execute('''INSERT INTO article_metadata VALUES(?,?,NULL,'failed',?,?)
                    ON CONFLICT(version,position) DO UPDATE SET status='failed',error=excluded.error,updated_at=excluded.updated_at''',
                    (row['version'],row['position'],error,now()))
        for row in rows:
            record(row,'failed',error)
        notify('실패: ' + error)

    def process(pending):
        if not pending:
            return
        try:
            keys = prepare(store,provider,pending)
            with store.connect() as db:
                for row,key in zip(pending,keys):
                    bind(db,row['version'],row['position'],key)
            for row in pending:
                record(row,'completed')
        except MetadataBatchError as exc:
            good = []
            for row in pending:
                error = exc.errors.get(identity(provider,row)[0])
                if error:
                    fail([row],'검색 메타데이터 생성 실패: '+error)
                else:
                    good.append(row)
            # Valid siblings are already cached. Embed/bind them without generating again.
            process(good)
        except (OSError,sqlite3.Error):
            # A report error is recoverable; a failed DB commit must stop new API work.
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc,(SetupError,ValueError)) else '메타데이터 처리 실패. 서버 응답을 확인하세요.'
            fail(pending,error)

    save(force=True)
    try:
        for batch in batches(rows):
            pending, skipped = [], []
            with store.connect() as db:
                for row in batch:
                    key, _, _ = identity(provider,row)
                    saved = db.execute('''SELECT b.metadata_key,m.summary,m.questions FROM article_metadata b
                        JOIN search_metadata m ON m.key=b.metadata_key WHERE b.version=? AND b.position=?''',
                        (row['version'],row['position'])).fetchone()
                    complete = saved and saved['metadata_key'] == key and vectors_ready(db,key,
                        {'summary':saved['summary'],'questions':json.loads(saved['questions'])},provider.fingerprint)
                    if complete:
                        bind(db,row['version'],row['position'],key)
                        skipped.append(row)
                    else:
                        pending.append(row)
            for row in skipped:
                record(row,'skipped')
            process(pending)
            save()
            notify(f'{len(report["items"])}/{len(rows)} 조문 · 완료 {report["completed"]}, 기존 완료 {report["skipped"]}, 실패 {report["failed"]}')
    except (Exception,KeyboardInterrupt):
        report['interrupted_at'] = now()
        save(force=True)
        raise
    report['finished_at'] = now()
    save(force=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='저장 조문에 검색 요약·일상 질문 생성 및 임베딩 추가 (재실행 가능)')
    parser.add_argument('--law', help='특정 법령만 처리, 생략하면 저장된 모든 버전')
    parser.add_argument('--status', action='store_true', help='DB 처리 현황만 확인 (API 호출 없음)')
    args = parser.parse_args(argv)
    settings = Settings()
    store = Store(settings.data_dir)
    if args.status:
        print(json.dumps(status(store),ensure_ascii=False))
        return 0
    if not settings.api_key:
        print('.env의 LLM_API_KEY 또는 OPENAI_API_KEY가 필요합니다.')
        return 2
    print(f'요약·예상 질문: {settings.metadata_model} / 임베딩: {settings.embedding_model}')
    print('저장된 조문으로 처리합니다. 중단 후 같은 명령을 실행하면 완료한 작업을 재사용합니다.')
    try:
        path = store.directory / 'metadata-enrichment-report.json'
        report = backfill(store,Provider(settings),path,law=args.law)
        if report.get('report_error'):
            print('DB 처리는 끝났지만 결과 파일 저장에 실패했습니다:',report['report_error'])
            print('--status로 DB 현황을 확인할 수 있습니다. 같은 명령을 재실행하면 완료 건을 재사용합니다.')
        else:
            print('결과 파일:',path)
        return 1 if report['failed'] or report.get('report_error') else 0
    except KeyboardInterrupt:
        print('\n작업을 중단했습니다. 같은 명령을 재실행하면 DB에 저장된 완료 작업을 건너뜁니다.')
        return 130
    except (SetupError,ValueError,OSError,sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc,(SetupError,ValueError)) else '저장 작업 중단: '+storage_error(exc))
        print('이미 DB에 저장된 작업은 유지됩니다. 문제를 해결한 후 같은 명령으로 재개하세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

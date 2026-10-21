"""Backfill searchable summaries/questions from stored articles without law API calls."""
import argparse
import json
from .config import Settings
from .laws import SetupError
from .provider import Provider
from .store import Store
from .search_metadata import batches, bind, identity, now, prepare, vectors_ready


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

    def save():
        report['updated_at'] = now()
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(report_path)

    def record(row, result, error=''):
        report[result] += 1
        report['items'].append({'version':row['version'],'position':row['position'],
                               'name':row['law_name'],'number':row['number'],'status':result,'error':error})

    save()
    for batch in batches(rows):
        pending = []
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
                    record(row,'skipped')
                else:
                    pending.append(row)
        if pending:
            try:
                keys = prepare(store,provider,pending)
                with store.connect() as db:
                    for row,key in zip(pending,keys):
                        bind(db,row['version'],row['position'],key)
                for row in pending:
                    record(row,'completed')
            except Exception as exc:
                error = str(exc) if isinstance(exc,(SetupError,ValueError)) else '메타데이터 저장 또는 처리 실패. 서버와 저장 공간을 확인하세요.'
                with store.connect() as db:
                    for row in pending:
                        # Retain a previously usable binding on a failed refresh.
                        db.execute('''INSERT INTO article_metadata VALUES(?,?,NULL,'failed',?,?)
                            ON CONFLICT(version,position) DO UPDATE SET status='failed',error=excluded.error,updated_at=excluded.updated_at''',
                            (row['version'],row['position'],error,now()))
                        record(row,'failed',error)
                notify('실패: ' + error)
        save()
        notify(f'{len(report["items"])}/{len(rows)} 조문 · 완료 {report["completed"]}, 기존 완료 {report["skipped"]}, 실패 {report["failed"]}')
    report['finished_at'] = now()
    save()
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
        print('결과 파일:',path)
        return 1 if report['failed'] else 0
    except (SetupError,ValueError,OSError) as exc:
        print(str(exc) if isinstance(exc,(SetupError,ValueError)) else '결과 파일 저장 실패. 저장 경로를 확인하세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

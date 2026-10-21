"""Resume-safe local embedding upgrade from stored article text. Stop the app first."""
import json
import hashlib
from dataclasses import replace
from dotenv import set_key
import sqlite_vec
from .config import Settings, ROOT, _MODELS_FROM_PROCESS
from .laws import SetupError
from .provider import Provider
from .store import Store, body_text

TARGET = 'text-embedding-3-large'


def reindex(store, provider, report_path, notify=print):
    report = {'embedding_model': TARGET, 'completed': 0, 'skipped': 0, 'failed': 0, 'items': []}
    with store.connect() as db:
        versions = [dict(r) for r in db.execute('SELECT * FROM law_versions ORDER BY name,effective')]
    for version in versions:
        item = {'name': version['name'], 'version': version['version'], 'effective': version['effective']}
        try:
            with store.connect() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position', (version['version'],))]
            if rows and all(r['model'] == provider.fingerprint and r['vector'] and r['title_vector'] for r in rows):
                item['status'] = 'skipped'
                report['skipped'] += 1
            else:
                raw = store.directory / 'raw' / (version['version'] + '.xml')
                if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest()[:20] != version['version']:
                    raise ValueError('보관 원문 XML이 없거나 버전 해시가 일치하지 않습니다.')
                if not rows:
                    raise ValueError('재임베딩할 조문이 없습니다.')
                texts = [body_text(r) for r in rows] + [f'{version["name"]} - {r["number"]}({r["title"]})' for r in rows]
                vectors = provider.embed(texts)
                if len(vectors) != len(texts) or len({len(v) for v in vectors}) != 1:
                    raise ValueError('임베딩 응답 개수 또는 차원이 일치하지 않습니다.')
                # No DB mutation until every title/body vector for this version succeeded.
                with store.connect() as db:
                    for i, row in enumerate(rows):
                        body = sqlite_vec.serialize_float32(vectors[i])
                        title = sqlite_vec.serialize_float32(vectors[i + len(rows)])
                        db.execute('UPDATE version_articles SET vector=?,title_vector=?,model=? WHERE version=? AND position=?',
                                   (body,title,provider.fingerprint,version['version'],row['position']))
                        db.execute('UPDATE articles SET vector=?,model=? WHERE law_id IN (SELECT id FROM laws WHERE version=?) AND number=?',
                                   (body,provider.fingerprint,version['version'],row['number']))
                item['status'] = 'completed'
                report['completed'] += 1
        except (SetupError, ValueError, OSError) as exc:
            item.update(status='failed', reason=str(exc))
            report['failed'] += 1
        report['items'].append(item)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        notify(f'{item["name"]} ({item["effective"]}): {item["status"]}' + (f' — {item["reason"]}' if 'reason' in item else ''))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def configure(env_path):
    # dotenv only changes these model entries; it never prints credentials.
    set_key(str(env_path), 'EMBEDDING_MODEL', TARGET)
    set_key(str(env_path), 'CHAT_MODEL', 'gpt-4.1')
    set_key(str(env_path), 'RERANK_MODEL', '')


def main():
    if _MODELS_FROM_PROCESS.get('EMBEDDING_MODEL', TARGET) != TARGET:
        print('프로세스의 EMBEDDING_MODEL이 .env보다 우선합니다. 해당 환경변수를 text-embedding-3-large로 변경한 뒤 다시 실행하세요.')
        return 2
    settings = replace(Settings(), embedding_model=TARGET, chat_model='gpt-4.1', rerank_model='')
    if not settings.api_key:
        print('.env의 LLM_API_KEY 또는 OPENAI_API_KEY가 필요합니다.')
        return 2
    print('앱 서버를 종료한 상태에서 실행하세요. 저장된 모든 개정 버전의 제목·본문을 재임베딩합니다.')
    configure(ROOT / '.env')
    store = Store(settings.data_dir)
    report = reindex(store, Provider(settings), settings.data_dir / 'model-upgrade-report.json')
    print(f'완료 {report["completed"]}, 기존 완료 {report["skipped"]}, 실패 {report["failed"]}')
    if report['failed']:
        print('일부 버전은 이전 모델로 남아 있습니다. 실패 사유를 해결하고 같은 명령을 다시 실행하세요.')
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

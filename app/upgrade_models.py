"""Resume-safe local embedding upgrade from stored article text. Stop the app first."""
import json
import hashlib
from dataclasses import replace
from dotenv import set_key
import sqlite_vec
from .config import Settings, ROOT, _MODELS_FROM_PROCESS
from .laws import SetupError
from .provider import Provider
from .store import Store, body_text, article_vectors

# 제목·본문 임베딩을 교체하는 유지보수 CLI: python -m app.upgrade_models
# 서버를 종료하고 실행합니다. 조문 제목·본문 벡터를 현재 임베딩 모델로 갱신합니다.
TARGET = 'text-embedding-3-large'


def reindex(store, provider, report_path, notify=print):
    report = {'embedding_model': provider.settings.endpoint('embedding').model if hasattr(provider,'settings') else TARGET,
              'completed': 0, 'skipped': 0, 'failed': 0, 'items': []}
    with store.connect() as db:
        versions = [dict(r) for r in db.execute('SELECT * FROM law_versions ORDER BY name,effective')]
    for version in versions:
        item = {'name': version['name'], 'version': version['version'], 'effective': version['effective']}
        try:
            with store.connect() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM version_articles WHERE version=? ORDER BY position', (version['version'],))]
            # DB의 모델 식별자와 제목/본문 벡터가 갖춰진 버전은 건너뛰어 재실행 비용을 줄입니다.
            if rows and all(not body_text(r).strip() or
                            (r['model'] == provider.fingerprint and r['vector'] and r['title_vector']) for r in rows):
                item['status'] = 'skipped'
                report['skipped'] += 1
            else:
                raw = store.directory / 'raw' / (version['version'] + '.xml')
                if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest()[:20] != version['version']:
                    raise ValueError('보관 원문 XML이 없거나 버전 해시가 일치하지 않습니다.')
                if not rows:
                    raise ValueError('재임베딩할 조문이 없습니다.')
                vectors, titles = article_vectors(rows, version['name'], provider)
                # No DB mutation until every title/body vector for this version succeeded.
                # 한 버전의 모든 임베딩 응답이 준비된 다음 DB를 한 번에 갱신합니다.
                # API 실패로 같은 버전 안에 이전/새 모델 벡터가 반쯤 섞이지 않도록 하는 경계입니다.
                with store.connect() as db:
                    for i, row in enumerate(rows):
                        body = sqlite_vec.serialize_float32(vectors[i]) if vectors[i] is not None else None
                        title = sqlite_vec.serialize_float32(titles[i]) if titles[i] is not None else None
                        model = provider.fingerprint if body is not None else None
                        db.execute('UPDATE version_articles SET vector=?,title_vector=?,model=? WHERE version=? AND position=?',
                                   (body,title,model,version['version'],row['position']))
                        db.execute('UPDATE articles SET vector=?,model=? WHERE law_id IN (SELECT id FROM laws WHERE version=?) AND number=?',
                                   (body,model,version['version'],row['number']))
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
    selected = Settings()
    if selected.embedding_backend != 'openai' or selected.chat_backend != 'openai':
        print('사내 서버 설정은 변경하지 않았습니다. 현재 선택한 임베딩 모델로 바꾸려면 python -m app.reindex_embeddings를 사용하세요.')
        return 2
    # local.env가 모델 설정을 덮어쓰는 경우 .env만 수정해서 성공했다고 안내하지 않습니다.
    from .config import _LOCAL_VALUES
    if any(_LOCAL_VALUES.get(k) not in (None,v) for k,v in
           [('EMBEDDING_MODEL',TARGET),('CHAT_MODEL','gpt-4.1'),('RERANK_MODEL','')]):
        print('local.env의 모델 설정이 우선합니다. 설정을 변경한 뒤 python -m app.reindex_embeddings를 사용하세요.')
        return 2
    if _MODELS_FROM_PROCESS.get('EMBEDDING_MODEL', TARGET) != TARGET:
        print('프로세스의 EMBEDDING_MODEL이 .env보다 우선합니다. 해당 환경변수를 text-embedding-3-large로 변경한 뒤 다시 실행하세요.')
        return 2
    settings = replace(selected, embedding_model=TARGET, chat_model='gpt-4.1', rerank_model='')
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

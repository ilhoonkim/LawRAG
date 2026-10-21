"""보관 XML로 모든 수집 버전의 HTML만 재생성합니다. API·임베딩·DB 변경 없음.

실행: python -m app.rebuild_html
다른 저장 폴더: python -m app.rebuild_html --data-dir ./data
"""
import argparse
import hashlib
from pathlib import Path
import re
import sqlite3
import tempfile

from .config import Settings
from .laws import parse_law, parse_admrul, render_html


def rebuild_version(directory, row):
    """원본 버전과 경로를 검증한 뒤 HTML 파일 하나를 원자적으로 교체합니다."""
    version = row['version']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{20}', version):
        raise ValueError('잘못된 수집 버전 식별자입니다.')
    if row['html'] != version + '.html':
        raise ValueError('HTML 파일명과 수집 버전이 일치하지 않습니다.')
    if row['kind'] not in ('law', 'admrul'):
        raise ValueError('지원하지 않는 자료 구분입니다.')
    raw_path = directory / 'raw' / (version + '.xml')
    if not raw_path.is_file():
        raise ValueError('보관 원문 XML이 없습니다. 기존 HTML은 유지합니다.')
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()[:20] != version:
        raise ValueError('보관 원문 XML의 해시가 수집 버전과 다릅니다. 기존 HTML은 유지합니다.')
    law = parse_admrul(raw) if row['kind'] == 'admrul' else parse_law(raw)
    page = render_html(law)
    target = directory / 'html' / row['html']
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 같은 폴더에서 완성한 뒤 교체합니다. Windows에서도 교체 전 파일을 닫습니다.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=version + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(page)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def rebuild_all(directory, notify=print):
    directory = Path(directory)
    database = directory / 'laws.sqlite3'
    if not database.is_file():
        raise ValueError('수집 DB가 없습니다. DATA_DIR 또는 --data-dir 경로를 확인하세요.')
    # Store를 생성하면 마이그레이션이 실행될 수 있어 읽기 전용 SQLite 연결만 사용합니다.
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            'SELECT version,name,kind,effective,html FROM law_versions ORDER BY name,effective,version')]
    finally:
        db.close()
    report = {'total': len(rows), 'completed': 0, 'failed': 0, 'errors': []}
    for index, row in enumerate(rows, 1):
        try:
            rebuild_version(directory, row)
        except (OSError, ValueError) as exc:
            report['failed'] += 1
            report['errors'].append({'version': row['version'], 'reason': str(exc)})
            notify(f'[{index}/{len(rows)}] 실패: {row["name"]} ({row["effective"]}) · {exc}')
        else:
            report['completed'] += 1
            notify(f'[{index}/{len(rows)}] HTML 생성: {row["name"]} ({row["effective"]})')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='보관 XML로 전체 수집 버전의 HTML만 재생성 (API 호출 없음)')
    parser.add_argument('--data-dir', type=Path, help='저장 폴더 (생략하면 .env의 DATA_DIR 사용)')
    args = parser.parse_args(argv)
    try:
        report = rebuild_all(args.data_dir if args.data_dir is not None else Settings().data_dir)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'HTML 재생성 실패: {exc}')
        return 1
    print(f'전체 {report["total"]}개 버전 · HTML 완료 {report["completed"]} · 실패 {report["failed"]}')
    print('법령 본문·임베딩·메타데이터·수집 이력 DB는 변경하지 않았습니다.')
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

"""선택한 임베딩 서버로 저장된 모든 버전을 재색인합니다. .env/local.env는 수정하지 않습니다."""
import argparse
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .laws import SetupError
from .provider import Provider
from .store import Store
from .upgrade_models import reindex


def main(argv=None):
    parser = argparse.ArgumentParser(description='현재 선택된 임베딩 모델로 전체 조문 제목·본문 재색인')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args(argv)
    settings = Settings()
    if args.data_dir:
        settings = replace(settings, data_dir=args.data_dir)
    if not (settings.data_dir / 'laws.sqlite3').is_file():
        print('기존 수집 DB가 없습니다. DATA_DIR를 확인하세요.')
        return 2
    try:
        settings.endpoint('embedding').validate()
        print('앱 서버와 수집 작업을 종료한 상태에서 실행하세요. 모든 저장 버전의 제목·본문 벡터를 갱신합니다.')
        report = reindex(Store(settings.data_dir), Provider(settings),
                         settings.data_dir / 'embedding-reindex-report.json')
    except (SetupError, OSError) as exc:
        print(str(exc))
        return 1
    print(f'완료 {report["completed"]}, 기존 완료 {report["skipped"]}, 실패 {report["failed"]}')
    if report['failed']:
        print('실패 항목은 기존 벡터를 유지합니다. 같은 명령으로 재개하고 실패 0건을 확인한 뒤 검색하세요.')
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

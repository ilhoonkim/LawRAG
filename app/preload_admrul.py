"""지정 행정규칙의 현재 시행 버전을 하나씩 수집합니다.

python -m app.preload_admrul [--list-only] [--rule 행정규칙명]
화면과 같은 파싱·저장 경로를 사용하며 법령의 상하위 수집은 호출하지 않습니다.
"""
import argparse
from datetime import datetime
from pathlib import Path
import re
import sqlite3

from .backfill_hierarchy import write_report
from .catalog import normalize_name
from .config import Settings
from .laws import LawAPI, SetupError
from .management import CollectionInput, Management, now
from .preload_real_estate import indexed, record_discovery_failure
from .provider import Provider
from .store import Store
from .temporal import today_in_korea

# 요청 목록 68행에서 '선령 20년 초과 내항 여객선...'의 띄어쓰기 중복을 합친 67개입니다.
# 명칭 변경·폐지 여부를 추측해서 다른 규칙으로 대체하지 않습니다.
RULES = (
    '강선의 구조기준',
    '강화검사 등에 관한 기준',
    '강화플라스틱(FRP)선의 구조기준',
    '고속기관 등의 정비 점검 확인을 위한 지침',
    '고속선 기준',
    '고체화물 개별운송요건',
    '공기부양정의 구조 및 설비 등에 관한 기준',
    '극지해역 운항선박 기준',
    '낚시터의 시설 및 장비 세부기준',
    '목선의 구조기준',
    '범선의 구조 및 설비 등에 관한 기준',
    '부선의 구조 및 설비 등에 관한 기준',
    '부유식 해상구조물의 구조 및 설비 등에 관한 기준',
    '산적액체위험물 운송선박의 시설 등에 관한 기준',
    '새로운 형식의 선박시설에 대한 잠정기준 마련에 관한 규정',
    '선령 20년 초과 내항 여객선의 선박검사기준',
    '선령기준 초과 유선 및 도선의 선박검사 및 선박관리평가 기준',
    '선박 톤수 측정 요령',
    '선박구명설비기준',
    '선박구획기준',
    '선박기관기준',
    '선박만재흘수선기준',
    '선박방화구조기준',
    '선박법 사무취급 요령',
    '선박복원성기준',
    '선박설비기준',
    '선박소방설비기준',
    '선박안전법 적용을 받는 특수한 구조로 되어 있는 선박의 종류에 대한 고시',
    '선박용물건의 형식승인 시험 및 검정에 관한 기준',
    '선박위치발신장치의 설치기준 및 운영 등에 관한 규정',
    '선박의 에너지효율검사 등에 관한 기준',
    '선박전기설비기준',
    '선박평형수 관리 등을 위한 기준',
    '선박해양오염비상계획서 검인에 관한 업무대행',
    '소형선박의 구조 및 설비 기준',
    '소형유조선의 이중선저구조에 관한 특례기준',
    '수륙양용선박 검사지침',
    '수면비행선박기준',
    '수상레저기구 안전검사 기준',
    '안전복지를 강화한 표준어선형에 관한 기준',
    '알루미늄선의 구조기준',
    '어선 건조·개조허가 오차 허용범위',
    '어선 고속기관 등의 비개방정밀검사 지침',
    '어선구조기준',
    '어선기관기준',
    '어선법 사무취급 요령',
    '어선복원성 및 만재흘수선 기준',
    '어선설비기준',
    '어선용품의 형식승인 시험 및 검정 등에 관한 기준',
    '어선톤수 측정업무의 집행요령',
    '예비검사의 대상 및 기준',
    '위험물 선박운송 기준',
    '잠수선 기준',
    '저인화점연료 추진선박기준',
    '전기추진 선박기준',
    '지정사업장의 설비 및 확인대상 선박용물건 등에 관한 기준',
    '총톤수 10톤 미만 소형어선의 구조 및 설비기준',
    '축전지 추진 어선의 설비기준',
    '카페리선박의 구조 및 설비 등에 관한 기준',
    '플레저보트 검사기준',
    '항만건설작업선 검사 등에 관한 기준',
    '해양수산부 관공선 대체건조에 관한 기준',
    '해양오염방지설비 형식승인을 위한 성능시험 및 검정기준',
    '화물적재고박 등에 관한 기준',
    '환경친화적 선박 및 기자재 인증제도 운영요령',
    '황산화물 배출규제해역 지정 고시',
    '휘발성유기화합물규제항만 지정 등에 관한 기준',
)


def unique_names(names):
    """원래 표시명을 보존하고, API 검색 결과와 동일하게 공백만 무시합니다."""
    unique = {}
    for name in names:
        name = ' '.join(name.split())
        if name:
            unique.setdefault(normalize_name(name), name)
    return list(unique.values())


def current_rule(api, name, as_of):
    """현행 검색 결과 중 기준일까지 발령·시행된 정확한 이름의 최신 한 건.

    과거 연혁의 마지막 항목은 이미 폐지된 규칙일 수 있으므로 현행 검색만 합니다.
    API 페이지를 모두 확인한 뒤 선택해야 첫 페이지의 오래된 버전을 고르지 않습니다.
    """
    def eligible(value):
        if not re.fullmatch(r'\d{8}', value or ''):
            return False
        try:
            return datetime.strptime(value, '%Y%m%d').date() <= as_of
        except ValueError:
            return False

    # API 명칭이 '선박톤수측정요령'처럼 붙어 있으면 원래 검색어로 조회되지 않을
    # 수 있습니다. 결과가 없을 때만 공백 없는 이름으로 재조회하며 일치 조건은 유지합니다.
    for query in dict.fromkeys((name, normalize_name(name))):
        found, seen = {}, set()
        for page in range(1, 201):
            result = api.search(query, page, kind='admrul', revisions=False, display=100)
            rows, total = result['laws'], int(result['total'])
            signature = tuple((r['id'], r['effective']) for r in rows)
            if (not rows and (page - 1) * 100 < total) or (rows and signature in seen):
                raise ValueError('현행 행정규칙 검색 페이지가 누락되거나 반복되었습니다.')
            seen.add(signature)
            for row in rows:
                if normalize_name(row['name']) != normalize_name(name):
                    continue
                status = row.get('status', '')
                if any(word in status for word in ('폐지', '연혁', '예정')) or '폐지' in row.get('amendment', ''):
                    continue
                if not eligible(row['effective']) or (row.get('promulgated') and not eligible(row['promulgated'])):
                    continue
                if not re.fullmatch(r'\d{1,20}', row['id']):
                    raise ValueError('현행 행정규칙의 일련번호가 올바르지 않습니다.')
                found[(row['id'], row['effective'])] = {**row, 'kind': 'admrul'}
            if page * 100 >= total:
                break
        else:
            raise ValueError('현행 행정규칙 조회가 200페이지를 초과했습니다.')
        if found:
            return max(found.values(), key=lambda row: (row['effective'], row.get('promulgated', ''), int(row['id'])))
    raise ValueError(f'{name}: 현재 시행 중인 정확한 명칭의 행정규칙을 확인하지 못했습니다. '
                     '폐지·명칭 변경·시행 예정·API 수록 여부를 확인하세요.')


def collected(manager, row):
    """본문·제목 벡터가 현 모델로 저장되고 원문/HTML도 있을 때만 건너뜁니다."""
    if not indexed(manager, row):
        return False
    with manager.store.connect() as db:
        version = db.execute('SELECT version,html FROM law_versions WHERE kind=? AND mst=? AND effective=?',
                             ('admrul', row['id'], row['effective'])).fetchone()
    directory = manager.store.directory
    return (directory / 'raw' / (version['version'] + '.xml')).is_file() and (directory / 'html' / version['html']).is_file()


def preload(manager, names=RULES, *, list_only=False, notify=print):
    names = unique_names(names)
    if not names:
        raise ValueError('수집할 행정규칙명을 입력하세요.')
    if not list_only:
        manager.require_collection_keys()
    as_of = today_in_korea()
    report = {'started_at': now(), 'as_of': as_of.isoformat(), 'targets': len(names), 'version_limit': 1,
              'list_only': list_only, 'items': [], 'ok': False, 'interrupted': False}
    path = manager.store.directory / 'preload-admrul-report.json'
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
            entry = {'name': name, 'status': 'checking', 'errors': []}
            report['items'].append(entry)
            batch = None
            notify(f'[{index}/{len(names)}] {name}: 현재 시행 버전 조회')
            checkpoint()
            try:
                row = current_rule(manager.api, name, as_of)
                entry.update(selected=row, status='planned')
                notify(f'  {row["name"]} · 시행 {row["effective"]} · 발령 {row.get("promulgated", "")} · ID {row["id"]}')
                if list_only:
                    continue
                if collected(manager, row):
                    entry['status'] = 'already_collected'
                    notify('  기존 수집·임베딩 완료: 건너뜀')
                    continue
                # 현재 버전 하나를 먼저 정한 후 완료 여부를 확인합니다. 재실행 때
                # 완료한 규칙 대신 과거 버전을 추가하거나 전체 DB를 초기화하지 않습니다.
                queued = manager.queue(CollectionInput(items=[{
                    'mst': row['id'], 'kind': 'admrul', 'name': row['name'],
                    'effective': row['effective'], 'promulgated': row.get('promulgated', ''),
                }]), reindex=True)
                batch = queued['batch']
                if not queued['ids']:
                    entry['status'] = 'pending'
                    entry['errors'].append('같은 버전의 수집이 이미 대기/진행 중입니다. 앱 서버 종료 후 다시 실행하세요.')
                    notify('  ' + entry['errors'][-1])
                    continue
                notify('  본문 수집·HTML 생성·제목/본문 임베딩 중')
                manager.run(queued['ids'], with_relations=False)
                with manager.store.connect() as db:
                    event = db.execute('SELECT status,reason,version FROM collection_history WHERE id=?',
                                       (queued['ids'][0],)).fetchone()
                entry.update(status=event['status'], version=event['version'])
                if event['status'] != 'completed':
                    entry['errors'].append(event['reason'])
                notify('  ' + ('완료' if event['status'] == 'completed' else '실패: ' + event['reason']))
            except (SetupError, ValueError) as exc:
                entry['status'] = 'failed'
                entry['errors'].append(str(exc))
                notify('  확인 필요: ' + str(exc))
                if not list_only:
                    record_discovery_failure(manager, {'name': name, 'kind': 'admrul'}, str(exc))
            finally:
                checkpoint()
        report['ok'] = all(r['status'] in ('completed', 'already_collected', 'planned') and not r['errors']
                           for r in report['items'])
    except KeyboardInterrupt:
        report['interrupted'] = True
        if report['items']:
            report['items'][-1]['status'] = 'interrupted'
        if batch:
            with manager.store.connect() as db:
                db.execute("UPDATE collection_history SET status='interrupted',finished_at=?,reason=? "
                           "WHERE batch=? AND status IN ('queued','running')",
                           (now(), '수집이 중단되었습니다. 같은 명령으로 재개하세요.', batch))
        notify('중단했습니다. 같은 명령을 실행하면 완료된 규칙은 건너뜁니다.')
    finally:
        report['finished_at'] = now()
        checkpoint()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='지정 행정규칙 67개의 현재 시행 버전 하나씩 수집·임베딩')
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument('--rule', help='정확한 행정규칙명 하나만 지정 (기본 목록 외 규칙도 가능)')
    targets.add_argument('--names-file', type=Path, help='UTF-8 텍스트, 한 줄에 행정규칙명 하나')
    parser.add_argument('--list-only', action='store_true', help='선택 버전만 조회, 본문 수집·임베딩 없음')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args(argv)
    names = [args.rule] if args.rule else RULES
    if args.names_file:
        try:
            names = args.names_file.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            print('행정규칙명 파일 경로와 UTF-8 인코딩을 확인하세요.')
            return 2
    if not unique_names(names):
        print('수집할 행정규칙명을 입력하세요.')
        return 2
    settings = Settings()
    if not settings.law_oc or (not args.list_only and not settings.api_key):
        print('.env의 LAW_API_OC' + (' 및 LLM_API_KEY(또는 OPENAI_API_KEY)' if not args.list_only else '') + '를 확인하세요.')
        return 2
    print('앱 서버를 종료한 뒤 실행하세요. 한국 날짜 ' + today_in_korea().isoformat() + ' 기준 현재 시행 버전만 수집합니다.')
    try:
        manager = Management(Store(args.data_dir or settings.data_dir), Provider(settings), LawAPI(settings.law_oc),
                             recover_pending=not args.list_only)
        report = preload(manager, names, list_only=args.list_only)
    except (OSError, sqlite3.Error):
        print('DB/파일 처리 실패. 저장 공간·경로를 확인한 뒤 같은 명령으로 재개하세요.')
        return 1
    counts = {status: sum(r['status'] == status for r in report['items'])
              for status in ('completed', 'already_collected', 'planned')}
    print(f'신규 완료 {counts["completed"]} · 기존 완료 {counts["already_collected"]} · 조회 {counts["planned"]}')
    print('결과:', manager.store.directory / 'preload-admrul-report.json')
    print('완료' if report['ok'] else '미완료 항목이 있습니다. 결과 파일과 수집 이력을 확인하세요.')
    return 130 if report['interrupted'] else (0 if report['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())

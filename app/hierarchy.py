"""공식 상하위법 트리와 시행 기간을 이용한 동반 수집 계획.

체계도의 관계와 MST·시행일·공포일을 함께 보관하여 수집할 버전을 정합니다.
동반 법령은 체계도가 제공하는 버전을 우선 사용하고, 빠진 정보만 연혁으로 보완합니다.
"""
import json
import re
from datetime import datetime, timedelta

from .catalog import normalize_name
from .laws import SetupError
from .temporal import today_in_korea


def parse_hierarchy(raw):
    """JSON의 실제 부모·자식 구조를 보존합니다. 이름 접미사로 관계를 만들지 않습니다."""
    try:
        tree = json.loads(raw)['법령체계도']['상하위법']
        if not isinstance(tree, dict):
            raise ValueError()
        nodes, edges = {}, []

        def scalar(value):
            return str(value.get('content', '') if isinstance(value, dict) else value or '').strip()

        def walk(parent, parent_key=None, depth=0):
            if depth > 8 or len(nodes) > 200:
                raise ValueError()
            for kind in ('법률', '시행령', '시행규칙'):
                children = parent.get(kind, [])
                if isinstance(children, dict):
                    children = [children]
                if not isinstance(children, list):
                    raise ValueError()
                for child in children:
                    info = child['기본정보']
                    name = scalar(info.get('법령명') or info.get('법령명_한글'))
                    law_id = scalar(info.get('법령ID'))
                    mst = scalar(info.get('법령일련번호'))
                    if not name or len(name) > 200:
                        raise ValueError()
                    for identifier in (law_id, mst):
                        if identifier and not re.fullmatch(r'\d{1,20}', identifier):
                            raise ValueError()
                    key = law_id or normalize_name(name)
                    node = {'key': key, 'name': name, 'law_id': law_id, 'mst': mst, 'level': kind,
                            'effective': scalar(info.get('시행일자')),
                            'promulgated': scalar(info.get('공포일자'))}
                    if key in nodes and nodes[key] != node:
                        raise ValueError()
                    nodes[key] = node
                    if parent_key:
                        edge = {'parent': parent_key, 'child': key}
                        if edge not in edges:
                            edges.append(edge)
                    walk(child, key, depth + 1)
        walk(tree)
        if not nodes:
            raise ValueError()
        return {'nodes': list(nodes.values()), 'edges': edges}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        # 권한 오류 HTML이나 빈 응답을 '상위법 없음'으로 취급하면 안 됩니다.
        raise SetupError('상하위법 체계도 응답을 확인할 수 없습니다. OC의 법령체계도(lsStmd) API 권한과 응답 형식을 확인하세요.') from None


def revision_rows(manager, node):
    """공식 체계도의 식별자(없으면 정확한 명칭)로 연혁 전체를 검증합니다."""
    found, seen = {}, set()
    for page in range(1, 201):
        result = manager.api.search(node['name'], page, kind='law', revisions=True, display=100)
        rows, total = result['laws'], int(result['total'])
        signature = tuple((r['id'], r['effective']) for r in rows)
        if (not rows and (page - 1) * 100 < total) or (rows and signature in seen):
            raise ValueError('상하위법 연혁 페이지가 누락되거나 반복되었습니다.')
        seen.add(signature)
        for row in rows:
            if node.get('law_id') and row.get('law_id'):
                matches = int(row['law_id']) == int(node['law_id'])
            else:
                matches = normalize_name(row['name']) == normalize_name(node['name'])
            if matches and manager.collectible_date(row['effective']):
                if not re.fullmatch(r'\d{1,20}', row['id']):
                    raise ValueError('상하위법 연혁에 유효한 버전 식별자가 없습니다.')
                if row.get('promulgated') and not manager.collectible_date(row['promulgated']):
                    continue
                found[(row['id'], row['effective'])] = {**row, 'kind': 'law'}
        if page * 100 >= total:
            break
    else:
        raise ValueError('상하위법 연혁 조회 범위를 초과했습니다.')
    rows = sorted(found.values(), key=revision_order)
    with manager.store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO revision_catalog VALUES(?,?,?,?,?)',
            [('law', r['id'], r['name'], r['effective'], r.get('promulgated', '')) for r in rows])
    return rows


def revision_order(row):
    return row['effective'], row.get('promulgated', ''), int(row['id'])


def current_revision(manager, node):
    """현행 검색(nw=3)에서 정확한 법령의 오늘 시행 버전만 선택합니다.

    연혁의 마지막 행은 폐지법일 수도 있습니다. 현행 결과가 없으면 과거 연혁이나
    이름이 비슷한 후속 법으로 대체하지 않고 호출자에게 확인 필요를 알립니다.
    """
    found, seen = {}, set()
    for page in range(1, 201):
        result = manager.api.search(node['name'], page, kind='law', revisions=False, display=100)
        rows, total = result['laws'], int(result['total'])
        signature = tuple((r['id'],r['effective']) for r in rows)
        if (not rows and (page-1)*100 < total) or (rows and signature in seen):
            raise ValueError('현행 법령 검색 페이지가 누락되거나 반복되었습니다.')
        seen.add(signature)
        for row in rows:
            matches = normalize_name(row['name']) == normalize_name(node['name'])
            if node.get('law_id') and row.get('law_id'):
                matches = matches and str(node['law_id']).lstrip('0') == str(row['law_id']).lstrip('0')
            if (matches and manager.collectible_date(row['effective']) and
                (not row.get('promulgated') or manager.collectible_date(row['promulgated'])) and
                '폐지' not in row.get('status','') and '폐지' not in row.get('amendment','')):
                if not re.fullmatch(r'\d{1,20}', row['id']):
                    raise ValueError('현행 법령의 MST가 올바르지 않습니다.')
                found[(row['id'],row['effective'])] = {**row, 'kind':'law'}
        if page*100 >= total:
            break
    else:
        raise ValueError('현행 법령 조회 범위를 초과했습니다.')
    if not found:
        raise ValueError(f'{node["name"]}: 현재 시행 중인 정확한 명칭의 법령을 확인하지 못했습니다. 폐지·명칭 변경·API 수록 여부를 확인하세요.')
    return max(found.values(), key=revision_order)


def build_plan(manager, items, *, current_only=False):
    """선택 항목과 공식 트리의 법령들을 합칩니다. 행정규칙은 자동 확장하지 않습니다."""
    today = today_in_korea().strftime('%Y%m%d')
    planned, families, warnings, cache, trees = {}, [], [], {}, {}

    def add(row, requested=False):
        key = (row['kind'], row['id'], row['effective'])
        if key not in planned:
            planned[key] = {'mst': row['id'], 'kind': row['kind'], 'name': row['name'],
                'effective': row['effective'], 'promulgated': row.get('promulgated', ''), 'requested': requested}
        elif requested:
            planned[key]['requested'] = True
        if len(planned) > 500:
            raise ValueError('동반 수집 대상이 500개 버전을 초과했습니다. 선택한 개정 버전 수를 줄여 주세요.')

    def revisions(node):
        key = node.get('law_id') or normalize_name(node['name'])
        if key not in cache:
            cache[key] = revision_rows(manager, node)
        return cache[key]

    for item in items:
        if item.effective and not manager.collectible_date(item.effective):
            raise ValueError('시행 예정이거나 시행일이 잘못된 버전은 수집할 수 없습니다.')
        if item.kind != 'law':
            add({'id': item.mst, **item.model_dump()}, True)
            continue
        if item.mst not in trees:
            trees[item.mst] = manager.api.hierarchy(item.mst)
        tree = trees[item.mst]
        sources = [n for n in tree['nodes'] if n['mst'] == item.mst or
                   normalize_name(n['name']) == normalize_name(item.name)]
        if len(sources) != 1:
            raise ValueError(f'{item.name}: 체계도에서 선택 법령을 유일하게 확인하지 못했습니다.')
        source = sources[0]
        source_rows = [current_revision(manager, source)] if current_only else revisions(source)
        matches = [r for r in source_rows if r['id'] == item.mst and
                   (not item.effective or r['effective'] == item.effective)]
        if not matches:
            raise ValueError(f'{item.name}: 선택한 시행 버전을 연혁에서 확인하지 못했습니다.')
        selected = max(matches, key=revision_order)
        if current_only:
            source.update(mst=selected['id'], effective=selected['effective'], promulgated=selected.get('promulgated',''))
        start = selected['effective']
        stop = next((r['effective'] for r in source_rows if r['effective'] > start), None)
        add(selected, True)
        # 선택 버전이 현재 적용 중이면 오늘의 체계도를 사용합니다. 과거 버전은 다음
        # 개정 직전까지만 허용하여, 현재 체계도에 섞여 오는 훗날의 버전을 걸러냅니다.
        cutoff = min(today, (datetime.strptime(stop, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')) if stop else today
        family = {'mst': item.mst, 'name': item.name, 'effective': start, 'until': stop, 'as_of': cutoff,
                  'tree': tree, 'members': []}
        for node in tree['nodes']:
            if node['key'] == source['key']:
                continue
            # 체계도에 내려온 버전을 우선 사용합니다. 모든 동반 법령의 연혁을 다시
            # 조회하지 않습니다. 누락/예정/기간 밖인 경우에만 기준일 버전을 찾습니다.
            direct = (node.get('mst') and manager.collectible_date(node.get('effective')) and
                      manager.collectible_date(node.get('promulgated')) and
                      node['effective'] <= cutoff and node['promulgated'] <= cutoff)
            if current_only:
                try:
                    companions = [current_revision(manager, node)]
                except ValueError as exc:
                    warnings.append(str(exc))
                    companions = []
            elif direct:
                companions = [{'id': node['mst'], 'name': node['name'], 'kind': 'law',
                               'effective': node['effective'], 'promulgated': node['promulgated']}]
            else:
                eligible = [r for r in revisions(node) if r['effective'] <= cutoff and
                            (not r.get('promulgated') or r['promulgated'] <= cutoff)]
                companions = [max(eligible, key=revision_order)] if eligible else []
            if not companions:
                warnings.append(f'{node["name"]}: 선택한 {item.name} 시행 기간에 적용할 버전이 확인되지 않았습니다.')
            for row in companions:
                if current_only:
                    node.update(mst=row['id'], effective=row['effective'], promulgated=row.get('promulgated',''))
                add(row)
                family['members'].append({'mst': row['id'], 'effective': row['effective'], 'name': row['name']})
        families.append(family)
    result = list(planned.values())
    with manager.store.connect() as db:
        # 본문 수집 실패 시 더 오래된 수집본으로 조용히 대체하지 않도록, 체계도에서
        # 확인한 버전도 연혁 카탈로그에 남깁니다. 저장됨과 조회됨은 다른 상태입니다.
        db.executemany('INSERT OR REPLACE INTO revision_catalog VALUES(?,?,?,?,?)',
            [(r['kind'], r['mst'], r['name'], r['effective'], r['promulgated']) for r in result if r['effective']])
        for row in result:
            row['collected'] = bool(db.execute('SELECT 1 FROM law_versions WHERE kind=? AND mst=? AND effective=?',
                (row['kind'], row['mst'], row['effective'])).fetchone())
    return {'items': result, 'families': families, 'warnings': list(dict.fromkeys(warnings)),
            'new_count': sum(not r['collected'] for r in result), 'as_of': today}

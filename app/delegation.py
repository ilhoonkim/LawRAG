"""위임조문 3단비교 원본 → 조별 관계 → 검증된 수집 버전 간 연결.

API의 비교 행은 법률을 중심으로 한 묶음이지 시행령×시행규칙의 직접 위임
행렬이 아닙니다. 따라서 비교 관계와 본문에서 확인한 인용 관계를 구별합니다.
응답의 공포/시행 정보와 조 전체 본문이 일치하는 버전에만 연결하며, 대상이
없거나 버전이 모호하면 원본과 미해결 사유를 남겨 이후 수집 때 재매핑합니다.
"""
from datetime import datetime, timezone
import html
import json
import re
from urllib.parse import quote

from .catalog import normalize_name
from .laws import SetupError


def now():
    return datetime.now(timezone.utc).isoformat()


def items(value):
    if value in (None, ''):
        return []
    result = value if isinstance(value, list) else [value]
    if any(not isinstance(x, dict) for x in result):
        raise SetupError('위임조문 3단비교의 조문 형식을 확인할 수 없습니다.')
    return result


def text(value):
    return str(value).strip() if isinstance(value, (str, int)) else ''


def plain(value):
    # 실제 HTML 태그만 제거합니다. <개정 ...> 같은 법령 주석은 보존합니다.
    return html.unescape(re.sub(r'</?(?:a|span|div|p|br|b|strong|em|sup|sub|font)\b[^>]*>', '', text(value), flags=re.I))


def body_key(value):
    value = plain(value)
    value = re.sub(r'^\s*제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(?:\([^)]*\))?\s*', '', value)
    return re.sub(r'\s+', '', value)


def date_value(value):
    value = text(value)
    match = re.fullmatch(r'(\d{4})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})[.일\s]*', value)
    if not match:
        return ''
    try:
        return datetime(*map(int, match.groups())).strftime('%Y%m%d')
    except ValueError:
        return ''


def summary_date(summary):
    # 요약정보의 공포일은 [법률 제...호, YYYY. M. D., 일부개정] 형식입니다.
    # 시행일이 함께 쓰였거나 여러 날짜가 있으면 임의로 고르지 않습니다.
    publication = re.findall(r'호\s*,\s*(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.)', plain(summary))
    if len(publication) == 1:
        return date_value(publication[0])
    dates = re.findall(r'\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.', plain(summary))
    return date_value(dates[0]) if len(dates) == 1 and '시행' not in plain(summary) else ''


def summary_effective(summary):
    dates = re.findall(r'시행\s+(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.)', plain(summary))
    return date_value(dates[0]) if len(dates) == 1 else ''


def parse(raw):
    """공식 필드명을 유지한 원본에서 비교 edge 후보를 만듭니다. 단일 객체도 허용합니다."""
    try:
        data = json.loads(raw)
        service = data['LspttnThdCmpLawXService']
        basic = service['기본정보']
        if not isinstance(basic, dict):
            raise TypeError
        exists = basic.get('삼단비교존재여부')
        group = service.get('위임조문삼단비교')
        rows = items(group.get('법률조문')) if isinstance(group, dict) else []
        if not rows and exists != 'N':
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise SetupError('위임조문 3단비교 JSON 형식 또는 이용 권한을 확인하세요.') from None

    def article(row, level):
        prefix = {'법률': '법령', '시행령': '시행령', '시행규칙': '시행규칙'}[level]
        name = text(row.get('법령명') or row.get(prefix+'명') or row.get('법령명_한글') or basic.get(prefix+'명'))
        # 다른 이름의 하위 규칙에는 기본정보의 대표 규칙 ID/날짜를 적용하지 않습니다.
        fallback = normalize_name(name) == normalize_name(text(basic.get(prefix+'명')))
        number, branch = text(row.get('조번호')), text(row.get('조가지번호') or '0')
        if not number.isdigit() or not branch.isdigit() or not int(number) or not name:
            raise SetupError('위임조문 3단비교의 법령명 또는 조번호가 없습니다.')
        summary = row.get('법령요약정보') or (basic.get(prefix+'요약정보') if fallback else '')
        return dict(name=name, level=level,
            law_id=text(row.get('법령ID') or (basic.get(prefix+'ID') if fallback else '')),
            effective=date_value(row.get('시행일자') or (basic.get(prefix+'시행일자') if fallback else '')) or summary_effective(summary),
            promulgated=date_value(row.get('공포일자') or (basic.get(prefix+'공포일자') if fallback else '')) or summary_date(summary),
            number=f'제{int(number)}조'+(f'의{int(branch)}' if int(branch) else ''),
            title=plain(row.get('조제목')), body=plain(row.get('조내용')))

    edges = []
    for row in rows:
        # 편/장 표제가 조번호를 공유하는 경우가 있습니다. 본문 조문으로 연결하지 않습니다.
        if re.match(r'^제\s*\d+\s*[편장절관]', plain(row.get('조내용')).strip()):
            continue
        parent = article(row, '법률')
        for level in ('시행령', '시행규칙'):
            for child in items(row.get(level+'조문')):
                edges.append({'parent': parent, 'child': article(child, level),
                              'relation': 'delegation_comparison'})
        # 위임행정규칙목록은 이 기능의 수집/탐색 범위가 아닙니다.
    return {'basic': basic, 'edges': edges, 'exists': exists != 'N'}


def request_mst(store, version):
    """공식 체계도의 법률 MST로 묶어 같은 가족을 조문마다 재요청하지 않습니다."""
    with store.connect() as db:
        row = db.execute('SELECT * FROM law_versions WHERE version=?', (version,)).fetchone()
        if row is None or row['kind'] != 'law':
            return None
        mst = row['mst'] or ''
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='law_hierarchy_snapshots'").fetchone():
            snapshot = db.execute('SELECT payload FROM law_hierarchy_snapshots WHERE mst=? AND effective=?',
                                  (mst, row['effective'])).fetchone()
            if snapshot:
                parents = [n for n in json.loads(snapshot['payload'])['tree']['nodes'] if n['level'] == '법률']
                if len(parents) == 1 and parents[0].get('mst'):
                    mst = parents[0]['mst']
    if not re.fullmatch(r'\d{1,20}', mst):
        raise SetupError('위임관계 조회용 MST가 없습니다. 먼저 상하위법 보완 수집을 실행하세요.')
    return mst


def bind(store, mst):
    """보관한 응답을 다시 매핑합니다. API/LLM 호출 없이 늦게 수집된 대상을 연결합니다."""
    current = {r['version'] for r in store.temporal_versions()['versions']}
    with store.connect() as db:
        snapshot = db.execute('SELECT * FROM delegation_snapshots WHERE mst=?', (mst,)).fetchone()
        if not snapshot or not snapshot['payload']:
            return
        parsed = parse(snapshot['payload'])
        stored = [dict(r) for r in db.execute('''SELECT v.version,v.law_id,v.name,v.effective,v.promulgated,
            a.number,a.title,a.body FROM law_versions v JOIN version_articles a ON a.version=v.version WHERE v.kind='law' ''')]
        indexed = {}
        # 공포일 하나로 시행 버전 여러 개가 생길 수 있습니다. 응답에 시행일이 없으면
        # 연혁 카탈로그로 확인한 현재 버전에만 연결하고 과거 버전에는 적용하지 않습니다.
        catalog_verified = {r['version'] for r in db.execute('''SELECT DISTINCT v.version FROM law_versions v
            JOIN revision_catalog c ON c.kind=v.kind AND c.mst=v.mst AND c.effective=v.effective
            WHERE c.promulgated=v.promulgated''')}
        for row in stored:
            indexed.setdefault((normalize_name(row['name']), row['number']), []).append(row)
        cache = {}

        def match(node):
            identity = json.dumps(node, sort_keys=True, ensure_ascii=False)
            if identity not in cache:
                candidates = indexed.get((normalize_name(node['name']), node['number']), [])
                # MST는 조회 selector일 뿐입니다. 날짜도 본문도 없는 응답을 제목만으로 연결하지 않습니다.
                candidates = [r for r in candidates if (node['effective'] or node['promulgated']) and body_key(node['body'])
                    and body_key(r['body']) == body_key(node['body'])
                    and (not node['law_id'] or r['law_id'].lstrip('0') == node['law_id'].lstrip('0'))
                    and (not node['effective'] or r['effective'] == node['effective'])
                    and (node['effective'] or (r['version'] in current and r['version'] in catalog_verified))
                    and (not node['promulgated'] or r['promulgated'] == node['promulgated'])]
                cache[identity] = candidates[0] if len(candidates) == 1 else None
            return cache[identity]

        db.execute('DELETE FROM delegation_edges WHERE snapshot_mst=?', (mst,))
        db.execute('DELETE FROM delegation_unresolved WHERE snapshot_mst=?', (mst,))
        unresolved, bound = 0, []
        for candidate in parsed['edges']:
            parent, child = match(candidate['parent']), match(candidate['child'])
            if not parent or not child:
                db.execute('INSERT INTO delegation_unresolved VALUES(?,?,?,?)', (mst, unresolved,
                    json.dumps(candidate, ensure_ascii=False), '응답의 날짜·법령 식별자·조 전체 본문과 일치하는 수집 버전을 유일하게 확인하지 못했습니다.'))
                unresolved += 1
                continue
            bound.append((parent, child, candidate))

        def save(parent, child, relation, provenance):
            db.execute('INSERT OR IGNORE INTO delegation_edges VALUES(?,?,?,?,?,?,?)',
                (mst, parent['version'], parent['number'], child['version'], child['number'], relation,
                 json.dumps(provenance, ensure_ascii=False)))

        for parent, child, candidate in bound:
            save(parent, child, 'delegation_comparison', {'source': 'thdCmp', 'knd': 2,
                'requested_mst': mst, 'fetched_at': snapshot['fetched_at'],
                'group_law': parent['name'], 'group_article': parent['number'],
                'note': '법률 중심 위임조문 비교 관계. 같은 행의 시행령·규칙 사이 직접 위임을 뜻하지 않음.'})
        # 같은 비교 묶음 안에서도 규칙의 실제 '영 제N조' 인용이 있는 쌍만 추가합니다.
        # 여기서 저장하는 방향은 상위→하위이며, 인용의 원문 방향은 하위→상위입니다.
        from .graph import aliases, links
        documents = {}
        for parent, child, candidate in bound:
            if candidate['child']['level'] != '시행규칙':
                continue
            decrees = [c for p,c,e in bound if p['version'] == parent['version'] and p['number'] == parent['number']
                       and e['child']['level'] == '시행령']
            if child['version'] not in documents:
                documents[child['version']] = aliases('\n'.join(r['body'] for r in stored if r['version'] == child['version']))
            declared = dict(documents[child['version']])
            names = {d['name'] for d in decrees}
            if len(names) == 1:
                declared.setdefault('영', next(iter(names)))
                declared.setdefault('시행령', next(iter(names)))
            for number, name, _, excerpt in links({**child, 'law_name': child['name']}, declared):
                for decree in decrees:
                    if number == decree['number'] and normalize_name(name) == normalize_name(decree['name']):
                        save(decree, child, 'hierarchy_citation', {'source': 'thdCmp+article_text', 'knd': 2,
                             'excerpt': excerpt, 'excerpt_version': child['version'],
                             'note': '위임조문 비교 묶음 안에서 확인한 하위→상위 명시 인용. 직접 위임의 법적 판단과 구별.'})
        status = 'partial' if unresolved else ('completed' if parsed['exists'] else 'unavailable')
        db.execute('UPDATE delegation_snapshots SET status=?,reason=? WHERE mst=?',
            (status, f'버전 확인 또는 본문 수집 필요: {unresolved}개 관계' if unresolved else '', mst))


def refresh(store, api, version, attempted=None):
    """관계 실패는 본문/임베딩 완료와 분리합니다. 한 작업에서는 가족별 한 번 요청합니다."""
    mst = request_mst(store, version)
    if mst is None:
        return None
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO delegation_sources VALUES(?,?)', (version, mst))
    if attempted is None or mst not in attempted:
        if attempted is not None:
            attempted.add(mst)
        with store.connect() as db:
            db.execute('''INSERT INTO delegation_snapshots(mst,fetched_at,status,reason) VALUES(?,?,?,?)
                ON CONFLICT(mst) DO UPDATE SET status=excluded.status,reason=excluded.reason''',
                (mst, now(), 'running', '위임조문 3단비교 조회 중'))
        try:
            raw = api.delegation(mst)
            # 실패 응답의 내용/URL은 저장하지 않습니다. 성공 JSON만 원본 그대로 보존합니다.
            parse(raw)
            payload = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO delegation_snapshots VALUES(?,?,?,?,?)',
                           (mst, payload, now(), 'pending', ''))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SetupError) else '위임조문 3단비교 조회·파싱 실패. API 이용 권한과 응답 형식을 확인하세요.'
            with store.connect() as db:
                # 이전에 확인한 관계는 보존하고 마지막 갱신 실패만 기록합니다.
                db.execute('''INSERT INTO delegation_snapshots(mst,fetched_at,status,reason) VALUES(?,?,?,?)
                    ON CONFLICT(mst) DO UPDATE SET status=excluded.status,reason=excluded.reason''',
                    (mst, now(), 'failed', reason))
            return status(store, version)
    with store.connect() as db:
        snapshot = db.execute('SELECT status FROM delegation_snapshots WHERE mst=?', (mst,)).fetchone()
    if snapshot and snapshot['status'] != 'failed':
        bind(store, mst)
    return status(store, version)


def status(store, version):
    with store.connect() as db:
        row = db.execute('''SELECT s.mst,s.status,s.reason,s.fetched_at,
            (SELECT count(*) FROM delegation_edges e WHERE e.snapshot_mst=s.mst) edge_count,
            (SELECT count(*) FROM delegation_unresolved u WHERE u.snapshot_mst=s.mst) unresolved_count
            FROM delegation_sources v JOIN delegation_snapshots s ON s.mst=v.mst WHERE v.version=?''', (version,)).fetchone()
    return dict(row) if row else {'status': 'uncollected', 'reason': '위임관계를 아직 조회하지 않았습니다.'}


def active_edges(store, cutoff):
    """양쪽 모두 동일 기준일에 선택된 정확한 버전인 관계만 제공합니다."""
    selected = {v['version']: v for v in store.temporal_versions(cutoff)['versions']}
    with store.connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM delegation_edges ORDER BY snapshot_mst,from_version,from_article,to_version,to_article,relation')]
    result, seen = [], set()
    for row in rows:
        signature = tuple(row[k] for k in ('from_version','from_article','to_version','to_article','relation'))
        if row['from_version'] not in selected or row['to_version'] not in selected or signature in seen:
            continue
        seen.add(signature)
        result.append({**row, 'from_law': selected[row['from_version']]['name'],
                       'to_law': selected[row['to_version']]['name'], 'provenance': json.loads(row['provenance'])})
    return result


def missing_context(store, roots):
    """출발 조문을 확인할 수 있으나 상대 버전을 못 찾은 관계만 충분성 검토에 알립니다."""
    result, seen = [], set()
    with store.connect() as db:
        rows = db.execute('SELECT payload,reason FROM delegation_unresolved').fetchall()
        metadata = {r['version']:dict(r) for r in db.execute('SELECT version,law_id,effective,promulgated FROM law_versions')}
    for row in rows:
        edge = json.loads(row['payload'])
        for side, other in [('parent','child'),('child','parent')]:
            own, target = edge[side], edge[other]
            for root in roots:
                meta = metadata.get(root['version'], {})
                if (normalize_name(own['name']) != normalize_name(root['law_name']) or own['number'] != root['number']
                    or not body_key(own['body']) or body_key(own['body']) != body_key(root['body'])
                    or not (own['effective'] or own['promulgated'])
                    or (own['effective'] and own['effective'] != meta.get('effective'))
                    or (own['promulgated'] and own['promulgated'] != meta.get('promulgated'))):
                    continue
                signature = (root['version'],root['number'],target['name'],target['number'])
                if signature not in seen:
                    seen.add(signature)
                    result.append({'law_name':target['name'], 'number':target['number'], 'version':root['version'],
                        'from_article':root['number'], 'reason':row['reason'], 'relation':'delegation_comparison'})
    return result


def html_links(store, version, cutoff):
    """HTML에는 한 단계의 양방향 링크만 표시합니다. 클릭 시 대상 버전 HTML로 이동합니다."""
    result, seen = {}, set()
    with store.connect() as db:
        for edge in active_edges(store, cutoff):
            for side, other, label in [('from','to','하위'), ('to','from','상위')]:
                if edge[side+'_version'] != version:
                    continue
                target_version, number = edge[other+'_version'], edge[other+'_article']
                own_number = edge[side+'_article']
                key = (own_number, target_version, number)
                if key in seen:
                    continue
                node = db.execute('SELECT title,section_id FROM version_articles WHERE version=? AND number=?', (target_version, number)).fetchone()
                if not node:
                    continue
                seen.add(key)
                result.setdefault(own_number, []).append(dict(name=edge[other+'_law'], number=number, title=node['title'],
                    label=label+(' 위임조문 비교' if edge['relation']=='delegation_comparison' else ' 인용 조문'),
                    url='/documents/'+target_version+'.html?as_of='+cutoff.isoformat()+'#'+quote(node['section_id'],safe='')))
    return result


def expand(store, roots, cutoff, limit=40):
    """검증된 검색 근거에서 위쪽/아래쪽을 각각 최대 두 단계 탐색합니다.

    위로 갔다가 다른 가지로 내려오는 형제 확장은 하지 않습니다. 원래 관계 방향,
    비교 API 출처, 실제 인용 여부를 보존하며 연결 조문은 검색 점수를 새로 받지 않습니다.
    """
    edges = active_edges(store, cutoff)
    adjacency = {'up': {}, 'down': {}}
    for edge in edges:
        for direction, side in [('up','to'), ('down','from')]:
            adjacency[direction].setdefault((edge[side+'_version'],edge[side+'_article']), []).append(edge)
    added, missing = {}, missing_context(store, roots)
    roots_keys = {(r['version'],r['number']) for r in roots}
    with store.connect() as db:
        def node(version, number):
            row = db.execute('''SELECT a.number,a.title,a.body,a.section_id,a.chapter,v.version,v.name law_name,
                v.law_id,v.effective,v.source_url FROM version_articles a JOIN law_versions v ON v.version=a.version
                WHERE a.version=? AND a.number=?''', (version,number)).fetchone()
            if not row:
                return None
            result = dict(row)
            result.update(id=version+':'+number, html_url='/documents/'+version+'.html?as_of='+cutoff.isoformat()+'#'+quote(result['section_id'],safe=''))
            return result

        for root in roots:
            for direction in ('up','down'):
                frontier = [((root['version'],root['number']), [])]
                visited = {(root['version'],root['number'])}
                for depth in range(2):
                    next_frontier = []
                    for origin, path in frontier:
                        for edge in adjacency[direction].get(origin, []):
                            side = 'from' if direction == 'up' else 'to'
                            target = (edge[side+'_version'],edge[side+'_article'])
                            if target in visited:
                                continue
                            visited.add(target)
                            provenance = edge['provenance']
                            step = {k:edge[k] for k in ('relation','from_version','from_law','from_article','to_version','to_law','to_article')}
                            step.update(provenance=provenance, explicit_citation=edge['relation']=='hierarchy_citation',
                                        excerpt=provenance.get('excerpt',''), excerpt_version=provenance.get('excerpt_version',''))
                            chain = path+[step]
                            if target not in added and target not in roots_keys and len(added) >= limit:
                                missing.append({'law_name':edge[side+'_law'], 'number':target[1], 'version':target[0],
                                                'reason':'상하위 조문 확장 개수 제한으로 제외되었습니다.'})
                                continue
                            target_node = node(*target)
                            if not target_node:
                                continue
                            next_frontier.append((target, chain))
                            if target in roots_keys:
                                continue
                            if target not in added:
                                added[target] = {**target_node, 'score':root.get('score',0), 'relation':'delegation_context',
                                    'referenced_by':root['number'], 'referenced_by_law':root['law_name'],
                                    'reference_edges':chain, 'reference_titles':[], 'evidence_role':'supporting',
                                    'include_in_sources':True, 'source_type':'hierarchy_connection'}
                            else:
                                for step in chain:
                                    if step not in added[target]['reference_edges']:
                                        added[target]['reference_edges'].append(step)
                    frontier = next_frontier
    return list(added.values()), missing

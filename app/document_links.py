"""보관 원문의 상위법 참조를 같은 기준일의 저장된 HTML로 연결합니다.

문서 열람에는 API 호출이나 재임베딩이 없습니다. 날짜별 링크는 응답을 만들 때만
붙여서, 한 버전의 정적 HTML을 다른 기준일의 링크로 덮어쓰지 않습니다.
"""
import json
from urllib.parse import quote

from .catalog import normalize_name
from .graph import aliases
from .references import reference_target


def hierarchy_aliases(store, mst, effective, name):
    """공식 트리에 유일한 조상이 있을 때만 '법'·'영'의 기본 대상을 정합니다."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='law_hierarchy_snapshots'").fetchone():
            return {}
        snapshot = db.execute('SELECT payload FROM law_hierarchy_snapshots WHERE mst=? AND effective=?',
                              (mst, effective)).fetchone()
    if snapshot is None:
        return {}
    tree = json.loads(snapshot['payload'])['tree']
    nodes = {n['key']: n for n in tree['nodes']}
    own = [n['key'] for n in nodes.values() if normalize_name(n['name']) == normalize_name(name)]
    if len(own) != 1:
        return {}
    ancestors, pending = set(), own[:]
    while pending:
        child = pending.pop()
        for edge in tree['edges']:
            parent = edge['parent']
            if edge['child'] == child and parent not in ancestors:
                ancestors.add(parent)
                pending.append(parent)
    result = {}
    for level, short_names in [('법률', ('법',)), ('시행령', ('영', '시행령'))]:
        names = {nodes[k]['name'] for k in ancestors if nodes[k]['level'] == level}
        if len(names) == 1:
            result.update(dict.fromkeys(short_names, next(iter(names))))
    return result


def reference_resolver(store, law, as_of):
    """약칭 선언 > 공식 상위법 순서. 대상 조문/별표가 저장돼 있을 때만 링크합니다."""
    with store.connect() as db:
        source = db.execute('SELECT mst FROM law_versions WHERE version=?', (law.version,)).fetchone()
    declared = hierarchy_aliases(store, source['mst'], law.effective, law.name) if source else {}
    declared.update(aliases('\n'.join(a.body for a in law.articles)))
    versions = store.temporal_versions(as_of)['versions']
    named = {}
    for version in versions:
        if version['kind'] == 'law':
            named.setdefault(normalize_name(version['name']), []).append(version)
    cache = {}

    def resolve(article, match, number):
        target = reference_target(article.body, match.start(), law.name)
        target = target.replace('(대상 확인 필요)', '')
        target = declared.get(target, target)
        if normalize_name(target) == normalize_name(law.name):
            return None
        candidates = named.get(normalize_name(target), [])
        if len(candidates) != 1:
            return None
        version = candidates[0]
        key = (version['version'], number)
        if key not in cache:
            table = 'annex_nodes' if number.startswith('별표') else 'version_articles'
            with store.connect() as db:
                row = db.execute(f'SELECT number,title,section_id FROM {table} WHERE version=? AND number=?', key).fetchone()
            cache[key] = dict(row) if row else None
        node = cache[key]
        if not node:
            return None
        return {**node, 'name': version['name'], 'url': '/documents/' + version['html'] +
                '?as_of=' + as_of.isoformat() + '#' + quote(node['section_id'], safe='')}
    return resolve

"""보관 원문의 상위법 참조를 같은 기준일의 저장된 HTML로 연결합니다.

문서 열람에는 API 호출이나 재임베딩이 없습니다. 앱은 질문 기준일의 링크를 만들고,
저장 HTML은 재생성 시 확인한 기준일의 상대 파일 링크를 사용합니다.
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


def document_url(filename, section_id, as_of, *, standalone=False):
    """직접 여는 HTML은 같은 폴더의 파일, 앱에서는 기준일을 보존하는 문서 API로 이동합니다."""
    prefix = '' if standalone else '/documents/'
    query = '' if standalone else '?as_of=' + as_of.isoformat()
    fragment = '#' + quote(section_id, safe='') if section_id else ''
    return prefix + filename + query + fragment


def reference_resolver(store, law, as_of, *, standalone=False):
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

    class Resolver:
        """렌더러의 조번호 콜백과 인용 법령명 링크가 같은 대상·기준일을 공유합니다."""
        def law_name(self, name):
            # 전체 법령명은 약칭으로 추측하지 않고 정확히 일치하는 저장 문서만 연결합니다.
            candidates = named.get(normalize_name(name), [])
            if len(candidates) != 1 or normalize_name(name) == normalize_name(law.name):
                return None
            version = candidates[0]
            return {'name':version['name'], 'url':document_url(
                version['html'], '', as_of, standalone=standalone)}

        def __call__(self, article, match, number):
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
            return {**node, 'name': version['name'], 'url': document_url(
                version['html'], node['section_id'], as_of, standalone=standalone)}
    return Resolver()

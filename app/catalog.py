"""Exact user-specified names; registration does not imply successful collection."""
import json
from pathlib import Path
import re

CATALOG = json.loads((Path(__file__).parent / 'catalogs' / 'real_estate.json').read_text(encoding='utf-8'))


def normalize_name(name):
    return re.sub(r'\s+', '', name).strip('「」')


# 카테고리 초기 등록일 뿐 자동 수집이 아닙니다. 등록 이력으로 반복 초기화를 피하고,
# 과거 명칭에 실제 수집 버전이 있는 경우에는 해당 카테고리를 보존합니다.
def register_catalog(db):
    # The user supplied these exact names; registration is not collection.
    db.execute('CREATE TABLE IF NOT EXISTS catalog_registrations (id TEXT PRIMARY KEY)')
    if db.execute('SELECT 1 FROM catalog_registrations WHERE id=?', (CATALOG['id'],)).fetchone():
        return
    db.executemany('INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)',
                   [(item['name'],item['kind']) for item in CATALOG['items']])
    for item in CATALOG['items']:
        for alias in item.get('aliases', []):
            db.execute('''DELETE FROM categories WHERE name=? AND kind=? AND NOT EXISTS
                (SELECT 1 FROM law_versions WHERE kind=? AND compact_name(name)=?)''',
                (alias,item['kind'],item['kind'],normalize_name(alias)))
    db.execute('INSERT INTO catalog_registrations VALUES(?)', (CATALOG['id'],))

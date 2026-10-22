import json

import pytest

from app import backfill_hierarchy as task
from app.config import Settings
from app.management import CollectionInput, Management
from app.store import Store
from test_hierarchy_collection import HierarchyAPI, ROWS
from test_workflow import FakeProvider


def seed(manager, mst='300', *, missing_mst=False):
    row = next(r for r in manager.api.rows if r['id'] == mst)
    result = manager.store.ingest(manager.api.fetch(mst, effective=row['effective']), FakeProvider(),
                                  mst='' if missing_mst else mst, archive_only=True)
    # The synthetic XML helper uses a name as its ID; the real API uses a numeric ID.
    with manager.store.connect() as db:
        db.execute('UPDATE law_versions SET law_id=? WHERE version=?', (row['law_id'], result['version']))
    manager.api.fetched.clear()
    return result['version']


@pytest.fixture
def manager(tmp_path):
    manager = Management(Store(tmp_path), FakeProvider(), HierarchyAPI())
    seed(manager)
    return manager


def test_backfill_only_existing_roots_no_recursive_growth_and_reuses_stored(manager):
    original = (manager.store.directory / 'raw').glob('*.xml')
    original = {p.name: p.read_bytes() for p in original}
    report = task.backfill(manager, notify=lambda _: None)
    assert report['sources'] == 1 and len(report['items']) == 1
    assert report['ok'] and report['collected'] == 3 and report['failed'] == 0
    assert set(manager.api.fetched) == {'100', '200', '400'}
    assert all((manager.store.directory/'raw'/name).read_bytes() == raw for name, raw in original.items())
    assert manager.history(1)['total'] == 3
    assert all(e['hierarchy_status'] == 'completed' for e in manager.history(1)['items'])
    assert json.loads((manager.store.directory/'hierarchy-backfill-report.json').read_text())['ok']
    manager.api.fetched.clear()
    repeated = task.backfill(manager, notify=lambda _: None)
    assert repeated['sources'] == 4  # next run explicitly snapshots the expanded DB
    assert repeated['ok'] and repeated['collected'] == 0 and not manager.api.fetched


def test_failed_shared_version_attempted_once_then_resumed(manager):
    seed(manager, '400')
    manager.api.fail.add('200')
    report = task.backfill(manager, notify=lambda _: None)
    assert not report['ok'] and report['failed'] == 1
    assert manager.api.fetched.count('200') == 1
    assert all(e['status'] == 'incomplete' for e in report['items'])
    manager.api.fail.clear()
    manager.api.fetched.clear()
    # Restart uses persistent DB completion, not JSON or the prior instance.
    (manager.store.directory/'hierarchy-backfill-report.json').write_text('invalid JSON')
    restarted = Management(Store(manager.store.directory), FakeProvider(), manager.api)
    report = task.backfill(restarted, notify=lambda _: None)
    assert report['ok'] and report['collected'] == 1
    assert manager.api.fetched == ['200']


def test_list_only_no_body_calls_embeddings_or_history_changes(manager):
    manager.queue(CollectionInput(items=[{'mst': '100', 'name': ROWS[0]['name'], 'effective': ROWS[0]['effective']}]))
    with manager.store.connect() as db:
        history = [tuple(r) for r in db.execute('SELECT * FROM collection_history')]
    manager.provider = object()  # No embedding or key methods may be called.
    report = task.backfill(manager, list_only=True, notify=lambda _: None)
    assert report['ok'] and report['items'][0]['status'] == 'planned'
    assert report['collected'] == 0 and not manager.api.fetched
    with manager.store.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM collection_history')] == history
        assert db.execute('SELECT count(*) FROM law_versions').fetchone()[0] == 1


def test_report_write_failure_does_not_stop_collection(manager, monkeypatch):
    monkeypatch.setattr(task, 'write_report', lambda *a: (_ for _ in ()).throw(PermissionError('locked')))
    messages = []
    report = task.backfill(manager, notify=messages.append)
    assert report['ok'] and report['collected'] == 3
    assert sum('JSON 저장 실패' in m for m in messages) == 1
    assert manager.history(1)['total'] == 3


def test_interrupt_retains_completed_and_clears_only_own_pending(manager):
    original = manager.api.fetch
    def interrupted(mst, **kwargs):
        if mst == '200':
            raise KeyboardInterrupt()
        return original(mst, **kwargs)
    manager.api.fetch = interrupted
    report = task.backfill(manager, notify=lambda _: None)
    assert report['interrupted'] and not report['ok'] and report['collected'] == 1
    assert manager.history(1)['pending'] == 0
    assert {r['status'] for r in manager.history(1)['items']} == {'completed', 'interrupted'}
    manager.api.fetch = original
    manager.api.fetched.clear()
    report = task.backfill(manager, notify=lambda _: None)
    assert report['ok'] and set(manager.api.fetched) == {'200','400'}


def test_missing_mst_recovers_exact_version_without_refetching_original(tmp_path):
    manager = Management(Store(tmp_path), FakeProvider(), HierarchyAPI())
    version = seed(manager, missing_mst=True)
    report = task.backfill(manager, notify=lambda _: None)
    assert report['ok'] and '300' not in manager.api.fetched
    with manager.store.connect() as db:
        assert db.execute('SELECT mst FROM law_versions WHERE version=?', (version,)).fetchone()[0] == '300'


def test_ambiguous_legacy_mst_is_reported_not_guessed(tmp_path):
    api = HierarchyAPI()
    manager = Management(Store(tmp_path), FakeProvider(), api)
    seed(manager, missing_mst=True)
    api.rows.append({**ROWS[2], 'id': '301'})
    report = task.backfill(manager, notify=lambda _: None)
    assert not report['ok'] and report['items'][0]['status'] == 'failed'
    assert '유일하게' in report['items'][0]['errors'][0]['reason']
    assert not manager.api.fetched and manager.history(1)['total'] == 1
    assert '상하위법 조회/계획 실패' in manager.history(1)['items'][0]['reason']


def test_all_existing_versions_excluding_admin_rules_and_exact_name_filter(manager):
    manager.api.rows.append({**ROWS[2], 'id': '299', 'effective': '20250101', 'promulgated': '20241201'})
    seed(manager, '299')
    with manager.store.connect() as db:
        db.execute("INSERT INTO law_versions SELECT 'admin',law_id,'별도 지침','admrul',mst,promulgated,effective,collected_at,html,source_url FROM law_versions LIMIT 1")
    report = task.backfill(manager, law_name='합성안전법 시행규칙', list_only=True, notify=lambda _: None)
    assert report['sources'] == 2
    assert {e['effective'] for e in report['items']} == {'20250101', '20260101'}
    assert task.backfill(manager, law_name='없는법', notify=lambda _: None)['sources'] == 0


def test_cli_checks_existing_database_and_missing_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(task, 'Settings', lambda: Settings(data_dir=tmp_path, law_oc='', api_key=''))
    assert task.main([]) == 2
    assert 'DB가 없습니다' in capsys.readouterr().out
    Store(tmp_path)
    assert task.main([]) == 2
    assert 'LAW_API_OC' in capsys.readouterr().out


def test_cli_end_to_end_uses_env_data_directory(manager, monkeypatch):
    monkeypatch.setattr(task, 'Settings', lambda: Settings(data_dir=manager.store.directory, law_oc='test', api_key='test'))
    monkeypatch.setattr(task, 'LawAPI', lambda _: manager.api)
    monkeypatch.setattr(task, 'Provider', lambda _: FakeProvider())
    assert task.main([]) == 0
    assert set(manager.api.fetched) == {'100', '200', '400'}

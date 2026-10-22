"""Current-only collection sets, exact names, resumability and explicit gaps."""
import json

import pytest

from app.config import Settings
from app.hierarchy import current_revision
from app.management import Management
from app.preload_maritime import LAWS, NAME_CORRECTIONS, preload, main
from app.store import Store
from test_delegation import DelegationAPI
from test_hierarchy_collection import ROWS
from test_workflow import FakeProvider


@pytest.fixture
def manager(tmp_path):
    return Management(Store(tmp_path), FakeProvider(), DelegationAPI())


def test_manifest_has_all_31_and_only_spelling_correction():
    assert len(LAWS)==31 and len(set(LAWS))==31
    assert '해사안전법' in LAWS
    assert '수상레저기구의 등록 및 검사에 관한 법률' in LAWS
    assert len(NAME_CORRECTIONS)==1


def test_current_only_not_latest_historical_or_future(manager):
    original = manager.api.search
    def search(query, *a, **kw):
        assert kw['revisions'] is False
        result = original(query,*a,**kw)
        result['laws'] += [{**ROWS[0], 'id':'101','effective':'20290101','promulgated':'20281001'}]
        result['total'] = len(result['laws'])
        return result
    manager.api.search = search
    assert current_revision(manager, {'name':ROWS[0]['name']})['id']=='100'
    manager.api.search = lambda *a,**kw:{'laws':[], 'total':0}
    with pytest.raises(ValueError, match='현재 시행'):
        current_revision(manager, {'name':ROWS[0]['name']})


def test_abolished_and_similarly_named_laws_are_not_substituted(manager):
    manager.api.rows[0]['amendment'] = '폐지'
    manager.api.rows.append({**ROWS[0], 'id':'101','name':'새로운 합성안전법'})
    report = preload(manager, [ROWS[0]['name']], notify=lambda _:None)
    assert not report['ok'] and report['items'][0]['status']=='failed'
    assert not manager.api.fetched
    assert manager.history(1)['items'][0]['status']=='failed'


def test_one_current_set_with_sibling_rules_and_restart(manager):
    manager.api.rows.append({**ROWS[0], 'id':'99','effective':'20210101','promulgated':'20201201'})
    report = preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert report['ok'] and report['version_limit']==1
    assert set(manager.api.fetched)=={'100','200','300','400'}
    assert report['items'][0]['completed']==4
    assert manager.api.calls==['100']
    manager.api.fetched.clear()
    restarted = Management(Store(manager.store.directory), FakeProvider(), manager.api)
    again = preload(restarted,[ROWS[0]['name']],notify=lambda _:None)
    assert again['ok'] and again['items'][0]['completed']==0
    assert again['items'][0]['already_collected']==4 and not manager.api.fetched
    saved = json.loads((manager.store.directory/'preload-maritime-report.json').read_text())
    assert saved['ok']


def test_current_set_verifies_companions_despite_older_tree_nodes(manager):
    manager.api.rows = [r for r in manager.api.rows if r['id']!='200'] + [{**ROWS[1], 'id':'201','effective':'20260101','promulgated':'20251201'}]
    # The root's tree still carries decree MST 200, but the current search is authoritative here.
    report = preload(manager,[ROWS[0]['name']],list_only=True,notify=lambda _:None)
    assert report['ok']
    assert {r['mst'] for r in report['items'][0]['items']}=={'100','201','300','400'}
    assert not manager.api.fetched
    with manager.store.connect() as db:
        tree = json.loads(db.execute("SELECT payload FROM law_hierarchy_snapshots WHERE mst='201'").fetchone()[0])['tree']
        assert next(n for n in tree['nodes'] if n['level']=='시행령')['mst']=='201'


def test_list_only_uses_no_ai_no_body_or_delegation(manager):
    manager.provider = object()
    report = preload(manager,[ROWS[0]['name']],list_only=True,notify=lambda _:None)
    assert report['ok'] and not manager.api.calls and not manager.api.fetched
    assert manager.history(1)['total']==0


def test_partial_body_failure_retries_only_missing_document(manager):
    manager.api.fail.add('200')
    report = preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert not report['ok'] and report['items'][0]['status']=='incomplete'
    assert report['items'][0]['missing']==[ROWS[1]['name']]
    manager.api.fail.clear()
    manager.api.fetched.clear()
    report = preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert report['ok'] and manager.api.fetched==['200']


def test_relation_failure_keeps_completed_body_and_reports_incomplete(manager):
    manager.api.fail_delegation=True
    report=preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert not report['ok'] and report['items'][0]['completed']==4
    assert all(r['status']=='completed' for r in manager.history(1)['items'])
    assert all(r['status']=='failed' for r in report['items'][0]['delegation'])


def test_interrupt_then_resume_skips_completed(manager):
    original=manager.api.fetch
    def interrupted(mst,**kw):
        if mst=='200':
            raise KeyboardInterrupt
        return original(mst,**kw)
    manager.api.fetch=interrupted
    report=preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert report['interrupted'] and not report['ok']
    assert manager.history(1)['pending']==0
    manager.api.fetch=original
    manager.api.fetched.clear()
    report=preload(manager,[ROWS[0]['name']],notify=lambda _:None)
    assert report['ok'] and '100' not in manager.api.fetched


def test_cli_uses_selected_data_dir_and_single_law(manager,monkeypatch):
    monkeypatch.setattr('app.preload_maritime.Settings',lambda:Settings(data_dir=manager.store.directory,law_oc='test',api_key='test'))
    monkeypatch.setattr('app.preload_maritime.LawAPI',lambda _:manager.api)
    monkeypatch.setattr('app.preload_maritime.Provider',lambda _:FakeProvider())
    assert main(['--law',ROWS[0]['name']])==0
    assert main(['--law',ROWS[0]['name'],'--list-only'])==0


def test_missing_current_companion_never_falls_back_to_tree_historical_version(manager):
    original = manager.api.search
    def search(query,*args,**kw):
        if query==ROWS[1]['name'] and not kw.get('revisions'):
            return {'laws':[], 'total':0}
        return original(query,*args,**kw)
    manager.api.search=search
    report=preload(manager,[ROWS[0]['name']],list_only=True,notify=lambda _:None)
    assert not report['ok'] and report['items'][0]['warnings']
    assert '200' not in {r['mst'] for r in report['items'][0]['items']}

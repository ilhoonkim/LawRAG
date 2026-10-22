"""Current administrative-rule selection and restart behavior without external APIs."""
from datetime import date
import json
import xml.etree.ElementTree as ET

import pytest

from app.catalog import normalize_name
from app.config import Settings
from app.laws import LawAPI, SetupError
from app.management import Management
from app.preload_admrul import RULES, current_rule, main, preload, unique_names
from app.store import Store
from test_workflow import FakeProvider, RAW

TODAY = date(2026, 10, 22)


def row(mst='101', name='강선의 구조기준', **overrides):
    return {'id': mst, 'name': name, 'effective': '20260101', 'promulgated': '20251201',
            'status': '현행', 'amendment': '일부개정', **overrides}


class RuleAPI(LawAPI):
    """Exercise the real search XML adapter and admrul fetch parameters."""
    def __init__(self):
        super().__init__('test')
        self.rows = [row(), row('102', '목선의 구조기준')]
        self.requests, self.fetched, self.fail = [], [], set()
        self.body_name = None
        self.body_effective = None

    def request(self, endpoint, **params):
        self.requests.append((endpoint, params))
        assert params['target'] == 'admrul'  # No law hierarchy/delegation requests.
        if endpoint == 'lawSearch.do':
            assert params['nw'] == 1  # Current administrative rules, not historical nw=2.
            start = (params['page'] - 1) * params['display']
            root = ET.Element('AdmrulSearch')
            ET.SubElement(root, 'totalCnt').text = str(len(self.rows))
            for item in self.rows[start:start + params['display']]:
                node = ET.SubElement(root, 'admrul')
                for tag, key in [('행정규칙일련번호', 'id'), ('행정규칙명', 'name'), ('시행일자', 'effective'),
                                 ('발령일자', 'promulgated'), ('현행연혁구분', 'status'), ('제개정구분명', 'amendment')]:
                    ET.SubElement(node, tag).text = item[key]
        else:
            assert endpoint == 'lawService.do' and 'MST' not in params
            mst = params['ID']
            self.fetched.append(mst)
            if mst in self.fail:
                raise SetupError('법령 API 요청 실패 [HTTP 503]')
            item = next(r for r in self.rows if r['id'] == mst)
            root = ET.Element('행정규칙')
            for tag, value in [('행정규칙ID', '200000' + mst), ('행정규칙명', self.body_name or item['name']),
                               ('시행일자', self.body_effective or item['effective']), ('발령일자', item['promulgated'])]:
                ET.SubElement(root, tag).text = value
            article = ET.SubElement(root, '조문내용')
            article.text = '제1조(적용) 선박의 구조에 적용하는 합성 시험용 본문입니다.'
        return ET.tostring(root, encoding='utf-8')


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr('app.preload_admrul.today_in_korea', lambda: TODAY)
    monkeypatch.setattr('app.management.today_in_korea', lambda: TODAY)
    return Management(Store(tmp_path), FakeProvider(), RuleAPI())


def run(manager, names=('강선의 구조기준', '목선의 구조기준'), **kwargs):
    return preload(manager, names, notify=lambda _: None, **kwargs)


def test_manifest_and_whitespace_duplicate():
    assert len(RULES) == len({normalize_name(n) for n in RULES}) == 67
    assert RULES[0] == '강선의 구조기준' and RULES[-1] == '휘발성유기화합물규제항만 지정 등에 관한 기준'
    assert '어선 건조·개조허가 오차 허용범위' in RULES
    names = ['선령 20년 초과 내항 여객선의 선박검사기준', '선령 20년   초과 내항여객선의 선박검사기준']
    assert unique_names(names) == names[:1]


def test_latest_current_exact_title_and_dates(manager):
    manager.api.rows = [row('99', effective='20200101'), row(),
                        row('103', effective='20990101'), row('104', promulgated='20990101'),
                        row('105', name='강선의 구조기준 해설'), row('106', effective='20260230'),
                        row('107', effective='20261022', promulgated='20261022')]
    selected = current_rule(manager.api, '강선의   구조기준', TODAY)
    assert selected['id'] == '107' and selected['kind'] == 'admrul'
    assert not manager.api.fetched


@pytest.mark.parametrize('changes', [
    {'status': '폐지'}, {'amendment': '폐지'}, {'status': '연혁'}, {'status': '시행예정'},
    {'name': '다른 구조기준'}, {'effective': '20990101'}, {'effective': ''},
    {'promulgated': '20260230'}, {'id': 'invalid-id'},
])
def test_missing_current_never_collects_historical_similar_or_invalid(manager, changes):
    manager.api.rows = [row(**changes)]
    result = run(manager, ['강선의 구조기준'])
    assert not result['ok'] and result['items'][0]['status'] == 'failed'
    assert not manager.api.fetched
    assert manager.history(1)['items'][0]['status'] == 'failed'
    assert manager.categories() == []


def test_latest_version_on_second_page(manager):
    manager.api.rows = [row(str(i), name='다른 규칙') for i in range(100)] + [row('201')]
    assert current_rule(manager.api, '강선의 구조기준', TODAY)['id'] == '201'
    assert [p['page'] for _, p in manager.api.requests] == [1, 2]


def test_search_retries_spacing_variant_without_accepting_other_titles(manager):
    queries = []
    def search(query, page, **kwargs):
        queries.append(query)
        rows = [row(name='강선의구조기준')] if query == '강선의구조기준' else [row(name='강선의 구조기준 해설')]
        return {'laws': rows, 'total': len(rows)}
    manager.api.search = search
    assert current_rule(manager.api, '강선의 구조기준', TODAY)['name'] == '강선의구조기준'
    assert queries == ['강선의 구조기준', '강선의구조기준']


@pytest.mark.parametrize('second', ['repeat', 'missing'])
def test_bad_pagination_is_failure_even_if_first_page_has_a_match(manager, second):
    manager.api.search = lambda query, page, **kwargs: {
        'laws': [row()] if page == 1 or second == 'repeat' else [], 'total': 101}
    result = run(manager, ['강선의 구조기준'])
    assert not result['ok'] and '페이지' in result['items'][0]['errors'][0]
    assert not manager.api.fetched


def test_collects_one_version_and_resumes_preserving_laws(manager):
    existing = manager.store.ingest(RAW, manager.provider)
    manager.api.rows.append(row('99', effective='20200101'))
    result = run(manager, ['강선의 구조기준', '강선의구조기준', '목선의 구조기준'])
    assert result['ok'] and result['targets'] == 2 and result['version_limit'] == 1
    assert manager.api.fetched == ['101', '102']
    with manager.store.connect() as db:
        stored = list(db.execute("SELECT * FROM law_versions WHERE kind='admrul'"))
        assert len(stored) == 2
        assert db.execute('SELECT 1 FROM law_versions WHERE version=?', (existing['version'],)).fetchone()
        assert db.execute('SELECT count(*) FROM version_articles WHERE vector IS NOT NULL AND title_vector IS NOT NULL').fetchone()[0] == 5
    for item in stored:
        assert (manager.store.directory / 'html' / item['html']).is_file()
        assert (manager.store.directory / 'raw' / (item['version'] + '.xml')).is_file()
    assert {'강선의 구조기준', '목선의 구조기준'} <= {r['name'] for r in manager.categories()}
    manager.api.fetched.clear()
    restarted = Management(Store(manager.store.directory), FakeProvider(), manager.api)
    again = run(restarted)
    assert again['ok'] and {r['status'] for r in again['items']} == {'already_collected'}
    assert not manager.api.fetched and manager.history(1)['total'] == 2
    saved = json.loads((manager.store.directory / 'preload-admrul-report.json').read_text())
    assert saved['ok'] and saved['as_of'] == '2026-10-22'


@pytest.mark.parametrize('damage', ['vectors', 'model', 'articles', 'html', 'raw'])
def test_repairs_only_incomplete_current_rule(manager, damage):
    assert run(manager)['ok']
    with manager.store.connect() as db:
        version = db.execute("SELECT version FROM law_versions WHERE mst='101'").fetchone()[0]
        if damage == 'vectors':
            db.execute('UPDATE version_articles SET title_vector=NULL WHERE version=?', (version,))
        elif damage == 'model':
            db.execute("UPDATE version_articles SET model='old-model' WHERE version=?", (version,))
        elif damage == 'articles':
            db.execute('DELETE FROM version_articles WHERE version=?', (version,))
        else:
            suffix = '.html' if damage == 'html' else '.xml'
            (manager.store.directory / damage / (version + suffix)).unlink()
    manager.api.fetched.clear()
    result = run(manager)
    assert result['ok'] and manager.api.fetched == ['101']


def test_failure_continues_then_retries_only_failed_rule(manager):
    manager.api.fail.add('101')
    result = run(manager)
    assert not result['ok'] and [r['status'] for r in result['items']] == ['failed', 'completed']
    manager.api.fail.clear()
    manager.api.fetched.clear()
    assert run(manager)['ok'] and manager.api.fetched == ['101']


@pytest.mark.parametrize('field,value', [('body_name', '다른 규칙'), ('body_effective', '20200101')])
def test_mismatched_body_is_not_saved_as_selected_version(manager, field, value):
    setattr(manager.api, field, value)
    result = run(manager, ['강선의 구조기준'])
    assert not result['ok'] and '불일치' in result['items'][0]['errors'][0]
    assert manager.categories() == []
    assert list((manager.store.directory / 'raw').glob('*.xml'))


def test_list_only_never_embeds_fetches_or_queues(manager):
    manager.provider = object()
    result = run(manager, list_only=True)
    assert result['ok'] and {r['status'] for r in result['items']} == {'planned'}
    assert not manager.api.fetched and manager.history(1)['total'] == 0


def test_interrupt_then_resume(manager):
    original = manager.api.fetch
    def interrupted(mst, **kwargs):
        if mst == '102':
            raise KeyboardInterrupt
        return original(mst, **kwargs)
    manager.api.fetch = interrupted
    result = run(manager)
    assert result['interrupted'] and not result['ok']
    assert manager.history(1)['pending'] == 0
    manager.api.fetch = original
    manager.api.fetched.clear()
    assert run(manager)['ok'] and manager.api.fetched == ['102']


def test_report_write_failure_keeps_db_progress(manager, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('app.preload_admrul.write_report', fail)
    messages = []
    assert preload(manager, ['강선의 구조기준'], notify=messages.append)['ok']
    assert sum('JSON 저장 실패' in m for m in messages) == 1
    assert manager.history(1)['items'][0]['status'] == 'completed'


def test_cli_names_file_and_custom_directory(manager, monkeypatch, tmp_path):
    monkeypatch.setattr('app.preload_admrul.Settings', lambda: Settings(law_oc='test', api_key='test'))
    monkeypatch.setattr('app.preload_admrul.LawAPI', lambda _: manager.api)
    monkeypatch.setattr('app.preload_admrul.Provider', lambda _: FakeProvider())
    names = tmp_path / 'names.txt'
    names.write_text('강선의 구조기준\n강선의구조기준\n\n목선의 구조기준\n', encoding='utf-8-sig')
    assert main(['--names-file', str(names), '--data-dir', str(tmp_path)]) == 0
    assert manager.api.fetched == ['101', '102']
    assert main(['--rule', '강선의 구조기준', '--data-dir', str(tmp_path), '--list-only']) == 0
    assert main(['--rule', '없는 규칙', '--data-dir', str(tmp_path)]) == 1
    assert main(['--names-file', str(tmp_path / 'missing.txt')]) == 2
    names.write_text('\n \n', encoding='utf-8')
    assert main(['--names-file', str(names)]) == 2

import json
from dotenv import dotenv_values
from app.store import Store
from app.upgrade_models import reindex, configure, TARGET
from app.laws import SetupError
from test_workflow import RAW, FakeProvider


class LargeProvider(FakeProvider):
    fingerprint = 'test-api|' + TARGET
    def embed(self, texts):
        return [[1.,0.,.5,.1] for t in texts]


def test_upgrade_is_resumable_and_keeps_versions_and_legacy_index(tmp_path):
    store = Store(tmp_path)
    store.ingest(RAW,FakeProvider())
    store.ingest(RAW.replace(b'20260101',b'20200101'),FakeProvider(),archive_only=True)
    report_path = tmp_path / 'report.json'
    report = reindex(store,LargeProvider(),report_path,lambda _:None)
    assert report['completed'] == 2 and not report['failed']
    with store.connect() as db:
        assert {r[0] for r in db.execute('SELECT model FROM version_articles')} == {LargeProvider.fingerprint}
        assert {r[0] for r in db.execute('SELECT model FROM articles')} == {LargeProvider.fingerprint}
        assert {r[0] for r in db.execute('SELECT length(vector) FROM version_articles')} == {16}
    report = reindex(store,LargeProvider(),report_path,lambda _:None)
    assert report['skipped'] == 2 and report['completed'] == 0
    assert json.loads(report_path.read_text()) == report


def test_failed_upgrade_preserves_old_vectors(tmp_path):
    store = Store(tmp_path); store.ingest(RAW,FakeProvider())
    class Failure(LargeProvider):
        def embed(self, texts):
            raise SetupError('합성 사용 한도 오류')
    result = reindex(store,Failure(),tmp_path/'report.json',lambda _:None)
    assert result['failed'] == 1
    with store.connect() as db:
        assert {r[0] for r in db.execute('SELECT model FROM version_articles')} == {FakeProvider.fingerprint}


def test_config_preserves_keys_and_only_updates_models(tmp_path):
    env = tmp_path / '.env'
    env.write_text('LLM_API_KEY=test-secret\nLAW_API_OC=test-oc\nDATA_DIR=somewhere\nCHAT_MODEL=gpt-4.1-mini\n')
    configure(env)
    result = dotenv_values(env)
    assert result['LLM_API_KEY'] == 'test-secret' and result['LAW_API_OC'] == 'test-oc'
    assert result['DATA_DIR'] == 'somewhere' and result['EMBEDDING_MODEL'] == TARGET
    assert result['CHAT_MODEL'] == 'gpt-4.1'

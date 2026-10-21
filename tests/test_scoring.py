from datetime import date
import pytest
from app.store import Store, combined_score
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from test_workflow import FakeProvider, RAW


def test_title_and_body_are_both_required():
    assert combined_score(1.,0.) is None
    assert combined_score(0.,1.) is None
    assert combined_score(.8,.9)==pytest.approx(.35*.8+.65*.9)


def test_embeddings_are_separate_and_scores_exposed(tmp_path):
    class Spy(FakeProvider):
        calls=[]
        def embed(self,texts):
            self.calls.append(texts)
            return super().embed(texts)
    provider=Spy();store=Store(tmp_path);store.ingest(RAW,provider)
    inputs=provider.calls[0]
    assert len(inputs)==6
    assert inputs[4]=='선박안전법 - 제3조(적용범위)'
    assert not inputs[1].startswith('제3조(적용범위)')
    results=store.search('선박안전법에 적용되지 않는 것은?',provider,as_of=date(2026,10,21))
    r=results[0]
    assert r['number']=='제3조'
    assert r['score']==pytest.approx(.35*r['title_score']+.65*r['body_score'],abs=.0001)
    assert 'title_semantic_score' in r and 'body_keyword_score' in r
    assert 'title_vector' not in r and 'vector' not in r


def test_title_only_or_body_only_decoys_are_excluded(tmp_path):
    articles=[('1','항로표지 설치 기준','항로표지 설치 위치와 기준을 정한다.'),
              ('2','항로표지 설치 기준','기금 납부 징수 대금 처리 방법.'),
              ('3','기금 납부','항로표지 설치 위치와 기준을 정한다.')]
    raw='<법령><기본정보><법령ID>T</법령ID><법령명_한글>테스트법</법령명_한글><시행일자>20200101</시행일자></기본정보><조문>'
    for number,title,body in articles:
        raw+=f'<조문단위><조문번호>{number}</조문번호><조문제목>{title}</조문제목><조문내용>제{number}조({title}) {body}</조문내용></조문단위>'
    raw+='</조문></법령>'
    store=Store(tmp_path);store.ingest(raw.encode(),FakeProvider(),keyword_only=True)
    result=store.search('테스트법 항로표지 설치 기준',FakeProvider(),mode='keyword',as_of=date(2026,10,21))
    assert [r['number'] for r in result]==['제1조']


def test_reindex_existing_snapshot_enables_dual_embeddings(tmp_path):
    app=create_app(Settings(data_dir=tmp_path),FakeProvider())
    result=app.state.store.ingest(RAW,FakeProvider(),keyword_only=True)
    client=TestClient(app)
    assert client.post(f'/api/management/versions/{result["version"]}/reindex').status_code==200
    result=client.post('/api/search',json={'question':'선박안전법 적용범위','as_of':'2026-10-21'}).json()
    assert result['evidence'][0]['title_score']>0

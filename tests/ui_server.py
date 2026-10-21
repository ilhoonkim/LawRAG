"""Synthetic API server for UI checks. Never uses production DATA_DIR or credentials."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT / 'tests'))
from tempfile import mkdtemp
from pathlib import Path
import uvicorn
from app.main import create_app
from app.config import Settings
from test_management import FakeAPI, version
from test_workflow import FakeProvider
class UIAPI(FakeAPI):
    def search(self,query,page=1,kind='law',revisions=False,display=10):
        items=super().search(query,page,kind,revisions,display)['laws'][:3]
        items.extend({'id':str(i+200),'name':'선박안전법','kind':'law','effective':f'{2010+i}0101','promulgated':f'{2009+i}1201','status':'연혁'} for i in range(9))
        items.append({'id':'999','name':'선박안전법','kind':'law','effective':'20000101','promulgated':'19991201','status':'연혁'})
        return {'laws':items[(page-1)*display:page*display],'total':len(items),'page':page}
    def fetch(self,mst,kind='law',effective=''):
        if 200<=int(mst)<=208:
            n=int(mst)-200
            return version(f'{2010+n}0101',f'{2009+n}1201',mst)
        return super().fetch(mst,kind)
app=create_app(Settings(data_dir=Path(mkdtemp(prefix='lawrag-management-'))),FakeProvider(),UIAPI())
app.state.store.ingest(version('20260101','20251201','101'),FakeProvider(),mst='101',archive_only=True)
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]) if len(sys.argv)>1 else 8002)

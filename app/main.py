from pathlib import Path
from datetime import date
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import Settings
from .laws import LawAPI, SetupError, parse_law, parse_admrul, render_html
from .provider import Provider
from .store import Store
from .management import Management, management_router
from .temporal import resolve_date

class SearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    threshold: float | None = Field(default=None, ge=0, le=1)
    mode: Literal['hybrid', 'keyword'] = 'hybrid'
    limit: int = Field(default=5, ge=1, le=10)
    as_of: date | None = None

class Message(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(max_length=6000)

class ChatRequest(SearchRequest):
    history: list[Message] = Field(default_factory=list, max_length=6)

class IngestRequest(BaseModel):
    mst: str = Field(pattern=r'^\d{1,20}$')
    keyword_only: bool = False


def create_app(settings=None, provider=None, law_api=None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    provider = provider or Provider(settings)
    law_api = law_api or LawAPI(settings.law_oc)
    app = FastAPI(title='LawRAG', version='0.1.0')
    app.state.store = store
    manager = Management(store, provider, law_api)
    app.state.management = manager
    app.include_router(management_router(manager))

    @app.exception_handler(SetupError)
    async def setup_error(request, exc):
        return JSONResponse(status_code=503, content={'detail': str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse(status_code=400, content={'detail': str(exc)})

    @app.middleware('http')
    async def local_security(request: Request, call_next):
        # Local development app: prevent cross-origin browser mutation requests.
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse(status_code=403, content={'detail': '동일 출처 요청만 허용됩니다.'})
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        return response

    @app.get('/api/status')
    def status():
        return {'law_api_configured': bool(settings.law_oc), 'llm_configured': bool(settings.api_key),
                'threshold': settings.threshold, 'laws': store.list_laws()}

    @app.get('/api/laws/search')
    def find_laws(query: str = Query(min_length=2, max_length=100), page: int = Query(default=1, ge=1)):
        return law_api.search(query, page)

    @app.post('/api/laws/ingest')
    def ingest(body: IngestRequest):
        return store.ingest(law_api.fetch(body.mst), provider, body.keyword_only)

    @app.post('/api/search')
    def search(body: SearchRequest):
        cutoff, clarification = resolve_date(body.question, body.as_of)
        if clarification:
            return {'evidence': [], 'clarification': clarification, 'as_of': None, 'missing_versions': []}
        timeline = store.temporal_versions(cutoff)
        missing = timeline['missing']
        named = [r['name'] for r in timeline['versions'] + missing if r['name'] in body.question]
        if named:
            missing = [r for r in missing if r['name'] == max(named, key=len)]
        evidence = store.search(body.question, provider, settings.threshold if body.threshold is None else body.threshold,
                                body.limit, body.mode, as_of=cutoff, title_weight=settings.title_weight,
                                min_title=settings.min_title_score, min_body=settings.min_body_score)
        evidence, missing_references = store.expand_references(evidence)
        return {'evidence': evidence, 'missing_references': missing_references, 'as_of': cutoff.isoformat(), 'clarification': None,
                'missing_versions': missing, 'coverage_note': timeline['note']}

    @app.post('/api/chat')
    def chat(body: ChatRequest):
        result = search(body)
        evidence = result['evidence']
        if result['clarification']:
            return {**result, 'answer': result['clarification'], 'generated': False}
        if result.get('missing_references'):
            names = ', '.join(dict.fromkeys(f'{r["law_name"]} {r["number"]}' for r in result['missing_references']))
            return {**result, 'answer': f'관련 조문은 찾았지만 참조 자료({names})의 본문이 확보되지 않았습니다. 해당 참조 자료를 확보한 뒤 답변해야 합니다.', 'generated': False}
        if not evidence:
            if result['missing_versions']:
                answer = '기준일에 적용될 버전이 미수집 상태입니다. 법령관리에서 해당 개정 연혁을 수집한 뒤 다시 질문해 주세요.'
            else:
                answer = '설정한 임계치 이상의 근거 조문을 찾지 못했습니다. 기준일에 시행 중인 버전의 수집 여부를 확인하거나 법령명과 질문을 구체적으로 입력해 주세요.'
            return {**result, 'answer': answer, 'generated': False}
        question = f'기준일: {result["as_of"]} (이 날짜에 시행 중인 수집 버전 기준)\n{body.question}'
        answer = provider.answer(question, evidence, [m.model_dump() for m in body.history])
        return {**result, 'answer': answer, 'generated': True}

    @app.get('/documents/{filename}')
    def document(filename: str):
        import re
        if not re.fullmatch(r'[0-9a-f]{20}\.html', filename):
            raise HTTPException(404, '문서가 없습니다.')
        path = settings.data_dir / 'html' / filename
        if not path.is_file():
            raise HTTPException(404, '문서가 없습니다.')
        # Upgrade the viewer from this exact archived XML; no API/embedding call
        # and no substitution with the currently effective version.
        raw = settings.data_dir / 'raw' / (path.stem + '.xml')
        if raw.is_file() and 'data-viewer-version="2"' not in path.read_text(encoding='utf-8')[:200]:
            with store.connect() as db:
                row = db.execute('SELECT kind FROM law_versions WHERE version=?', (path.stem,)).fetchone()
            law = parse_admrul(raw.read_bytes()) if row and row['kind']=='admrul' else parse_law(raw.read_bytes())
            if law.version != path.stem:
                raise HTTPException(409, '보관 XML과 요청 버전이 일치하지 않습니다.')
            from uuid import uuid4
            temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
            try:
                temporary.write_text(render_html(law),encoding='utf-8')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return FileResponse(path, media_type='text/html')

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')
    return app

# Uvicorn resolves app.main:app lazily. Importing create_app in tests must not
# initialize or mark jobs interrupted in the user's real database.
def __getattr__(name):
    if name == 'app':
        instance = create_app()
        globals()['app'] = instance
        return instance
    raise AttributeError(name)

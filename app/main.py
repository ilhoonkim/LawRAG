from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import Settings
from .laws import LawAPI, SetupError
from .provider import Provider
from .store import Store

class SearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    threshold: float | None = Field(default=None, ge=0, le=1)
    mode: Literal['hybrid', 'keyword'] = 'hybrid'
    limit: int = Field(default=5, ge=1, le=10)

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
        return {'evidence': store.search(body.question, provider, settings.threshold if body.threshold is None else body.threshold, body.limit, body.mode)}

    @app.post('/api/chat')
    def chat(body: ChatRequest):
        evidence = search(body)['evidence']
        if not evidence:
            return {'answer': '설정한 임계치 이상의 근거 조문을 찾지 못했습니다. 법령 수집 여부를 확인하거나 법령명과 질문을 구체적으로 입력해 주세요.', 'evidence': [], 'generated': False}
        answer = provider.answer(body.question, evidence, [m.model_dump() for m in body.history])
        return {'answer': answer, 'evidence': evidence, 'generated': True}

    @app.get('/documents/{filename}')
    def document(filename: str):
        import re
        if not re.fullmatch(r'[0-9a-f]{20}\.html', filename):
            raise HTTPException(404, '문서가 없습니다.')
        path = settings.data_dir / 'html' / filename
        if not path.is_file():
            raise HTTPException(404, '문서가 없습니다.')
        return FileResponse(path, media_type='text/html')

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')
    return app

app = create_app()

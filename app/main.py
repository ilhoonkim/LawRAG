from pathlib import Path
from datetime import date
import json
from queue import Queue, Empty
from threading import Thread, Event
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import Settings
from .laws import LawAPI, SetupError, parse_law, parse_admrul, render_html
from .provider import Provider
from .store import Store
from .management import Management, management_router
from .chat_pipeline import retrieve, run_chat, STAGES

class SearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    threshold: float | None = Field(default=None, ge=0, le=1)
    mode: Literal['hybrid', 'keyword'] = 'hybrid'
    limit: int = Field(default=5, ge=1, le=10)
    as_of: date | None = None
    fusion: Literal['weighted', 'rrf'] | None = None

class Message(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(max_length=6000)

class ChatRequest(SearchRequest):
    model: str | None = Field(default=None, min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]+$")
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
                'threshold': settings.threshold, 'chat_model': settings.chat_model, 'search_fusion': settings.search_fusion, 'laws': store.list_laws()}

    @app.get('/api/laws/search')
    def find_laws(query: str = Query(min_length=2, max_length=100), page: int = Query(default=1, ge=1)):
        return law_api.search(query, page)

    @app.post('/api/laws/ingest')
    def ingest(body: IngestRequest):
        raw = law_api.fetch(body.mst)
        if not manager.collectible_date(parse_law(raw).effective):
            raise ValueError('오늘 이후 시행 예정이거나 시행일이 잘못된 본문은 저장하지 않았습니다.')
        return store.ingest(raw, provider, body.keyword_only, mst=body.mst)

    @app.get('/api/models')
    def models():
        presets = ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-4.1',
                   'gpt-5-mini', 'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5.4']
        available, warning = [], ''
        try:
            available = provider.models() if hasattr(provider, 'models') else []
        except SetupError as exc:
            warning = str(exc)
        return {'models': list(dict.fromkeys([settings.chat_model] + presets + available)),
                'available': available, 'default': settings.chat_model, 'warning': warning}

    @app.post('/api/search')
    def search(body: SearchRequest):
        return retrieve(store, provider, settings, body)

    @app.post('/api/search/compare')
    def compare_search(body: SearchRequest):
        from .temporal import resolve_date
        cutoff, clarification = resolve_date(body.question, body.as_of)
        if clarification:
            return {'clarification':clarification,'weighted':[],'rrf':[]}
        rewritten = provider.rewrite_question(body.question)
        return store.search_enhanced(body.question,provider,variants=[rewritten],cutoff=cutoff,
            threshold=settings.threshold if body.threshold is None else body.threshold,
            limit=body.limit,mode=body.mode,compare=True)

    def chat_provider(body):
        return provider.for_model(body.model or settings.chat_model) if hasattr(provider, 'for_model') else provider

    @app.post('/api/chat')
    def chat(body: ChatRequest):
        return run_chat(store, chat_provider(body), settings, body)

    @app.post('/api/chat/stream')
    def chat_stream(body: ChatRequest):
        selected = chat_provider(body)
        events, cancelled = Queue(), Event()
        def progress(stage):
            if cancelled.is_set():
                raise InterruptedError()
            events.put({'type': 'stage', 'stage': stage, 'label': dict(STAGES)[stage]})
        def work():
            try:
                result = run_chat(store, selected, settings, body, progress)
                if not cancelled.is_set():
                    events.put({'type': 'result', 'result': result})
            except InterruptedError:
                pass
            except (SetupError, ValueError) as exc:
                events.put({'type': 'error', 'detail': str(exc)})
            except Exception:
                events.put({'type': 'error', 'detail': '답변 처리 중 오류가 발생했습니다. 다시 시도해 주세요.'})
            finally:
                events.put(None)
        def stream():
            Thread(target=work, daemon=True).start()
            try:
                while True:
                    try:
                        event = events.get(timeout=1)
                    except Empty:
                        yield '\n'  # heartbeat lets disconnects cancel between stages
                        continue
                    if event is None:
                        break
                    yield json.dumps(event, ensure_ascii=False) + '\n'
            finally:
                cancelled.set()
        return StreamingResponse(stream(), media_type='application/x-ndjson',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

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

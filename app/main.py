from pathlib import Path
from datetime import date
import json
from queue import Queue, Empty
from threading import Thread, Event
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import Settings, SEARCH_FUSION, SEARCH_THRESHOLD
from .laws import LawAPI, SetupError, parse_law, parse_admrul, render_html, VIEWER_VERSION
from .provider import Provider
from .store import Store
from .management import Management, management_router
from .chat_pipeline import retrieve, run_chat, STAGES

# HTTP 진입점과 요청 검증을 담당합니다. 검색·답변 로직은 chat_pipeline.py,
# 수집 작업과 이력은 management.py, DB 접근은 store.py로 이어집니다.
class SearchRequest(BaseModel):
    # 이전 화면이 보낸 threshold/fusion은 미사용 필드로 무시합니다.
    # 검색 정책은 서버에서 고정하며 OpenAPI에도 변경 옵션을 노출하지 않습니다.
    question: str = Field(min_length=2, max_length=2000)
    mode: Literal['hybrid', 'keyword'] = 'hybrid'
    limit: int = Field(default=5, ge=1, le=10)
    as_of: date | None = None

class Message(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(max_length=6000)

# 대화 이력은 브라우저가 요청에 담아 보냅니다. 이 API는 서버에 대화를 저장하지 않습니다.
# 길이와 개수를 제한해 과거 대화가 프롬프트를 무제한으로 차지하지 않도록 합니다.
class ChatRequest(SearchRequest):
    model: str | None = Field(default=None, min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]+$")
    history: list[Message] = Field(default_factory=list, max_length=6)

class IngestRequest(BaseModel):
    mst: str = Field(pattern=r'^\d{1,20}$')
    keyword_only: bool = False


# 테스트에서는 설정·외부 API 대역을 주입할 수 있습니다.
# 앱 생성 시 DB 초기화와 이전 수집 작업의 중단 상태 정리도 함께 수행됩니다.
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
        # 브라우저의 다른 사이트에서 수집 요청을 보내는 것을 막는 검사입니다.
        # 로그인이나 사용자별 접근 권한을 제공하는 인증 기능은 아닙니다.
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
                'threshold': SEARCH_THRESHOLD, 'chat_model': settings.chat_model,
                'search_fusion': SEARCH_FUSION, 'laws': store.list_laws()}

    @app.get('/api/laws/search')
    def find_laws(query: str = Query(min_length=2, max_length=100), page: int = Query(default=1, ge=1)):
        return law_api.search(query, page)

    @app.post('/api/laws/ingest')
    def ingest(body: IngestRequest):
        raw = law_api.fetch(body.mst)
        if not manager.collectible_date(parse_law(raw).effective):
            raise ValueError('오늘 이후 시행 예정이거나 시행일이 잘못된 본문은 저장하지 않았습니다.')
        result = store.ingest(raw, provider, body.keyword_only, mst=body.mst)
        from .delegation import refresh
        return {**result, 'delegation': refresh(store, law_api, result['version'])}

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

    # 검색 API는 후보 조회용입니다. LLM 관련성 검토와 답변 생성은 채팅 API에서 수행합니다.
    @app.post('/api/search')
    def search(body: SearchRequest):
        return retrieve(store, provider, settings, body)

    # 요청마다 모델을 분리해야 동시에 접속한 사용자의 선택이 서로 영향을 주지 않습니다.
    def chat_provider(body):
        return provider.for_model(body.model or settings.chat_model) if hasattr(provider, 'for_model') else provider

    @app.post('/api/chat')
    def chat(body: ChatRequest):
        return run_chat(store, chat_provider(body), settings, body)

    # 스트림은 토큰 단위 답변이 아니라 실제 처리 단계와 최종 결과를 NDJSON으로 전달합니다.
    # 동기 작업은 별도 스레드에서 실행하고, Queue를 통해 HTTP 응답 쪽으로 이벤트를 넘깁니다.
    @app.post('/api/chat/stream')
    def chat_stream(body: ChatRequest):
        selected = chat_provider(body)
        events, cancelled = Queue(), Event()
        # 연결이 끊기면 다음 단계 진입 시 중단합니다. 이미 진행 중인 외부 API 호출은
        # 즉시 취소되지 않으므로 Provider의 타임아웃도 별도로 적용됩니다.
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

    # 출처 HTML은 원문 해시로 지정된 수집 버전을 보여줍니다.
    # 파일명 형식을 제한해 임의 경로 접근을 막고, 뷰어 갱신도 같은 XML로만 수행합니다.
    @app.get('/documents/{filename}')
    def document(filename: str, as_of: date | None = None):
        import re
        if not re.fullmatch(r'[0-9a-f]{20}\.html', filename):
            raise HTTPException(404, '문서가 없습니다.')
        path = settings.data_dir / 'html' / filename
        if not path.is_file():
            raise HTTPException(404, '문서가 없습니다.')
        # Upgrade the viewer from this exact archived XML; no API/embedding call
        # and no substitution with the currently effective version.
        raw = settings.data_dir / 'raw' / (path.stem + '.xml')
        if raw.is_file() and f'data-viewer-version="{VIEWER_VERSION}"' not in path.read_text(encoding='utf-8')[:200]:
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
        if raw.is_file():
            from .document_links import reference_resolver
            from .temporal import today_in_korea
            with store.connect() as db:
                row = db.execute('SELECT kind FROM law_versions WHERE version=?', (path.stem,)).fetchone()
            if row and row['kind'] == 'law':
                law = parse_law(raw.read_bytes())
                if law.version != path.stem:
                    raise HTTPException(409, '보관 XML과 요청 버전이 일치하지 않습니다.')
                # 채팅 출처는 질문 기준일을 전달합니다. 관리 화면에서 과거 문서를 직접
                # 열면 그 시행일, 현재 버전이면 오늘을 사용하며 원문 버전은 바꾸지 않습니다.
                cutoff = as_of or (today_in_korea() if law.version in {
                    v['version'] for v in store.temporal_versions()['versions']} else store._date(law.effective))
                if cutoff and cutoff < store._date(law.effective):
                    raise HTTPException(400, '문서 시행일 이전으로 참조 기준일을 지정할 수 없습니다.')
                if cutoff:
                    from .delegation import html_links
                    page = render_html(law, reference_resolver=reference_resolver(store, law, cutoff), reference_date=cutoff,
                                       delegation_links=html_links(store, law.version, cutoff))
                    return HTMLResponse(page, headers={'Cache-Control': 'no-store'})
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

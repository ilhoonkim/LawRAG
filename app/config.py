import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

# 설정 진입점: 프로세스 환경변수 > 프로젝트 local.env > .env 순으로 적용합니다.
# 아래 출처 기록은 진단용입니다. 키 값 자체를 로그나 API 응답에 포함하지 않습니다.
ROOT = Path(__file__).resolve().parent.parent
_KEYS_FROM_PROCESS = {name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)}
_MODELS_FROM_PROCESS = {name:os.environ[name] for name in ('CHAT_MODEL','RERANK_MODEL','EMBEDDING_MODEL') if os.getenv(name)}
# 사내 연결은 local.env에서 덮어쓸 수 있습니다. 실제 프로세스 환경변수는
# 두 파일보다 우선하고, local.env가 없으면 기존 .env만으로 그대로 실행됩니다.
_LOCAL_VALUES = dotenv_values(ROOT / 'local.env')
load_dotenv(ROOT / 'local.env')
load_dotenv(ROOT / '.env')

# 웹 검색·채팅의 고정 정책입니다. 환경변수나 사용자 요청으로 변경하지 않습니다.
SEARCH_FUSION = 'rrf'
SEARCH_THRESHOLD = 0.3


def ai_key_source():
    """Describe the winning key source, never its value."""
    name = next((name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)), None)
    if name is None:
        return '설정되지 않음'
    source = ('프로세스 환경변수 (.env보다 우선)' if name in _KEYS_FROM_PROCESS
              else '프로젝트 local.env' if name in _LOCAL_VALUES else '프로젝트 .env')
    return f'{name} / {source}'

# 기본값은 모듈을 불러올 때 평가됩니다. .env/local.env 변경 후 서버를 재시작합니다.
# 요청별 채팅 모델 변경은 Provider.for_model()에서 복사본을 만들어 적용합니다.
@dataclass
class Settings:
    data_dir: Path = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    law_oc: str = os.getenv('LAW_API_OC', '')
    api_key: str = os.getenv('LLM_API_KEY', '') or os.getenv('OPENAI_API_KEY', '')
    base_url: str = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1')
    embedding_model: str = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-large')
    chat_model: str = os.getenv('CHAT_MODEL', 'gpt-4.1')
    rerank_model: str = os.getenv('RERANK_MODEL', '')
    # 연결 선택은 서버 설정입니다. 클라이언트가 임의 API 주소/키를 전달하지 않습니다.
    chat_backend: str = os.getenv('CHAT_BACKEND', 'openai')
    # vllm: COMPANY_EMBEDDING_*로 HTTP 직접 호출. local_embedding: 기존 NSE MQ.
    embedding_backend: str = os.getenv('EMBEDDING_BACKEND', 'openai')
    company_chat_base_url: str = os.getenv('COMPANY_CHAT_BASE_URL', '')
    company_chat_model: str = os.getenv('COMPANY_CHAT_MODEL', '')
    company_chat_api_key: str = os.getenv('COMPANY_CHAT_API_KEY', '')
    company_embedding_base_url: str = os.getenv('COMPANY_EMBEDDING_BASE_URL', '')
    company_embedding_model: str = os.getenv('COMPANY_EMBEDDING_MODEL', '')
    company_embedding_api_key: str = os.getenv('COMPANY_EMBEDDING_API_KEY', '')
    company_chat_json_mode: str = os.getenv('COMPANY_CHAT_JSON_MODE', 'json_object')
    # NSE가 받는 출력 토큰 상한. 질문 재작성/근거 검토/최종 답변에 공통 적용.
    nse_max_tokens: int = int(os.getenv('NSE_MAX_TOKENS', '4096'))
    nse_embedding_model: str = os.getenv('NSE_EMBEDDING_MODEL', 'emb-bge-m3-ko')
    # 같은 served_model_name 뒤의 가중치를 교체했을 때 벡터 식별자를 갱신합니다.
    nse_embedding_revision: str = os.getenv('NSE_EMBEDDING_REVISION', '')
    ai_timeout_seconds: float = float(os.getenv('AI_TIMEOUT_SECONDS', '120'))
    embedding_max_input_bytes: int = int(os.getenv('EMBEDDING_MAX_INPUT_BYTES', '8000'))
    embedding_batch_size: int = int(os.getenv('EMBEDDING_BATCH_SIZE', '16'))
    evidence_review_max_chars: int = int(os.getenv('EVIDENCE_REVIEW_MAX_CHARS', '60000'))
    # 채팅의 실제 검토 상한은 chat_pipeline.review_limit()에서 최대 15개로 제한합니다.
    candidate_limit: int = int(os.getenv('SEARCH_CANDIDATE_LIMIT', '15'))
    reference_limit: int = int(os.getenv('REFERENCE_LIMIT', '40'))
    context_max_chars: int = int(os.getenv('CONTEXT_MAX_CHARS', '60000'))
    # 기존 검색용 설정 항목입니다. 현재 호출부는 이 값을 Store.search()에 전달하지 않습니다.
    # 현재 채팅의 필드별 점수·통과 조건은 retrieval.py를 참고하세요.
    title_weight: float = float(os.getenv('SEARCH_TITLE_WEIGHT', '0.35'))
    min_title_score: float = float(os.getenv('SEARCH_MIN_TITLE_SCORE', '0.15'))
    min_body_score: float = float(os.getenv('SEARCH_MIN_BODY_SCORE', '0.20'))

    def endpoint(self, purpose):
        from .ai_endpoints import endpoint
        return endpoint(self, purpose)

    @property
    def active_chat_model(self):
        return self.endpoint('chat').model

    def ai_configured(self, purpose):
        from .laws import SetupError
        try:
            self.endpoint(purpose).validate()
            return True
        except SetupError:
            return False

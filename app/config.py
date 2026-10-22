import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# 설정 진입점: 프로젝트 루트의 .env를 읽되, 이미 설정된 프로세스 환경변수가 우선합니다.
# 아래 출처 기록은 진단용입니다. 키 값 자체를 로그나 API 응답에 포함하지 않습니다.
ROOT = Path(__file__).resolve().parent.parent
_KEYS_FROM_PROCESS = {name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)}
_MODELS_FROM_PROCESS = {name:os.environ[name] for name in ('CHAT_MODEL','RERANK_MODEL','EMBEDDING_MODEL') if os.getenv(name)}
load_dotenv(ROOT / '.env')

# 웹 검색·채팅의 고정 정책입니다. 환경변수나 사용자 요청으로 변경하지 않습니다.
SEARCH_FUSION = 'rrf'
SEARCH_THRESHOLD = 0.3


def ai_key_source():
    """Describe the winning key source, never its value."""
    name = next((name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)), None)
    if name is None:
        return '설정되지 않음'
    return f'{name} / ' + ('프로세스 환경변수 (.env보다 우선)' if name in _KEYS_FROM_PROCESS else '프로젝트 .env')

# 기본값은 이 모듈을 불러올 때 평가됩니다. .env 변경 후에는 서버를 재시작해야 합니다.
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
    # 채팅의 실제 검토 상한은 chat_pipeline.review_limit()에서 최대 15개로 제한합니다.
    candidate_limit: int = int(os.getenv('SEARCH_CANDIDATE_LIMIT', '15'))
    reference_limit: int = int(os.getenv('REFERENCE_LIMIT', '40'))
    context_max_chars: int = int(os.getenv('CONTEXT_MAX_CHARS', '60000'))
    # 기존 검색용 설정 항목입니다. 현재 호출부는 이 값을 Store.search()에 전달하지 않습니다.
    # 현재 채팅의 필드별 점수·통과 조건은 retrieval.py를 참고하세요.
    title_weight: float = float(os.getenv('SEARCH_TITLE_WEIGHT', '0.35'))
    min_title_score: float = float(os.getenv('SEARCH_MIN_TITLE_SCORE', '0.15'))
    min_body_score: float = float(os.getenv('SEARCH_MIN_BODY_SCORE', '0.20'))

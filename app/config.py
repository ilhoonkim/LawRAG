import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
_KEYS_FROM_PROCESS = {name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)}
_MODELS_FROM_PROCESS = {name:os.environ[name] for name in ('CHAT_MODEL','RERANK_MODEL','EMBEDDING_MODEL') if os.getenv(name)}
load_dotenv(ROOT / '.env')


def ai_key_source():
    """Describe the winning key source, never its value."""
    name = next((name for name in ('LLM_API_KEY','OPENAI_API_KEY') if os.getenv(name)), None)
    if name is None:
        return '설정되지 않음'
    return f'{name} / ' + ('프로세스 환경변수 (.env보다 우선)' if name in _KEYS_FROM_PROCESS else '프로젝트 .env')

@dataclass
class Settings:
    data_dir: Path = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    law_oc: str = os.getenv('LAW_API_OC', '')
    api_key: str = os.getenv('LLM_API_KEY', '') or os.getenv('OPENAI_API_KEY', '')
    base_url: str = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1')
    embedding_model: str = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-large')
    metadata_model: str = os.getenv('METADATA_MODEL', 'gpt-4o-mini')
    chat_model: str = os.getenv('CHAT_MODEL', 'gpt-4.1')
    rerank_model: str = os.getenv('RERANK_MODEL', '')
    candidate_limit: int = int(os.getenv('SEARCH_CANDIDATE_LIMIT', '15'))
    search_fusion: str = os.getenv('SEARCH_FUSION', 'weighted')
    reference_limit: int = int(os.getenv('REFERENCE_LIMIT', '40'))
    context_max_chars: int = int(os.getenv('CONTEXT_MAX_CHARS', '60000'))
    threshold: float = float(os.getenv('SEARCH_THRESHOLD', '0.48'))
    title_weight: float = float(os.getenv('SEARCH_TITLE_WEIGHT', '0.35'))
    min_title_score: float = float(os.getenv('SEARCH_MIN_TITLE_SCORE', '0.15'))
    min_body_score: float = float(os.getenv('SEARCH_MIN_BODY_SCORE', '0.20'))

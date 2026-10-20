import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

@dataclass
class Settings:
    data_dir: Path = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    law_oc: str = os.getenv('LAW_API_OC', '')
    api_key: str = os.getenv('LLM_API_KEY', '') or os.getenv('OPENAI_API_KEY', '')
    base_url: str = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1')
    embedding_model: str = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
    chat_model: str = os.getenv('CHAT_MODEL', 'gpt-4.1-mini')
    threshold: float = float(os.getenv('SEARCH_THRESHOLD', '0.48'))

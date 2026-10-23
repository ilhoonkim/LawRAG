"""OpenAI와 사내 서버의 연결 설정. API별 요청 변환은 Provider에서 담당합니다."""
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .laws import SetupError

# vLLM 등에서 사용하는 조직/모델 ID도 허용합니다. URL로 사용하지는 않습니다.
MODEL_PATTERN = r'^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}$'


@dataclass(frozen=True)
class AIEndpoint:
    backend: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    purpose: str

    def validate(self):
        prefix = f'COMPANY_{self.purpose.upper()}'
        if self.backend == 'local_model' and self.purpose == 'chat':
            from .nse_mq import NseMqSettings
            NseMqSettings.from_env().validate(generation=True)
            return
        if self.backend == 'local_embedding' and self.purpose == 'embedding':
            from .nse_mq import NseMqSettings
            NseMqSettings.from_env().validate()
            if not re.fullmatch(MODEL_PATTERN, self.model or ''):
                raise SetupError('NSE_EMBEDDING_MODEL의 served_model_name을 확인하세요.')
            return
        if self.backend not in ('openai', 'company'):
            raise SetupError(f'{self.purpose.upper()}_BACKEND 설정을 확인하세요. 채팅은 openai, company, local_model을 지원하고 임베딩은 openai, company, local_embedding을 지원합니다.')
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in ('http', 'https') and url.hostname and url.port != 0
                     and not url.username and not url.password and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            name = prefix + '_BASE_URL' if self.backend == 'company' else 'LLM_BASE_URL'
            raise SetupError(f'{name}에 http(s)://서버주소:포트/v1 형식의 API 주소를 설정하세요.')
        if not re.fullmatch(MODEL_PATTERN, self.model or ''):
            name = prefix + '_MODEL' if self.backend == 'company' else self.purpose.upper() + '_MODEL'
            raise SetupError(f'{name}의 모델 ID를 확인하세요.')
        if self.backend == 'openai' and not self.api_key:
            raise SetupError('.env의 LLM_API_KEY 또는 OPENAI_API_KEY를 입력하세요.')

    @property
    def headers(self):
        # 사내 키가 비어 있으면 인증 헤더도 생략합니다. OpenAI 키를 대신 보내지 않습니다.
        return {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}

    def url(self, path):
        return self.base_url.rstrip('/') + path


def endpoint(settings, purpose):
    if purpose not in ('chat', 'embedding'):
        raise ValueError('지원하지 않는 AI 요청 종류입니다.')
    backend = getattr(settings, purpose + '_backend')
    if backend.lower() in ('local_model', 'loca_model') and purpose == 'chat':
        return AIEndpoint('local_model', '', 'LOCAL_MODEL', '', purpose)
    if purpose == 'embedding' and (backend.lower() in ('local_embedding', 'loca_embedding') or (
            backend == 'openai' and settings.embedding_model.upper() in ('LOCAL_EMBEDDING', 'LOCA_EMBEDDING'))):
        return AIEndpoint('local_embedding', '', settings.nse_embedding_model, '', purpose)
    if backend == 'company':
        return AIEndpoint(backend, getattr(settings, f'company_{purpose}_base_url'),
                          getattr(settings, f'company_{purpose}_model'),
                          getattr(settings, f'company_{purpose}_api_key'), purpose)
    # 기존 .env/Settings 생성 방식과 벡터 식별자를 그대로 지원합니다.
    return AIEndpoint(backend, settings.base_url, getattr(settings, purpose + '_model'),
                      settings.api_key, purpose)

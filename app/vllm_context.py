"""vLLM의 현재 배포 한도와 채팅 템플릿을 적용한 실제 토큰 수로 출력 예산 계산."""
from threading import Lock

from .http_retry import request_with_retry
from .laws import SetupError


class ContextBudgetExceeded(SetupError):
    """Raised before generation; callers may remove whole evidence items safely."""
    def __init__(self, count, limit, reserve):
        self.input_tokens, self.context_tokens, self.output_reserve = count, limit, reserve
        super().__init__(f'입력 {count}토큰과 출력 여유 {reserve}토큰이 서버 문맥 한도 {limit}토큰을 넘습니다. '
                         '조문을 임의로 자르지 않았습니다. 질문 또는 대화 입력을 줄여 주세요.')

    def details(self):
        return {'input_tokens':self.input_tokens, 'context_tokens':self.context_tokens,
                'output_reserve':self.output_reserve}


class VllmContext:
    def __init__(self):
        self._limits = {}
        self._lock = Lock()
        self.last_budget = {}

    @staticmethod
    def _json(client, method, url, headers, body=None):
        response = request_with_retry(lambda: client.request(method, url, headers=headers,
            **({'json': body} if body is not None else {})), service='vLLM 토큰 한도 확인')
        if not response.is_success:
            raise SetupError(f'vLLM 토큰 한도/입력 계산 실패 [HTTP {response.status_code}]. '
                             '/v1/models 및 /tokenize API 지원·접근 권한을 확인하세요.')
        try:
            return response.json()
        except ValueError:
            raise SetupError('vLLM 토큰 계산 API가 JSON 응답을 반환하지 않았습니다.') from None

    def prepare(self, client, endpoint, body):
        model = body['model']
        # Provider는 채팅 요청별 인스턴스입니다. 한 대화의 병렬 검토에서는 조회를 공유하고,
        # 다음 질문에서는 서버가 변경한 max_model_len을 다시 읽습니다.
        with self._lock:
            if model not in self._limits:
                models = self._json(client, 'GET', endpoint.url('/models'), endpoint.headers)
                try:
                    matches = [m for m in models['data'] if m['id'] == model]
                    limit = matches[0]['max_model_len'] if len(matches) == 1 else None
                    if type(limit) is not int or limit <= 0:
                        raise ValueError()
                except (KeyError, IndexError, TypeError, ValueError):
                    raise SetupError('선택한 모델의 max_model_len을 확인할 수 없습니다. 서버 /v1/models 설정을 확인하세요.') from None
                self._limits[model] = limit
            limit = self._limits[model]
        base = endpoint.base_url.rstrip('/')
        if not base.endswith('/v1'):
            raise SetupError('자동 토큰 계산은 COMPANY_CHAT_BASE_URL의 /v1 경로가 필요합니다.')
        tokenizer_input = {'model': model, 'messages': body['messages'], 'add_generation_prompt': True}
        if 'chat_template_kwargs' in body:
            tokenizer_input['chat_template_kwargs'] = body['chat_template_kwargs']
        result = self._json(client, 'POST', base[:-3] + '/tokenize', endpoint.headers, tokenizer_input)
        try:
            count = result['count']
            if type(count) is not int or count < 0:
                raise ValueError()
            token_limit = result.get('max_model_len', limit)
            if type(token_limit) is not int or token_limit <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SetupError('vLLM /tokenize 응답의 count 또는 max_model_len이 올바르지 않습니다.') from None
        # 두 엔드포인트가 다른 한도를 보고하면 작은 값을 지켜 실제 서버 한도를 넘지 않습니다.
        limit = min(limit, token_limit)
        remaining = limit - count
        self.last_budget = {'context_tokens': limit, 'input_tokens': count, 'max_output_tokens': max(0, remaining)}
        # Reserve room for structured judgments / the answer before sending.
        # max_tokens still uses all remaining capacity, not this minimum.
        reserve = min(2048, max(256, limit // 8))
        if remaining < reserve:
            raise ContextBudgetExceeded(count, limit, reserve)
        return {**body, 'max_tokens': remaining}

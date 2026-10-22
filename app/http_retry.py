"""일시적 외부 API 장애만 제한적으로 재시도합니다. 요청/응답 원문은 기록하지 않습니다."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
import time

import httpx

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4  # 최초 요청 + 추가 3회
PERMANENT_CODES = {'invalid_api_key', 'insufficient_quota', 'billing_hard_limit_reached',
                   'model_not_found', 'permission_denied', 'access_denied'}


def error_info(response):
    try:
        payload = response.json()
        error = payload.get('error', {}) if isinstance(payload, dict) else {}
        return error if isinstance(error, dict) else {}
    except ValueError:
        return {}


def retryable(response):
    status = response.status_code
    if status not in (408,409,425,429) and not (500 <= status < 600 and status not in (501,505)):
        return False
    info = error_info(response)
    if any(info.get(key) in PERMANENT_CODES for key in ('code','type') if isinstance(info.get(key),str)):
        return False
    return True


def retry_delay(attempt, response=None):
    """2·4·8초 지수 대기 + 작은 무작위 지연. 서버 Retry-After도 최대 60초까지 반영합니다."""
    delay = 2 ** attempt + random.uniform(0, .5)
    if response is not None:
        value = response.headers.get('Retry-After')
        if value:
            try:
                server_delay = float(value)
            except ValueError:
                try:
                    when = parsedate_to_datetime(value)
                    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
                    server_delay = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError,ValueError,OverflowError):
                    server_delay = 0
            if math.isfinite(server_delay):
                delay = max(delay, server_delay)
    return min(60., delay)


class RequestFailure(Exception):
    def __init__(self, reason, attempts):
        self.reason, self.attempts = reason, attempts
        super().__init__(reason)


def request_with_retry(send, *, service):
    """send는 같은 요청을 다시 보내는 함수입니다. 이미 성공한 다른 배치는 재실행하지 않습니다."""
    for attempt in range(1, MAX_ATTEMPTS+1):
        response = None
        try:
            response = send()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
            reason = '시간 초과' if isinstance(exc,httpx.TimeoutException) else '연결 오류'
            if attempt == MAX_ATTEMPTS:
                raise RequestFailure(reason,attempt) from None
        else:
            response.extensions['request_attempts'] = attempt
            if not retryable(response) or attempt == MAX_ATTEMPTS:
                return response
            reason = f'HTTP {response.status_code}'
        delay = retry_delay(attempt,response)
        if response is not None:
            response.close()
        # service는 코드에서 정한 단계명입니다. URL/OC/키/질문/응답 본문을 로그에 넣지 않습니다.
        logger.warning('%s 요청 재시도 %d/%d: %s, %.1f초 후',service,attempt,MAX_ATTEMPTS-1,reason,delay)
        time.sleep(delay)

# 회사 sLLM 추론 호출 Python 코드 — 전달용

다른 채팅이나 프로젝트에 이 문서 전체를 전달하면 됩니다. 아래 코드는 LawRAG의 회사 LLM 호출 경로를 독립 실행 예제로 정리한 것입니다. LawRAG 모듈, DB, 검색, 임베딩, FastAPI에 의존하지 않습니다.

## 1. 연결 방식

```text
Python → HTTP POST {COMPANY_CHAT_BASE_URL}/chat/completions
       → 회사 vLLM의 OpenAI 호환 API
       ← 일반 JSON 응답 또는 SSE 스트리밍 응답
```

- 모델은 문자열 `model`로 선택합니다. 공유된 모델 ID는 `google-gemma-4-31B-it-awq`이며 실제 배포의 `/v1/models` 응답과 일치해야 합니다.
- 이 직접 호출 방식에는 RabbitMQ, NSE 요청 큐, `physical_model_id`가 필요하지 않습니다.
- 주소·포트는 현재 배포 값을 사용합니다. `BASE_URL`에는 `/v1`까지 넣고 `/chat/completions`는 코드에서 붙입니다.
- 회사 API 키가 비어 있으면 `Authorization` 헤더를 보내지 않습니다. GPT용 키를 대신 사용하지 않습니다.

## 2. 설치와 설정

Python 3.10 이상에서 실행합니다.

```powershell
python -m pip install httpx==0.28.1 python-dotenv==1.1.0
```

아래 Python 코드를 `company_llm_client.py`로 저장하고, 같은 폴더에 `local.env`를 만듭니다. `<회사서버주소>`, `<현재포트>`는 실제 값으로 교체합니다.

```dotenv
COMPANY_CHAT_BASE_URL=http://<회사서버주소>:<현재포트>/v1
COMPANY_CHAT_MODEL=google-gemma-4-31B-it-awq
COMPANY_CHAT_API_KEY=
AI_TIMEOUT_SECONDS=120

# auto: /v1/models 및 /tokenize로 입력·출력 토큰 예산 계산
# server: 토큰 사전 계산 없이 서버 기본값 사용
COMPANY_CHAT_MAX_TOKENS=auto

# 회사 프록시를 거치면 안 되는 대상. 호스트/IP만 넣고 포트는 제외합니다.
NO_PROXY=localhost,127.0.0.1,::1,<회사서버주소>
```

설정 우선순위는 **프로세스 환경변수 → 코드 옆 local.env → 코드 옆 .env**입니다. 이 예제는 회사 서버만 호출하므로 `CHAT_BACKEND`를 읽지 않습니다. 회사 서버 호출 실패 시 다른 모델 서버로 전환하지 않습니다.

## 3. 독립 실행 Python 코드

`chat()`은 일반 응답의 전체 문자열을 반환합니다. `stream_chat()`은 새 텍스트가 올 때마다 `on_delta(새 텍스트)`를 호출하고, 정상 완료되면 전체 답변을 반환합니다.

```python
import argparse
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv


class LLMError(RuntimeError):
    pass


def load_settings():
    folder = Path(__file__).resolve().parent
    load_dotenv(folder / "local.env", override=False)
    load_dotenv(folder / ".env", override=False)
    base = os.getenv("COMPANY_CHAT_BASE_URL", "").strip().rstrip("/")
    model = os.getenv("COMPANY_CHAT_MODEL", "").strip()
    key = os.getenv("COMPANY_CHAT_API_KEY", "").strip()
    mode = os.getenv("COMPANY_CHAT_MAX_TOKENS", "auto").strip()
    timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "120"))
    url = urlsplit(base)
    if (url.scheme not in ("http", "https") or not url.hostname
            or not url.path.endswith("/v1") or url.query or url.fragment
            or url.username or url.password or url.port == 0):
        raise LLMError("COMPANY_CHAT_BASE_URL에 http(s)://호스트:포트/v1을 지정하세요.")
    if not model or mode not in ("auto", "server"):
        raise LLMError("모델 ID와 COMPANY_CHAT_MAX_TOKENS(auto/server)를 확인하세요.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise LLMError("AI_TIMEOUT_SECONDS는 0보다 커야 합니다.")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return base, model, headers, timeout, mode


def checked_json(response):
    if not response.is_success:
        # 요청·응답 원문이나 인증 키를 오류 메시지에 출력하지 않습니다.
        raise LLMError(f"회사 LLM HTTP 오류: {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise LLMError("서버 응답이 JSON 형식이 아닙니다.") from None
    if not isinstance(data, dict) or data.get("error"):
        raise LLMError("서버가 오류 응답을 반환했습니다.")
    return data


def prepare_body(client, base, model, messages, mode, stream):
    body = {"model": model, "messages": messages,
            "temperature": 0.1, "stream": stream}
    if mode == "server":
        return body

    models = checked_json(client.get(base + "/models"))
    matching = [m for m in models.get("data", []) if m.get("id") == model]
    limit = matching[0].get("max_model_len") if len(matching) == 1 else None
    if type(limit) is not int or limit <= 0:
        raise LLMError("/v1/models에서 모델의 max_model_len을 확인할 수 없습니다.")

    # /tokenize는 /v1 아래가 아니라 vLLM 서버 루트에 있습니다.
    tokens = checked_json(client.post(base[:-3] + "/tokenize", json={
        "model": model, "messages": messages, "add_generation_prompt": True,
    }))
    count = tokens.get("count")
    token_limit = tokens.get("max_model_len", limit)
    if (type(count) is not int or count < 0
            or type(token_limit) is not int or token_limit <= 0):
        raise LLMError("/tokenize의 count 또는 max_model_len이 올바르지 않습니다.")
    limit = min(limit, token_limit)
    remaining = limit - count
    reserve = min(2048, max(256, limit // 8))
    if remaining < reserve:
        raise LLMError(f"입력 {count} + 출력 여유 {reserve}가 문맥 한도 {limit}을 넘습니다. "
                       "대화 이력이나 참고 자료를 줄인 뒤 요청하세요.")
    body["max_tokens"] = remaining
    return body


def chat(messages):
    """stream=false: 전체 응답을 받은 뒤 답변 문자열 반환."""
    base, model, headers, timeout, mode = load_settings()
    with httpx.Client(headers=headers, timeout=timeout) as client:
        body = prepare_body(client, base, model, messages, mode, stream=False)
        data = checked_json(client.post(base + "/chat/completions", json=body))
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
        if (choice["finish_reason"] != "stop" or message.get("refusal")
                or message.get("tool_calls") or message.get("function_call")
                or not isinstance(content, str) or not content.strip()):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError("일반 응답이 정상 완료되지 않았거나 텍스트 답변이 없습니다.") from None
    return content


def sse_events(lines):
    """SSE의 빈 줄을 이벤트 경계로 처리. 주석·event/id 필드는 건너뜁니다."""
    parts = []
    for line in lines:
        if line == "":
            if parts:
                yield "\n".join(parts)
                parts = []
        elif line.startswith("data:"):
            parts.append(line[5:].lstrip(" "))


def stream_chat(messages, on_delta):
    """stream=true: 새 content 조각을 콜백에 전달하고 전체 답변 반환."""
    base, model, headers, timeout, mode = load_settings()
    pieces, finished = [], False
    with httpx.Client(headers=headers, timeout=timeout) as client:
        body = prepare_body(client, base, model, messages, mode, stream=True)
        with client.stream("POST", base + "/chat/completions", json=body) as response:
            if not response.is_success:
                raise LLMError(f"회사 LLM HTTP 오류: {response.status_code}")
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                raise LLMError("stream=true 요청에 SSE 응답이 오지 않았습니다.")
            for event in sse_events(response.iter_lines()):
                if event == "[DONE]":
                    answer = "".join(pieces)
                    if not finished or not answer.strip():
                        raise LLMError("정상 완료 신호 또는 답변이 없습니다.")
                    return answer
                try:
                    payload = json.loads(event)
                    if not isinstance(payload, dict) or payload.get("error"):
                        raise ValueError()
                    choices = payload["choices"]
                    if choices == []:  # 사용 토큰 통계만 담은 이벤트
                        continue
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise ValueError()
                    choice = choices[0]
                    if type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
                        raise ValueError()
                    delta = choice["delta"]
                    if (not isinstance(delta, dict) or delta.get("refusal")
                            or delta.get("tool_calls") or delta.get("function_call")):
                        raise ValueError()
                    text = delta.get("content")
                    reason = choice.get("finish_reason")
                    if text is not None and not isinstance(text, str):
                        raise ValueError()
                    if finished and (text or reason):
                        raise ValueError()
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise LLMError("스트리밍 이벤트 형식이 올바르지 않습니다.") from None
                if reason == "length":
                    raise LLMError("출력 토큰 한도로 답변이 중단됐습니다.")
                if reason not in (None, "stop"):
                    raise LLMError("모델이 답변을 정상 완료하지 못했습니다.")
                # reasoning/reasoning_content 등은 출력하지 않습니다.
                if text:
                    pieces.append(text)
                    on_delta(text)
                if reason == "stop":
                    finished = True
    raise LLMError("[DONE] 완료 신호 전에 연결이 끊겼습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="회사 sLLM HTTP 호출")
    parser.add_argument("question", nargs="?", default="대한민국의 수도를 한 문장으로 알려줘.")
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    messages = [
        {"role": "system", "content": "한국어로 간결하게 답하세요."},
        {"role": "user", "content": args.question},
    ]
    try:
        if args.no_stream:
            print(chat(messages))
        else:
            stream_chat(messages, lambda text: print(text, end="", flush=True))
            print()
    except (LLMError, httpx.HTTPError, ValueError) as error:
        detail = str(error) if isinstance(error, LLMError) else "연결·시간 초과 또는 설정 오류"
        parser.exit(1, "\n호출 실패: " + detail + "\n")
```

이 예제는 텍스트 채팅과 일반적인 vLLM 응답 형식을 대상으로 합니다. VLM 배포여도 위 요청은 텍스트만 보내며 이미지 입력과 tool calling은 포함하지 않습니다. HTTPX의 환경 프록시 설정을 그대로 따르므로 필요하면 `NO_PROXY`를 지정합니다. 타임아웃은 요청 전체의 총 실행시간이 아니라 각 네트워크 작업의 대기 제한입니다.

## 4. 실행과 다른 Python 코드에서의 사용

```powershell
# 스트리밍: 받는 즉시 터미널에 이어서 표시
python company_llm_client.py "벡터 검색을 설명해 줘."

# 일반 JSON 응답
python company_llm_client.py "벡터 검색을 설명해 줘." --no-stream
```

```python
from company_llm_client import chat, stream_chat

messages = [
    {"role": "system", "content": "한국어로 답하세요."},
    {"role": "user", "content": "벡터 검색이 뭐야?"},
    {"role": "assistant", "content": "의미가 비슷한 자료를 벡터로 찾는 방식입니다."},
    {"role": "user", "content": "키워드 검색과 비교해 줘."},
]

# 둘 중 필요한 방식을 선택합니다.
answer = chat(messages)
# answer = stream_chat(messages, lambda delta: print(delta, end="", flush=True))
```

화면에서는 `on_delta`로 받은 텍스트를 기존 답변에 이어 붙이면 됩니다. 한 번의 콜백이 반드시 한 글자나 한 토큰인 것은 아닙니다. 웹 서버에서 사용할 때는 이 동기 함수를 작업 스레드에서 실행하거나 비동기 클라이언트로 변환하고, 받은 조각을 브라우저용 SSE/WebSocket 등으로 중계합니다.

`stream_chat()`이 예외를 발생시키면 앞서 콜백으로 표시한 텍스트는 미완성 답변입니다. `[DONE]`과 `finish_reason=stop`을 확인해 함수가 반환된 뒤에만 완료 상태로 저장합니다. 예제는 자동 재시도를 하지 않습니다. 재시도를 추가한다면 일부 답변을 화면에 보낸 뒤 요청을 처음부터 반복해 내용이 중복되지 않도록 구분해야 합니다.

## 5. 문맥 길이와 LawRAG 코드와의 차이

- `auto`는 `/v1/models`에서 배포 한도를 읽고 `/tokenize`로 채팅 템플릿을 반영한 입력 길이를 계산합니다. 모델명만 보고 32,000 또는 32,768 토큰으로 가정하지 않습니다.
- `max_tokens`는 **출력 상한**입니다. 전체 문맥 한도에서 입력 토큰을 뺀 남은 길이를 사용합니다. 최소 출력 여유보다 적게 남으면 생성 전에 오류를 반환합니다.
- 예제는 긴 참고 자료나 대화를 자동으로 자르지 않습니다. 호출하는 쪽에서 오래된 대화·낮은 우선순위의 참고 자료를 줄여 다시 호출해야 합니다.
- `server`는 위 사전 계산과 `max_tokens` 지정을 생략합니다. 서버의 문맥 한도를 없애는 설정은 아닙니다.
- 일반 텍스트 답변에는 `response_format`을 보내지 않습니다. JSON 출력이 필요한 별도 작업은 서버 지원 여부에 맞게 요청 형식과 JSON 검증을 추가해야 합니다.

LawRAG의 원래 구현 위치는 다음과 같습니다. 이 문서의 예제는 설명·전달을 위해 필요한 기능을 독립적으로 구성했으며 원본 모듈을 그대로 복사한 것은 아닙니다.

| 원본 파일 | 담당 기능 |
| --- | --- |
| [app/provider.py](../app/provider.py) | `post()`, `stream_completion()`: 서버 선택·HTTP 요청·응답 |
| [app/chat_stream.py](../app/chat_stream.py) | SSE 이벤트 해석·완료 확인 |
| [app/vllm_context.py](../app/vllm_context.py) | 모델 한도 조회·실제 입력 토큰 계산 |
| [app/ai_endpoints.py](../app/ai_endpoints.py) | 회사 서버 주소·모델·인증 헤더 선택 |
| [app/http_retry.py](../app/http_retry.py) | 일시적 HTTP 오류 재시도. 독립 예제에서는 생략 |

**콜백 차이:** 원본 LawRAG의 `Provider.stream_completion(messages, on_text)`는 현재까지의 **누적 답변 전체**를 전달하므로 화면 문자열을 교체해야 합니다. 이 문서의 `stream_chat(messages, on_delta)`는 **새로 받은 텍스트 조각**만 전달하므로 화면 문자열 뒤에 붙입니다. 두 방식을 혼용하면 같은 문장이 반복 표시됩니다.

이 문서에는 실제 API 키나 접속 비밀번호를 포함하지 않습니다. 독립 예제의 실행·응답 처리는 모의 HTTP 응답으로 확인하며, 회사 서버 연결 여부는 실제 주소·모델·접근 환경에서 확인해야 합니다.

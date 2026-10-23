# OpenAI와 회사 AI 서버 연결

기존 OpenAI 설정을 유지하면서 채팅과 임베딩을 각각 회사 서버로 전환할 수 있습니다. 이 문서는 **OpenAI 호환 HTTP API** 설정을 설명합니다. NSE의 고유 RabbitMQ 채팅은 [LOCAL_MODEL 설정·통신 점검](nse_mq.md)을 참고하세요. 두 방식 모두 기존 임베딩을 유지할 수 있습니다.

## 회사 LLM 호출 코드 재사용

현재 `CHAT_BACKEND=company`는 MQ를 거치지 않고 `COMPANY_CHAT_BASE_URL`의 `/chat/completions`에 HTTP로 요청합니다. `BASE_URL`에 `/v1`이 포함되므로 실제 경로는 `/v1/chat/completions`입니다. 모델은 `COMPANY_CHAT_MODEL`을 사용하며 `physical_model_id`는 보내지 않습니다.

| 코드 | 역할 |
| --- | --- |
| [config.py](../app/config.py)의 `Settings.endpoint('chat')` | 회사/OpenAI 서버 주소·모델·인증 선택 |
| [provider.py](../app/provider.py)의 `Provider.post()` | 일반 요청 전송, JSON 응답 반환, 오류·재시도 처리 |
| 같은 파일의 `Provider.stream_completion()` | `stream: true` 전송, SSE 응답 수신 |
| [chat_stream.py](../app/chat_stream.py)의 `stream_answer()` | SSE의 `choices[0].delta.content`를 콜백으로 전달하고 전체 답변 조립 |
| [vllm_context.py](../app/vllm_context.py) | 회사 vLLM의 입력 토큰·출력 여유 확인 |
| [chat_pipeline.py](../app/chat_pipeline.py)의 `run_chat()` | 검색·검토·참조 확장·입력 예산 조정 후 답변 호출 |

LawRAG 프로젝트 안의 다른 Python 코드에서는 다음처럼 재사용합니다. `local.env`의 회사 연결 설정을 먼저 지정하세요. 이 예시는 검색·DB·임베딩을 호출하지 않습니다.

```python
from app.config import Settings
from app.provider import Provider

provider = Provider(Settings())
messages = [
    {"role": "system", "content": "한국어로 간결하게 답하세요."},
    {"role": "user", "content": "벡터 검색을 한 문장으로 설명해 주세요."},
]

# 새 텍스트 조각이 도착할 때마다 실행됩니다. 반환값은 전체 답변입니다.
answer = provider.stream_completion(
    messages, lambda chunk: print(chunk, end="", flush=True)
)
print()

# 스트리밍 없이 JSON 응답을 받으려면 위 호출 대신 아래를 사용합니다.
# data = provider.post("/chat/completions", {
#     "model": provider.chat_model, "messages": messages, "temperature": 0.1
# })
# answer = data["choices"][0]["message"]["content"]
```

`Provider`는 다른 앱 모듈에 의존하므로 파일 하나만 복사하는 독립 SDK는 아닙니다. 별도 프로젝트에서 HTTP 직접 호출을 구현하려면 `stream_completion()`의 요청 형식과 `chat_stream.py`의 SSE 처리를 참고하세요. 각 콜백은 한 글자가 아니라 서버가 보낸 텍스트 조각입니다. `COMPANY_CHAT_MAX_TOKENS=auto`는 vLLM의 `/v1/models`·`/tokenize` 지원이 필요합니다. 직접 `Provider`를 호출하면 초과 입력을 자동으로 줄이지 않고 `ContextBudgetExceeded`를 발생시키며, LawRAG의 후보 축소는 `run_chat()`이 담당합니다.

## bge-m3-ko vLLM 직접 연결

`EMBEDDING_BACKEND=vllm`은 **LawRAG → HTTP `/v1/embeddings` → vLLM** 경로로 호출합니다. 임베딩 수집·재색인·질문 검색에 모두 같은 연결을 사용합니다. MQ 접속정보, NSE 요청 큐, `physical_model_id`는 필요 없습니다. 기존 `company` HTTP 설정과 같은 `COMPANY_EMBEDDING_*` 항목을 사용합니다. `local_embedding`/`LOCA_EMBEDDING`은 기존 MQ 방식이므로 직접 연결할 때는 `vllm`을 선택하세요.

기존 서버와 수집 작업을 종료하고, 프로젝트 폴더의 PowerShell에서 최신 코드를 받습니다.

```powershell
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw "git pull 실패" }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "의존성 설치 실패" }
if (Test-Path local.env) { Copy-Item local.env local.env.backup -Force }
Copy-Item local.env.vllm.example local.env -Force
```

`local.env`와 백업은 Git에서 제외됩니다. 예시는 기존 `.env`의 GPT 모델·키를 유지하면서 임베딩만 전환합니다. 기존 `local.env`에 OpenAI 키·모델·`DATA_DIR` 등 별도 설정을 넣었다면 백업에서 필요한 값을 옮기세요. 프로세스 환경변수가 있으면 파일보다 우선합니다.

별도 PowerShell 창에서 **LawRAG Python 서버를 실행하는 같은 PC**에 포트 전달을 열어 두세요. `kdev-context` 접속 설정과 권한이 필요합니다.

```powershell
kubectl --context=kdev-context -n davinci-ragllm port-forward svc/emb-bge-m3-ko-vllm-predictor 8000:80
```

예시 파일의 핵심 설정은 다음과 같습니다. `BASE_URL`에는 `/v1`까지만 넣습니다.

```dotenv
CHAT_BACKEND=openai
EMBEDDING_BACKEND=vllm
COMPANY_EMBEDDING_BASE_URL=http://127.0.0.1:8000/v1
COMPANY_EMBEDDING_MODEL=emb-bge-m3-ko
COMPANY_EMBEDDING_API_KEY=
AI_TIMEOUT_SECONDS=120
EMBEDDING_MAX_INPUT_BYTES=8000
EMBEDDING_BATCH_SIZE=16
```

`COMPANY_EMBEDDING_API_KEY`가 비어 있으면 인증 헤더를 보내지 않습니다. 요청은 `{"model":"emb-bge-m3-ko","input":["본문"]}`이며, 응답의 `data[i].index`와 `data[i].embedding`을 검증합니다. NSE MQ의 3중 배열 응답 형식은 사용하지 않습니다. GPT 요청에는 기존 OpenAI 키를 사용합니다.

배포 안내의 `max_model_len=8192`는 **토큰 수**입니다. 현재 8,000 UTF-8 바이트 단위 분할을 유지하며 8,192글자로 늘리지 않습니다. 모델 토크나이저의 정확한 토큰 계산과 같지는 않습니다. 긴 조문의 모든 구간을 임베딩한 뒤 통합하며 입력 길이 오류가 계속되면 바이트 한도를 낮추고 재색인합니다.

포트 전달 창을 켠 상태에서 프로젝트 창으로 돌아와 임베딩만 먼저 확인합니다. 이 명령은 DB를 바꾸거나 GPT/MQ를 호출하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m app.check_ai
if ($LASTEXITCODE -ne 0) { throw "임베딩 연결 확인 실패" }
```

출력의 `임베딩 연결: vllm`과 `임베딩 연결 성공: …차원`을 확인하세요. OpenAI 또는 MQ로 이미 수집한 데이터가 있다면 다음 명령으로 저장된 조문을 재색인합니다. RAW/HTML은 유지되고 법령 API를 다시 수집하지 않습니다. 같은 `emb-bge-m3-ko` 이름이어도 기존 MQ와 HTTP는 다른 연결 식별자로 취급하므로 재색인이 필요합니다.

```powershell
.\.venv\Scripts\python.exe -m app.reindex_embeddings
if ($LASTEXITCODE -ne 0) { throw "재색인 실패: 실패 항목을 확인하세요" }
.\.venv\Scripts\python.exe -m app.serve --lan
```

기존 수집 데이터가 없다면 재색인 명령을 생략하고 서버를 시작한 뒤 수집합니다. LawRAG 화면 포트 `8001`과 임베딩 포트 `8000`은 별개입니다. 포트 전달이 끊어지면 다시 실행해야 하며, 클러스터가 다른 모델로 바뀐 경우 기존 벡터를 그대로 사용하면 안 됩니다.

## 설정 파일과 선택 범위

우선순위는 **프로세스 환경변수 > 프로젝트 `local.env` > 프로젝트 `.env`**입니다. 변경 후 서버를 재시작합니다. `local.env`를 만들지 않으면 기존 OpenAI 연결은 그대로 동작합니다. 실제 설정 파일은 Git에서 제외하고 예시 파일만 관리합니다.

기존 `.env`의 `LLM_BASE_URL`, `LLM_API_KEY`(또는 `OPENAI_API_KEY`), `CHAT_MODEL`, `EMBEDDING_MODEL`은 `openai` 연결에 사용합니다. 회사 연결은 아래 별도 값을 사용하므로 OpenAI 키를 회사 서버로 보내지 않습니다.

프로젝트 루트의 PowerShell에서 실행합니다.

```powershell
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw "git pull 실패" }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path local.env)) { Copy-Item local.env.example local.env }
notepad local.env
```

예시 파일은 처음 복사해도 기존 연결이 바뀌지 않도록 두 선택값 모두 `openai`입니다. 아래처럼 선택값과 실제 사내 주소·모델을 입력합니다. IP는 예시입니다.

```dotenv
# 회사 sLLM + 기존 OpenAI 임베딩
CHAT_BACKEND=company
EMBEDDING_BACKEND=openai

COMPANY_CHAT_BASE_URL=http://10.0.0.20:8000/v1
COMPANY_CHAT_MODEL=company/sllm-v1
COMPANY_CHAT_API_KEY=
COMPANY_CHAT_JSON_MODE=json_object

# EMBEDDING_BACKEND=company로 바꿀 때 사용하는 연결
COMPANY_EMBEDDING_BASE_URL=http://10.0.0.21:8001/v1
COMPANY_EMBEDDING_MODEL=company/embedding-v1
COMPANY_EMBEDDING_API_KEY=
```

둘 다 회사 서버를 쓰려면 `EMBEDDING_BACKEND=company`도 지정합니다. 채팅만 OpenAI로 되돌리려면 `CHAT_BACKEND=openai`로 바꿉니다. 설정된 회사 서버가 실패해도 OpenAI로 자동 전환하지 않습니다. 회사 인증 키가 비어 있으면 `Authorization` 헤더를 생략하고, 있으면 `Bearer` 인증을 사용합니다. 다른 인증 방식은 별도 어댑터가 필요합니다.

서버 선택은 서버 전체 설정입니다. 채팅 화면에서 고르는 모델은 **현재 선택된 채팅 서버의 모델**입니다. `/models`를 지원하면 `org/model:version` 같은 사내 모델도 목록에 표시하고, 미지원이면 설정한 기본 모델 또는 모델 ID 직접 입력을 사용합니다.

## vLLM 채팅: 실제 문맥 한도와 스트리밍

회사 서버 `/v1/models`가 반환한 `data[].id`를 `COMPANY_CHAT_MODEL`에 넣습니다. 예를 들어 `google-gemma-4-31B-it-awq`이며, NSE의 `physical_model_id`는 HTTP 직접 호출에 사용하지 않습니다. 서버 포트가 바뀌면 `COMPANY_CHAT_BASE_URL`을 새 주소의 `/v1`로 바꾼 뒤 앱을 재시작합니다. 임베딩 서버 주소는 독립적인 설정입니다.

vLLM의 JSON 출력 제약과 실제 토큰 계산을 사용하려면 기존 `local.env`에 다음을 설정합니다.

```dotenv
CHAT_BACKEND=company
COMPANY_CHAT_JSON_MODE=json_object
COMPANY_CHAT_MAX_TOKENS=auto
```

`auto`에서는 매 채팅 요청의 첫 호출 때 `/v1/models`에서 선택한 모델의 `max_model_len`을 읽습니다. 각 LLM 호출 전에 vLLM 루트의 `/tokenize`에 같은 `model`, `messages`, `add_generation_prompt=true`를 보내 채팅 템플릿을 포함한 입력 토큰 수를 구합니다. 예를 들어 기본 주소가 `http://host:9010/v1`이면 토큰 계산 주소는 `http://host:9010/tokenize`입니다. 내부 JSON 단계와 최종 스트리밍 답변 모두 다음 상한을 사용합니다.

```text
max_tokens = 서버의 max_model_len − 실제 입력 토큰 수
```

모델 설명에 32K 이상을 지원한다고 되어 있어도, 현재 배포가 12,288을 보고하면 그 한도가 우선합니다. 서버 배포 한도를 늘리면 다음 채팅 요청부터 새 한도를 읽습니다. `/models`와 `/tokenize`가 서로 다른 한도를 보고하면 작은 값을 사용합니다. 고정 256/512토큰 상한을 두지 않으며, 최대치를 설정해도 모델이 종료 토큰을 내면 그 전에 끝납니다. 토큰 수는 글자 수가 아닙니다.

전송 전 출력 여유 `min(2048, max(256, 문맥 한도 // 8))`토큰을 확보합니다. 32,768토큰 서버에서는 최소 2,048토큰을 남겨야 하며, 실제 `max_tokens`는 위 식대로 남은 공간 전체를 사용합니다. 입력과 이 여유가 한도를 넘으면 생성 요청을 보내기 전에 후보 검토 묶음의 낮은 순위부터 전체 조문을 제외하고 다시 계산합니다. 한 조문이 단독으로도 들어가지 않으면 건너뜁니다. 충분성 검토·최종 답변에서도 검사합니다. 검토를 통과한 조문과 직접 참조한 별표는 버전·법령·조 번호가 일치하는 인용 관계로 묶습니다. 일반 참고 문맥을 먼저 줄인 뒤 낮은 우선순위의 조문·별표 묶음부터 제외하므로, 상위 근거의 별표가 하위 후보보다 먼저 빠지지 않도록 합니다. 마지막 묶음조차 한도에 들어가지 않으면 별표 단위로 제외하고 그 사유를 남깁니다. 문자 수 예산에도 같은 묶음 우선순위를 적용하며 참조 개수 제한에서는 직접 참조 별표를 먼저 확보합니다. 최종 답변에서는 오래된 대화 메시지를 먼저 줄입니다. 본문 일부를 잘라 단서·예외를 잃지 않으며, 최종 출처 번호와 별표 링크도 실제 전달 자료에 맞춰 다시 구성합니다.

제외 기록은 답변의 **근거 검토 기록 다운로드**에 `token_budget_omissions`로 남습니다. `token_budget_excluded`는 LLM의 관련성 탈락과 다릅니다. `answer_context`에는 최종 전달 자료의 법령명·조 번호·본문 길이·이미지 표시 개수가 기록되어 별표 전달 여부를 확인할 수 있습니다. 별표가 이미지 표시만 포함한다면 전달 자체로 표의 수치가 확보되지는 않습니다. 넣을 수 있는 조문이 전혀 남지 않으면 근거 답변을 생성하지 않고 이를 안내합니다. 질문·시스템 입력 자체도 들어가지 않으면 한도 오류를 표시합니다. `/tokenize`가 없거나 응답 형식이 다르면 추정값으로 진행하지 않습니다. 그런 서버는 담당자에게 토큰 계산 API 지원을 확인하거나 `COMPANY_CHAT_MAX_TOKENS=server`로 서버 기본값을 사용합니다. `server` 모드·OpenAI·MQ 연결에는 이 정확한 vLLM 토큰 검사와 자동 축소가 적용되지 않습니다. 기존 근거 개수·문자 예산 설정은 유지되므로 서버 한도가 커져도 검색 후보 수를 자동으로 늘리지는 않습니다.

웹 채팅의 **최종 답변은 `stream=true`로 요청**합니다. 회사 OpenAI 호환 서버와 GPT의 SSE `delta.content`를 수신하는 즉시 누적 `answer_text` 이벤트로 브라우저에 전달합니다. 완성된 답변을 타이머로 쪼개는 방식이 아닙니다. 내부 질문 재작성·관련성 검토·충분성 판단은 완전한 JSON을 검증해야 하므로 일반 응답을 사용합니다. `reasoning_content` 등 모델의 내부 추론 필드는 화면에 표시하지 않습니다.

완료 신호와 정상 종료를 확인한 후에만 답변·출처를 확정합니다. 중간 연결 실패나 `finish_reason=length`는 완성된 답변으로 저장하지 않고 오류를 표시합니다. 스트리밍 도중에는 자동으로 다시 생성하지 않아 중복 출력이 생기지 않습니다. 브라우저 연결이 끊기면 다음 청크에서 상위 HTTP 응답을 닫으며, 수신 대기 중에는 설정된 읽기 타임아웃까지 걸릴 수 있습니다.

프로젝트 PowerShell에서 다음 진단을 실행하면 임베딩·질문 재작성·실제 SSE 답변을 차례로 확인합니다. 임베딩 포트 전달 창은 켜 두세요.

```powershell
.\.venv\Scripts\python.exe -m app.check_ai --stream
```

`채팅 토큰 예산`과 `답변 토큰 예산`에는 `context_tokens`, `input_tokens`, `max_output_tokens`가 표시됩니다. JSON 재작성 실패 시 종료 사유, 최종 본문 길이, 추론 필드 길이, JSON 형식 여부를 표시합니다. 응답 원문·추론 본문·키는 진단에 포함하지 않습니다. 스트리밍 검사는 고정된 인사말 테스트의 최종 텍스트를 실시간으로 출력하며 DB를 수정하지 않습니다.

`COMPANY_CHAT_JSON_MODE=prompt` 서버가 반환한 완전한 JSON 코드블록(` ```json ... ``` `)은 모든 JSON 단계에서 처리합니다. 필수 필드나 후보 ID가 누락되면 계속 실패 처리하며, 설명문 속 JSON을 임의로 뽑거나 추론 텍스트를 결과로 대신 사용하지 않습니다. JSON 제약 옵션을 지원하는 vLLM은 `json_object`를 권장합니다.

## 회사 서버가 받아야 할 형식

`BASE_URL`은 모델 서버의 API 기본 경로이며, 앱이 뒤에 `/chat/completions` 또는 `/embeddings`를 붙입니다. 예를 들어 `http://10.0.0.20:8000/v1`을 지정하면 채팅 요청은 `http://10.0.0.20:8000/v1/chat/completions`로 갑니다.

### 채팅

```http
POST /v1/chat/completions
Content-Type: application/json
```

```json
{
  "model": "company/sllm-v1",
  "messages": [
    {"role": "system", "content": "JSON 형식으로 검색 문장을 반환하세요."},
    {"role": "user", "content": "선박 검사를 받을 때 필요한 서류는?"}
  ],
  "temperature": 0,
  "response_format": {"type": "json_object"}
}
```

응답은 다음처럼 `choices[0].message.content`에 문자열을 담습니다. 재작성·관련성 검토·충분성 판단 단계는 이 문자열 안의 JSON을 파싱하고 검증합니다. 최종 답변 단계의 `content`는 Markdown 문자열입니다.

```json
{
  "choices": [{
    "finish_reason": "stop",
    "message": {"role": "assistant", "content": "{\"query\":\"선박 검사 신청 구비 서류\"}"}
  }]
}
```

회사 모델은 답변 생성뿐 아니라 **질문 재작성, 근거 관련성 검토, 근거 충분성 판단**도 수행합니다. JSON을 정상 생성하는지 연결 진단과 실제 질문으로 확인해야 합니다.

| `COMPANY_CHAT_JSON_MODE` | 동작 |
| --- | --- |
| `json_object` (기본) | JSON 단계에서 `response_format={"type":"json_object"}` 전송 |
| `prompt` | `response_format`을 보내지 않고 프롬프트로 JSON을 요구. 응답 검증은 유지 |
| `auto` | 기존 모델명 기준의 JSON Object/JSON Schema 선택 유지 |

API가 `response_format`을 거절한다면 `prompt`를 사용할 수 있습니다. 이 옵션도 응답 구조를 바꾸지는 않습니다. 입력 후보의 판정이 빠지거나 중복되면 재시도 후 실패 처리하며, 검증 없이 근거로 통과시키지 않습니다.

### 임베딩

```http
POST /v1/embeddings
Content-Type: application/json
```

```json
{"model":"company/embedding-v1","input":["첫 번째 조문 본문","법령명 - 제1조(제목)"]}
```

```json
{
  "data": [
    {"index":0,"embedding":[0.12,0.34,0.56]},
    {"index":1,"embedding":[0.22,0.18,0.63]}
  ]
}
```

벡터는 숫자 배열이며 위 3차원 값은 형식 예시입니다. 입력 개수와 응답 개수가 같아야 하고, `index`로 입력 순서를 복원할 수 있어야 합니다. 같은 모델은 저장 문서와 검색 질문에 동일한 차원의 벡터를 반환해야 합니다. 누락·중복 인덱스, 비정상 숫자, 영벡터, 차원 불일치는 저장 전에 거절합니다.

질문/문서별 접두어, 전용 토크나이저 또는 다른 요청 필드가 필요한 모델은 그 규약도 어댑터에 반영해야 합니다. 현재는 제목·본문·질문 텍스트를 `input` 배열로 전송합니다.

## 입력 크기와 시간 제한

| 설정 | 기본값 | 용도 |
| --- | --- | --- |
| `AI_TIMEOUT_SECONDS` | `120` | AI HTTP 요청 타임아웃(초) |
| `EMBEDDING_MAX_INPUT_BYTES` | `8000` | 임베딩 구간별 최대 UTF-8 바이트. 4~8000 |
| `EMBEDDING_BATCH_SIZE` | `16` | 한 임베딩 요청의 구간 수. 1~16 |
| `EVIDENCE_REVIEW_MAX_CHARS` | `60000` | LLM 후보 검토 묶음의 문자 예산. 1000~60000 범위로 적용 |
| `CONTEXT_MAX_CHARS` | `60000` | 최종 근거 본문 문자 예산 |

바이트·문자 수는 토큰 수가 아니므로 모델의 실제 문맥 한도에 맞춰 낮춥니다. 예를 들어 짧은 문맥 모델을 시험할 때 `EMBEDDING_MAX_INPUT_BYTES=2000`, `EVIDENCE_REVIEW_MAX_CHARS=12000`, `CONTEXT_MAX_CHARS=12000`으로 시작해 확인할 수 있지만, 이 수치가 모든 모델에 맞는 것은 아닙니다. 시스템 프롬프트와 출력 토큰 공간도 필요합니다.

검토 묶음은 최대 두 개를 동시에 요청합니다. 한 조문 자체가 예산보다 길더라도 조문 중간을 잘라 단서를 없애지 않으므로, 서버가 그 조문을 처리할 수 없다면 길이 오류가 발생할 수 있습니다. 오류를 검토 통과로 대체하지 않습니다.

## 연결 확인과 임베딩 모델 전환

채팅 서버·모델만 바꾸는 경우 기존 임베딩은 그대로 사용합니다. 다음 진단은 실제 임베딩과 채팅 JSON 요청을 각 서버에 보내며 DB는 수정하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m app.check_ai --chat
```

임베딩만 확인하려면 `--chat`을 뺍니다. 진단 성공은 짧은 요청의 연결·응답 형식 확인이며 전체 법령 길이나 검색 품질을 보장하지 않습니다.

**임베딩 서버·모델·입력 분할 크기를 바꾸면 기존 벡터를 그대로 비교할 수 없습니다.** 벡터 식별자는 임베딩 주소·모델·기본값과 다른 입력 분할 크기를 포함하고, 검색 시 차원도 검사합니다. 서버 뒤의 모델 파일을 바꿨다면 같은 ID를 재사용하지 말고 버전이 구분되는 모델 ID를 지정합니다.

서버와 수집 작업을 종료한 뒤 선택한 임베딩 모델로 기존 자료를 재색인합니다.

```powershell
.\.venv\Scripts\python.exe -m app.reindex_embeddings
```

이 명령은 `.env`/`local.env`를 변경하지 않습니다. 저장된 RAW 해시를 확인하고 모든 수집 버전의 제목·본문 벡터를 교체합니다. 법령 API 재수집이나 HTML 교체는 하지 않습니다. 한 버전의 모든 임베딩이 성공해야 그 버전을 갱신하고, 실패한 버전은 기존 벡터를 보존합니다. 중단 후 같은 명령을 실행하면 완료된 버전을 건너뜁니다. 결과는 `DATA_DIR/embedding-reindex-report.json`에 남습니다. 실패 0건을 확인한 뒤 서버를 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m app.serve --lan
```

OpenAI와 회사 임베딩 데이터를 동시에 보존하며 비교하려면 `DATA_DIR`를 분리합니다. 별도 폴더는 기존 `laws.sqlite3`·`raw/`·`html/`을 함께 복사한 후 재색인하거나 새로 수집해서 준비합니다. **한 DB에 두 임베딩 공간을 동시에 저장·융합하는 기능은 아닙니다.**

## 회사 고유 API 형식인 경우

NSE의 RabbitMQ 규약은 [MQ 통신 점검 명령](nse_mq.md)으로 접속·일반 응답·실제 스트리밍·JSON 생성·임베딩을 확인할 수 있습니다. HTTP 응답 예시는 이 MQ 연동에 필요하지 않습니다. 웹에서 `LOCAL_MODEL`을 고르면 MQ를 사용하고 기존 GPT도 계속 선택할 수 있습니다. 임베딩은 별도로 `EMBEDDING_BACKEND=local_embedding`, `NSE_EMBEDDING_MODEL=emb-bge-m3-ko`로 설정하고 기존 벡터를 재색인합니다.

현재 연결 경계는 [ai_endpoints.py](../app/ai_endpoints.py)의 서버 선택과 [provider.py](../app/provider.py)의 HTTP 요청·응답 처리입니다. 검색·관계 확장·근거 검토 흐름은 이 계층 위에 있습니다.

회사 API가 `{"prompt":...}` → `{"answer":...}`처럼 다른 형식이라면 이 경계에 채팅/임베딩 어댑터를 추가해 앱 내부의 `messages`·`choices`와 `input`·`data[index,embedding]` 형식으로 변환합니다. 필요한 정보는 각 API의 URL, 인증 방식, 요청/응답 JSON 예시, 모델 ID, 최대 입력 길이입니다. 실제 키 값은 전달할 필요가 없습니다. 지원되지 않는 형식을 추측해서 보내거나 실패 시 다른 서버로 우회하지 않습니다.

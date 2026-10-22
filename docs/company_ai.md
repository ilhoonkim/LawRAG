# OpenAI와 회사 AI 서버 연결

기존 OpenAI 설정을 유지하면서 채팅과 임베딩을 각각 회사 서버로 전환할 수 있습니다. 현재 회사 서버 연동은 **OpenAI 호환 HTTP API**를 대상으로 합니다. URL만 바꿔 모든 회사 고유 JSON 형식을 처리하는 구조는 아니며, 아래 형식과 다르면 요청·응답 변환 코드를 추가해야 합니다.

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

현재 연결 경계는 [ai_endpoints.py](../app/ai_endpoints.py)의 서버 선택과 [provider.py](../app/provider.py)의 HTTP 요청·응답 처리입니다. 검색·관계 확장·근거 검토 흐름은 이 계층 위에 있습니다.

회사 API가 `{"prompt":...}` → `{"answer":...}`처럼 다른 형식이라면 이 경계에 채팅/임베딩 어댑터를 추가해 앱 내부의 `messages`·`choices`와 `input`·`data[index,embedding]` 형식으로 변환합니다. 필요한 정보는 각 API의 URL, 인증 방식, 요청/응답 JSON 예시, 모델 ID, 최대 입력 길이입니다. 실제 키 값은 전달할 필요가 없습니다. 지원되지 않는 형식을 추측해서 보내거나 실패 시 다른 서버로 우회하지 않습니다.

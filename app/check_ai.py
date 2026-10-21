"""Check the actual embedding configuration without printing credentials or touching DB."""
from .config import Settings, ai_key_source
from .provider import Provider
from .laws import SetupError


# python -m app.check_ai: 서버와 같은 Settings/Provider로 실제 짧은 임베딩 요청을 보냅니다.
# 키 값은 출력하지 않으며, 성공해도 법령 API 연결이나 DB 저장까지 검증한 것은 아닙니다.
def main():
    settings = Settings()
    print('AI 키 읽기:', '확인됨' if settings.api_key else '없음')
    print('AI 키 설정 위치:', ai_key_source())
    try:
        vectors = Provider(settings).embed(['법령 임베딩 연결 확인'])
    except (SetupError, ValueError) as exc:
        print(str(exc))
        return 1
    print(f'임베딩 연결 성공: {len(vectors[0])}차원. 키 인증·모델 호출이 확인됐습니다.')
    print('이 진단은 짧은 문장 1개만 검사합니다. 법령 본문 수집·DB 저장은 하지 않았습니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

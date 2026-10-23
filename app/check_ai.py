"""Check the selected AI servers without printing credentials or touching DB."""
import argparse
from .config import Settings, ai_key_source
from .provider import Provider
from .laws import SetupError


# python -m app.check_ai: 서버와 같은 Settings/Provider로 실제 짧은 임베딩 요청을 보냅니다.
# 키 값은 출력하지 않으며, 성공해도 법령 API 연결이나 DB 저장까지 검증한 것은 아닙니다.
def main(argv=None):
    parser = argparse.ArgumentParser(description='선택된 임베딩/채팅 서버 연결 확인 (실제 API 호출)')
    parser.add_argument('--chat', action='store_true', help='채팅 서버의 JSON 재작성 응답까지 검사')
    # 기존 코드에서 main()을 호출할 때 pytest 등의 프로세스 인자를 읽지 않습니다.
    args = parser.parse_args(argv if argv is not None else [])
    settings = Settings()
    endpoint = settings.endpoint('embedding')
    print('임베딩 연결:', endpoint.backend)
    if endpoint.backend == 'local_embedding':
        print('임베딩 인증: MQ 계정 (local.env 또는 netrc)')
    else:
        print('AI 키 읽기:', '설정됨' if endpoint.api_key else '없음 (사내 서버는 무인증 가능)')
    if endpoint.backend == 'openai':
        print('AI 키 설정 위치:', ai_key_source())
    try:
        vectors = Provider(settings).embed(['법령 임베딩 연결 확인'])
    except (SetupError, ValueError) as exc:
        print(str(exc))
        return 1
    print(f'임베딩 연결 성공: {len(vectors[0])}차원. 모델 호출이 확인됐습니다.')
    if args.chat:
        try:
            Provider(settings).rewrite_question('선박 검사를 받을 때 필요한 서류는?')
        except (SetupError, ValueError) as exc:
            print(str(exc))
            return 1
        print('채팅 연결 성공: 검색용 JSON 응답을 확인했습니다.')
    print('이 진단은 짧은 문장 1개만 검사합니다. 법령 본문 수집·DB 저장은 하지 않았습니다.')
    return 0


if __name__ == '__main__':
    import sys
    raise SystemExit(main(sys.argv[1:]))

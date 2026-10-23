"""python -m app.check_nse_mq [--connect-only | --stream]. DB는 열지 않습니다."""
import argparse
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys

from .config import ROOT, Settings
from .nse_mq import MqCheckError, NseMqClient, NseMqSettings


def terminal_text(text):
    # 모델이 출력한 터미널 제어 문자는 실행하지 않고 제거합니다.
    return ''.join(c for c in text if c in '\n\t' or (c.isprintable() and c != '\x7f'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='NSE RabbitMQ 연결/LLM/임베딩 점검 (DB·OpenAI 호출 없음)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--connect-only', action='store_true', help='접속·인증·요청 큐 조회만 수행; LLM 요청 없음')
    mode.add_argument('--stream', action='store_true', help='stream=true: 실제 델타와 END 수신 확인')
    mode.add_argument('--embedding', action='store_true', help='task=embedding: 시험 문장 2개의 벡터 확인')
    parser.add_argument('--model', type=int, help='NSE_PHYSICAL_MODEL_ID를 이번 실행에만 덮어쓰기')
    parser.add_argument('--embedding-model', help='NSE_EMBEDDING_MODEL의 served_model_name을 이번 실행에만 덮어쓰기')
    parser.add_argument('--timeout', type=float, default=120, help='발행부터 완료까지 제한 시간(초, 기본 120)')
    parser.add_argument('--max-tokens', type=int, default=128, help='출력 토큰 상한(기본 128)')
    parser.add_argument('--prompt', default='대한민국의 수도를 한 문장으로 알려주세요.', help='시험 질문')
    parser.add_argument('--json', action='store_true', help='JSON 생성도 검사; 시험 질문은 고정 JSON 요청으로 대체')
    parser.add_argument('--report', type=Path, default=ROOT/'data'/'nse-mq-check.json', help='접속정보·질문·답변을 제외한 진단 보고서')
    args = parser.parse_args(argv)
    if (args.connect_only or args.embedding) and args.json:
        parser.error('--json은 LLM 일반/스트리밍 시험에서만 사용할 수 있습니다.')
    if args.embedding and args.model is not None:
        parser.error('임베딩 모델은 숫자 --model 대신 --embedding-model 이름으로 지정하세요.')
    if args.embedding_model and not args.embedding:
        parser.error('--embedding-model은 --embedding과 함께 사용하세요.')
    # 라이브러리 자체 로그에는 주소·서버 오류 원문이 포함될 수 있습니다.
    logger = logging.getLogger('pika')
    handler = logging.NullHandler()
    # propagate=False는 pika 하위 logger에도 적용됩니다. 종료 시 복원합니다.
    old_propagate = logger.propagate
    logger.propagate = False
    logger.addHandler(handler)
    report, client, streamed = {}, None, False
    try:
        settings = NseMqSettings.from_env()
        if args.model is not None:
            settings = replace(settings, model_id=args.model)
        client = NseMqClient(settings)
        prompt = '{"ok":true} JSON 객체만 반환하세요. 설명이나 코드 블록은 쓰지 마세요.' if args.json else args.prompt

        def show_chunk(chunk):
            nonlocal streamed
            if not streamed:
                print('수신 중: ', end='', flush=True)
                streamed = True
            print(terminal_text(chunk), end='', flush=True)

        text = client.check(connect_only=args.connect_only, stream=args.stream, prompt=prompt,
                            timeout=args.timeout, max_tokens=args.max_tokens,
                            on_progress=lambda message: print(message, flush=True), on_chunk=show_chunk,
                            texts=[prompt, '벡터 검색은 의미가 유사한 문서를 찾습니다.'] if args.embedding else None,
                            served_model_name=(args.embedding_model or Settings().nse_embedding_model) if args.embedding else None)
        if streamed:
            print()
        if args.stream and not client.report.get('stream_observed'):
            raise MqCheckError('stream_unconfirmed', '최종 응답은 받았지만 생성 중 청크는 관찰하지 못했습니다. 한 조각짜리 답변은 END만 올 수 있으므로 --prompt로 조금 더 긴 답변을 요청해 확인하세요.')
        if args.json:
            try:
                value = json.loads(text)
                valid = isinstance(value, dict) and value.get('ok') is True
            except ValueError:
                valid = False
            if not valid:
                raise MqCheckError('json', 'MQ 응답은 받았지만 요청한 JSON 객체를 생성하지 못했습니다. 모델의 JSON 지시 이행을 확인하세요.')
        report.update(client.report, success=True, json_checked=args.json)
        if args.connect_only:
            print('접속 확인 성공. LLM 요청은 보내지 않았습니다.')
        elif args.embedding:
            print(f'임베딩 확인 성공 · 입력 {report["input_count"]}개 · 벡터 {report["vector_count"]}개 · {report["dimensions"]}차원 · {report["elapsed_seconds"]}초')
        else:
            print('최종 응답:', terminal_text(text))
            label = '스트리밍' if args.stream else '일반 응답'
            print(f'{label} 확인 성공 · 수신 {report["messages"]}건 · 델타 {report["chunks"]}개 · {report["elapsed_seconds"]}초')
            if args.stream:
                print(f'수신 상태 {report["stream_statuses"]} · 첫 청크까지 {report["first_chunk_seconds"]:.3f}초')
            if args.json:
                print('JSON 형식 확인 성공.')
        code = 0
    except MqCheckError as exc:
        if streamed:
            print()
        report.update(client.report if client else {}, success=False, stage=exc.stage, error=str(exc))
        print(f'점검 실패 [{exc.stage}]: {exc}')
        if report.get('request_id'):
            print('서버 로그 조회용 request_id:', report['request_id'])
        code = 1
    except KeyboardInterrupt:
        print('\n점검을 중단했습니다. 이미 발행한 NSE 추론은 서버에서 계속 진행될 수 있습니다.')
        report.update(client.report if client else {}, success=False, stage='interrupted')
        code = 130
    finally:
        logger.propagate = old_propagate
        logger.removeHandler(handler)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('진단 보고서 저장:', args.report)
    except OSError:
        print('진단 보고서 저장 실패. 저장 폴더 권한을 확인하세요.')
        return 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())

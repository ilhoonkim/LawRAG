"""OpenAI 호환 SSE의 최종 답변만 중계합니다. 추론 필드는 화면에 내보내지 않습니다."""
import json

from .laws import SetupError


def stream_answer(lines, on_text, cancelled):
    content, finished, chunks = '', False, 0
    event = []
    for line in lines:
        if cancelled():
            raise InterruptedError()
        if line:
            if line.startswith('data:'):
                event.append(line[5:].lstrip(' '))
            continue
        if not event:
            continue
        data, event = '\n'.join(event), []
        if data == '[DONE]':
            if not finished or not content.strip():
                raise SetupError('스트리밍 응답이 정상 완료되지 않았습니다. 최종 답변을 저장하지 않았습니다.')
            return content, {'content_chunks': chunks, 'content_chars': len(content), 'finish_reason': 'stop'}
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError()
            choices = payload['choices']
            if choices == []:  # OpenAI/vLLM의 usage 전용 이벤트
                continue
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            if type(choice.get('index', 0)) is not int or choice.get('index', 0) != 0:
                raise ValueError()
            delta = choice['delta']
            if not isinstance(delta, dict) or delta.get('refusal') or delta.get('tool_calls') or delta.get('function_call'):
                raise ValueError()
            text, reason = delta.get('content'), choice.get('finish_reason')
            if text is not None and not isinstance(text, str):
                raise ValueError()
            if finished and (text or reason):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SetupError('AI 스트리밍 응답 형식 오류입니다. 완료되지 않은 답변을 저장하지 않았습니다.') from None
        if reason == 'length':
            raise SetupError('답변이 출력 길이 제한으로 중단됐습니다 (finish_reason=length). 완료된 답변으로 저장하지 않았습니다.')
        if reason not in (None, 'stop'):
            raise SetupError('모델이 스트리밍 답변을 정상적으로 완료하지 않았습니다.')
        if text:
            content += text
            chunks += 1
            on_text(content)
        if reason == 'stop':
            finished = True
    # finish_reason만 받은 뒤 소켓이 끊긴 경우도 정상 완료로 취급하지 않습니다.
    raise SetupError('스트리밍 연결이 완료 신호 전에 끊겼습니다. 다시 요청해 주세요.')

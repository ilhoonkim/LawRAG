"""LLM의 구조화 응답 파싱. 원문을 오류·진단 로그에 노출하지 않습니다."""
import json
import re


class ChatJSONError(ValueError):
    pass


def completion_parts(response):
    try:
        choice = response['choices'][0]
        message = choice['message']
        if not isinstance(choice, dict) or not isinstance(message, dict):
            raise TypeError()
        return choice, message
    except (KeyError, IndexError, TypeError):
        raise ChatJSONError('AI 응답 구조 오류: choices[0].message가 없습니다') from None


def json_object(response):
    """완전한 JSON 객체 또는 그 객체만 감싼 Markdown 코드블록을 읽습니다.

    설명문 속 임의의 중괄호를 추출하거나 추론 텍스트를 최종 응답으로 대신 쓰지
    않습니다. JSON 파싱 후 query/decisions 등의 필수 필드는 호출부에서 검증합니다.
    """
    choice, message = completion_parts(response)
    if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
        raise ChatJSONError('모델이 응답을 거부했습니다')
    if choice.get('finish_reason') == 'length':
        raise ChatJSONError('응답이 출력 길이 제한으로 중단됐습니다 (finish_reason=length)')
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        if message.get('reasoning_content') or message.get('reasoning'):
            raise ChatJSONError('추론 텍스트만 있고 최종 응답 content가 비어 있습니다')
        raise ChatJSONError('최종 응답 content가 비어 있거나 문자열이 아닙니다')
    fenced = re.fullmatch(r'\s*```(?:json)?\s*(.*?)\s*```\s*', content, re.S | re.I)
    try:
        parsed = json.loads(fenced[1] if fenced else content)
    except json.JSONDecodeError:
        raise ChatJSONError('AI 응답의 JSON 형식 오류: JSON 객체만 반환해야 합니다') from None
    if not isinstance(parsed, dict):
        raise ChatJSONError('AI 응답이 JSON 객체가 아닙니다')
    return parsed


def response_diagnostics(response):
    """고정 진단문·길이·불리언만 반환. 질문·생성문·추론·임의 응답 필드는 제외."""
    try:
        choice, message = completion_parts(response)
    except ChatJSONError as exc:
        return {'structure': str(exc)}
    reason = choice.get('finish_reason')
    content = message.get('content')
    reasoning = message.get('reasoning_content') or message.get('reasoning')
    summary = {
        'finish_reason': reason if isinstance(reason, str) and reason in
            ('stop', 'length', 'content_filter', 'tool_calls', 'function_call') else '미제공/기타',
        'content_chars': len(content) if isinstance(content, str) else 0,
        'reasoning_chars': len(reasoning) if isinstance(reasoning, str) else 0,
        'markdown_fence': isinstance(content, str) and content.strip().startswith('```'),
    }
    try:
        json_object(response)
        summary['json_object'] = True
    except ChatJSONError as exc:
        summary['json_object'] = False
        summary['issue'] = str(exc)
    return summary

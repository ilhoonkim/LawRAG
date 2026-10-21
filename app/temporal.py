"""Conservative point-in-time resolution. Ambiguous periods require a date."""
import re
from datetime import date, datetime, timedelta, timezone


# 서버의 OS 시간대가 UTC여도 법령 시행일의 '오늘'은 한국 날짜로 일관되게 판단합니다.
def today_in_korea():
    return datetime.now(timezone(timedelta(hours=9))).date()


# 명시적인 as_of가 최우선이며, 없으면 질문의 단일 날짜를 사용합니다.
# 기간·모호한 과거 표현은 임의 날짜로 바꾸지 않고 확인 메시지를 반환합니다.
# 시간 표현이 없을 때만 한국 기준 오늘을 사용합니다.
def resolve_date(question: str, explicit: date | None = None):
    if explicit:
        return explicit, None
    matches = list(re.finditer(r'(\d{4})\s*(?:년\s*|[-./])(\d{1,2})\s*(?:월\s*|[-./])(\d{1,2})(?:일)?', question))
    if len(matches) == 1:
        match = matches[0]
        try:
            value = date(*map(int, match.groups()))
        except ValueError:
            return None, '유효한 기준일을 입력해 주세요. 예: 2020-01-15'
        remainder = question[:match.start()] + question[match.end():]
        if re.search(r'\d{4}\s*년|\d{4}[-./]\d|이전|이후|부터|까지|기간', remainder):
            return None, '기간 내에 법령이 개정될 수 있습니다. 조회할 기준일을 YYYY-MM-DD 형식으로 지정해 주세요.'
        return value, None
    if matches or re.search(r'\d{4}\s*년|\d{4}[-./]\d|작년|재작년|올해|당시|예전|과거|지난|그때', question):
        return None, '시기에 맞는 법령을 찾으려면 기준일을 지정해 주세요. 예: “2020년 1월 15일 기준 선박안전법 적용범위”'
    return today_in_korea(), None

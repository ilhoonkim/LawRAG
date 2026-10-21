"""Conservative point-in-time resolution. Ambiguous periods require a date."""
import re
from datetime import date, datetime, timedelta, timezone


def today_in_korea():
    return datetime.now(timezone(timedelta(hours=9))).date()


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

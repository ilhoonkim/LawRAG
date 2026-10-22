"""한국어 명사·복합어의 정확한 일치와 문서 빈도 기반 검색 신호.

형태소 분석은 질문과 실제 일치 지점의 짧은 어절에 수행합니다. 조문 전체를 다시
분석하거나 임베딩하지 않고, 현재 검색 대상 버전에서 명사 일치를 확인합니다.
법적 적용 여부는 이 점수로 확정하지 않으며 기존 LLM 검토에서 확인합니다.
"""
from functools import lru_cache
from math import log1p
import re
from threading import Lock
import unicodedata

from .laws import SetupError


# 법령 전반에 반복되는 일반 표현은 명사 일치 가산에서 제외합니다.
# 수치·부정·시점 조건은 원질의, 임베딩, LLM 입력에 그대로 남습니다.
STOP_NOUNS = frozenset('기준 규정 사항 내용 관련 경우 방법 여부 무엇 언제 얼마 몇 '
                       '년도 이후 이전 이상 이하 미만 초과 법 법률 시행령 시행규칙 '
                       '행정규칙 해당 다음 각 호 조 항 것 수 등 때 영 이 그 저'.split())
NOUN_TAGS = frozenset(('NNG', 'NNP', 'SL'))
_analyzer = None
_analyzer_lock = Lock()


def normalize(text):
    """띄어쓰기 차이는 허용하되 문장부호를 가로지르는 합성 일치는 막습니다."""
    text = unicodedata.normalize('NFKC', text).lower()
    return re.sub(r'[^가-힣a-z0-9]', ' ', re.sub(r'\s+', '', text))


@lru_cache(maxsize=512)
def query_terms(query):
    """(독립 명사, 인접 명사의 복합 표현)을 반환합니다. 조사는 포함하지 않습니다."""
    global _analyzer
    query = unicodedata.normalize('NFKC', query)
    # 단일 분석기를 재사용하고 초기화·동시 접근을 직렬화합니다.
    with _analyzer_lock:
        if _analyzer is None:
            try:
                from kiwipiepy import Kiwi
            except ImportError as exc:
                raise SetupError('명사 검색에 필요한 패키지가 없습니다. python -m pip install -r requirements.txt 실행 후 서버를 재시작하세요.') from exc
            _analyzer = Kiwi(num_workers=1)
        tokens = _analyzer.tokenize(query)
    nouns, compounds, run = set(), set(), []
    for token in tokens:
        form = normalize(token.form)
        is_noun = token.tag in NOUN_TAGS and form not in STOP_NOUNS
        # '의장수'의 수처럼 앞 명사에 붙은 구성 음절은 독립 검색어에서만
        # 제외합니다. 여기서 run까지 지우면 전문 복합 명칭이 손실됩니다.
        component = (token.tag in NOUN_TAGS and form in STOP_NOUNS and bool(run)
                     and run[-1].start + run[-1].len == token.start)
        suffix = token.tag == 'XSN' and bool(run)
        if not (is_noun or component or suffix):
            run = []
            continue
        if run:
            end = run[-1].start + run[-1].len
            if query[end:token.start].strip():
                run = []
        run = (run + [token])[-6:]
        # 닻·쇠 등 한 글자 실질 명사도 법령 검색에서는 유효합니다.
        if is_noun and form:
            nouns.add(form)
        # 복합어를 통째로 보존하되 조사·동사·문장부호를 건너뛰어 만들지 않습니다.
        for start in range(len(run) - 1):
            if run[start].tag not in NOUN_TAGS:
                continue
            expression = normalize(query[run[start].start:token.start + token.len])
            if 2 <= len(expression) <= 30 and ' ' not in expression:
                compounds.add(expression)
    return frozenset(nouns), frozenset(compounds - nouns)


@lru_cache(maxsize=512)
def term_pattern(term):
    # 같은 복합 명칭의 띄어쓰기 차이는 허용합니다.
    return re.compile(r'\s*'.join(re.escape(char) for char in term))


@lru_cache(maxsize=16384)
def context_terms(context):
    nouns, compounds = query_terms(context)
    return nouns | compounds


def contains_noun(text, term):
    """문자열 후보를 찾은 뒤 해당 어절의 형태소가 실제 명사인지 확인합니다.

    전체 법령을 요청마다 형태소 분석하지 않습니다. 일치 지점의 짧은 어절만
    분석하고 재사용합니다. '선의'를 '범선의'의 일부로 세거나 '점검 토대'를
    '검토'로 붙여 세는 오일치를 막으면서 '한국형닻'의 닻은 보존합니다.
    """
    for match in term_pattern(term).finditer(text):
        start, end = match.span()
        while start > 0 and text[start-1].isalnum():
            start -= 1
        while end < len(text) and text[end].isalnum():
            end += 1
        if term in context_terms(text[start:end]):
            return True
    return False


class NounIndex:
    """검색 요청 범위의 빈도만 사용합니다. 개정·삭제·기준일 변경은 즉시 반영됩니다."""
    def __init__(self, titles, bodies):
        # 어절 경계 검증을 위해 공백을 보존합니다.
        self.titles = [unicodedata.normalize('NFKC', t).lower() for t in titles]
        self.bodies = [unicodedata.normalize('NFKC', b).lower() for b in bodies]
        self.count = len(self.bodies)
        self.postings = {}
        self.plans = {}

    def occurrences(self, term):
        if term not in self.postings:
            title = frozenset(i for i, text in enumerate(self.titles) if contains_noun(text, term))
            body = frozenset(i for i, text in enumerate(self.bodies) if contains_noun(text, term))
            self.postings[term] = (title, body)
        return self.postings[term]

    def plan(self, query):
        if query not in self.plans:
            nouns, compounds = query_terms(query)
            # 붙여 만든 복합 표현은 실제 자료에 있을 때만 채택합니다.
            # 독립 명사는 미일치해도 분모에 남겨 질문의 핵심 조건 누락을 반영합니다.
            terms = nouns | {t for t in compounds if any(self.occurrences(t))}
            weights = {}
            for term in sorted(terms):
                title, body = self.occurrences(term)
                frequency = len(title | body)  # 같은 조에서 반복해도 한 번만 셉니다.
                idf = log1p((self.count + .5) / (frequency + .5))
                length = 1 + min(max(0, len(term) - 2), 8) / 8
                # '경보', '감지기', '단독경보형감지기'를 별개의 만점 신호로 중복 계산하지 않습니다.
                component = .25 if any(term != other and term in other for other in terms) else 1.
                weights[term] = idf * length * component
            self.plans[query] = NounPlan(self, weights)
        return self.plans[query]


class NounPlan:
    def __init__(self, index, weights):
        self.index = index
        self.weights = weights
        self.total = sum(weights.values())
        self.peak = max(weights.values(), default=0)
        # 실제 자료에서 확인되는 드문 핵심어만 후보 보호에 사용합니다.
        # 질문에만 있는 단어는 일반 점수의 분모에 남지만 이 목록을 지배하지
        # 않습니다. 법령명만 맞는 후보는 본문 일치가 없어 보호되지 않습니다.
        observed = {term: weight for term, weight in weights.items()
                    if any(index.occurrences(term))}
        peak = max(observed.values(), default=0)
        self.focus_terms = {term for term, weight in observed.items()
                            if weight >= .5 * peak
                            and len(set().union(*index.occurrences(term))) <= max(3, index.count * .1)}

    def matches(self, position, field):
        offset = 0 if field == 'title' else 1
        return [term for term in self.weights if position in self.index.occurrences(term)[offset]]

    def score(self, matches):
        if not self.total or not matches:
            return 0.
        covered = sum(self.weights[t] for t in matches) / self.total
        strongest = max(self.weights[t] for t in matches) / self.peak
        # 질문 전체의 명사 일치율에 핵심 용어 일치를 더합니다. 긴 질문 속 하나의
        # 희소한 설비명이 맞아도 충분히 반영하며, 반복 횟수·본문 길이로 희석하지 않습니다.
        return min(1., .6 * covered + .4 * strongest)

    def score_text(self, text):
        text = unicodedata.normalize('NFKC', text).lower()
        return self.score([term for term in self.weights if contains_noun(text, term)])

    def focus_score(self, matches):
        matched = self.focus_terms.intersection(matches)
        return self.score(matches) if matched else 0.

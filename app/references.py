"""Version-scoped article → annex relationships, independent of a graph DB."""
from dataclasses import dataclass
import re
from urllib.parse import urljoin, urlsplit


@dataclass
class Annex:
    number: str
    title: str
    body: str
    section_id: str
    source_url: str = ''


# 별표 번호·제목·추출 가능한 텍스트와 공식 파일 링크를 분리해 저장합니다.
# 링크만 있는 별표는 본문 미확보 상태이며, 파일 다운로드/OCR을 수행하는 함수는 아닙니다.
def parse_annexes(root, law_name):
    from .laws import text, content_text
    result = []
    for node in root.findall('.//별표단위'):
        kind = text(node, '별표구분')
        if kind and kind != '별표':
            continue
        number = text(node, '별표번호')
        if not number.isdigit():
            continue
        branch = text(node, '별표가지번호')
        label = f'별표 {int(number)}' + (f'의{int(branch)}' if branch.isdigit() and int(branch) else '')
        body_node = node.find('별표내용')
        body = content_text(body_node).strip() if body_node is not None else ''
        body = re.sub(r'<br\s*/?>', '\n', body, flags=re.I)
        # An image/file URL is not the content of an annex. Do not feed it as law text.
        if re.search(r'<(?:img|table|a|object|iframe)\b|https?://', body, re.I):
            body = ''
        source_url = ''
        for child in node.iter():
            if 'URL' in child.tag.upper() or '링크' in child.tag:
                candidate = ''.join(child.itertext()).strip()
                url = urljoin('https://www.law.go.kr/', candidate)
                if candidate and urlsplit(url).scheme == 'https' and urlsplit(url).hostname in {'law.go.kr','www.law.go.kr'}:
                    source_url = url
                    break
        if any(a.number == label for a in result):
            raise ValueError(f'중복 별표: {label}')
        result.append(Annex(label, text(node, '별표제목') or label, body, f'{law_name}-{label.replace(" ", "")}', source_url))
    return result


# 인용 바로 앞 문맥으로 대상 법령을 추정합니다. 명시된 「법령명」을 우선하고,
# '같은 법', '영'처럼 확인이 필요한 표시는 graph.py에서 약칭 선언과 함께 해석합니다.
# 완전한 법률 문법 분석기가 아니므로 불명확한 관계를 확정 사실로 다루면 안 됩니다.
def reference_target(body, position, own_law):
    # 범위·열거에서 생략된 법령명은 첫 인용에서 이어받습니다. 문장·줄바꿈을
    # 넘거나 서술문을 건너뛰지 않으며, 긴 목록도 재귀 없이 처리합니다.
    position = reference_head(body, position)
    prefix = re.split(r'[.\n。]', body[:position])[-1]
    if re.search(r'이\s*(?:법|영|규칙)\s*$', prefix):
        return own_law
    if re.search(r'같은\s*법\s*$', prefix):
        return '같은 법(대상 확인 필요)'
    # 바로 앞의 '법/영'은 앞쪽에 등장한 다른 「법령명」보다 우선합니다.
    # 선언된 약칭과 실제 상위법 매핑은 HTML/문맥 확장 단계에서 수행합니다.
    generic = re.search(r'(?:^|\s)(법|시행령|시행규칙|영|규칙)\s*$', prefix)
    if generic:
        return generic[1] + '(대상 확인 필요)'
    quoted = re.findall(r'「([^」]+)」', prefix)
    if quoted:
        return quoted[-1]
    unquoted = re.search(r'([가-힣]+(?:법|시행령|시행규칙))\s*(?:제\d+조(?:의\d+)?\s*)?$', prefix)
    if unquoted:
        return unquoted[1]
    return own_law


def reference_head(body, position):
    """'법 제4조부터 제11조까지'의 제11조는 제4조 앞의 '법'을 이어받습니다."""
    pattern = r'제\s*\d+\s*조(?:\s*의\s*\d+)?|별표\s*\d+(?:\s*의\s*\d+)?'
    connector = (r'\s*(?:제\s*\d+\s*(?:항|호)(?:\s*의\s*\d+)?\s*)*'
                 r'(?:부터|내지|~|∼|및|또는|,|·|까지\s*(?:,|및|또는))\s*')
    while True:
        prefix = re.split(r'[.\n。]', body[:position])[-1]
        matches = list(re.finditer(pattern, prefix))
        if not matches or not re.fullmatch(connector, prefix[matches[-1].end():]):
            return position
        position = position - len(prefix) + matches[-1].start()


def annex_references(body, own_law):
    result = set()
    for match in re.finditer(r'별표\s*(\d+)(?:\s*의\s*(\d+))?', body):
        label = f'별표 {int(match[1])}' + (f'의{int(match[2])}' if match[2] else '')
        target_law = reference_target(body, match.start(), own_law)
        result.add((label, target_law))
    # Explicit ranges are expanded, with a bound to prevent runaway malformed references.
    for match in re.finditer(r'별표\s*(\d+)\s*(?:부터|내지|~|∼)\s*(?:별표\s*)?(\d+)\s*(?:까지)?', body):
        first, last = map(int, match.groups())
        if 0 <= last-first <= 50:
            target = reference_target(body, match.start(), own_law)
            result.update((f'별표 {n}', target) for n in range(first,last+1))
    return sorted(result)


# 조문 번호와 대상 법령의 쌍만 추출합니다. 실제 수집 여부·시점·본문 존재 검사는
# 이 함수를 사용하는 저장소/문맥 확장 계층에서 수행합니다.
def article_links(body, own_law, source_number=''):
    result = set()
    for match in re.finditer(r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?', body):
        number = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
        target = reference_target(body, match.start(), own_law)
        if number != source_number or target != own_law:
            result.add((number, target))
    for match in re.finditer(r'제\s*(\d+)\s*조\s*(?:부터|내지|~|∼)\s*(?:제\s*)?(\d+)\s*조(?:까지)?', body):
        first,last = map(int,match.groups())
        if 0 <= last-first <= 50:
            target = reference_target(body, match.start(), own_law)
            result.update((f'제{n}조',target) for n in range(first,last+1)
                          if f'제{n}조'!=source_number or target!=own_law)
    return sorted(result)

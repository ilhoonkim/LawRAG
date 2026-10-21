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


def reference_target(body, position, own_law):
    prefix = re.split(r'[.\n。]', body[:position])[-1]
    if re.search(r'이\s*법\s*$', prefix):
        return own_law
    quoted = re.findall(r'「([^」]+)」', prefix)
    if quoted:
        return quoted[-1]
    if re.search(r'같은\s*법\s*$', prefix):
        return '같은 법(대상 확인 필요)'
    unquoted = re.search(r'([가-힣]+(?:법|시행령|시행규칙))\s*(?:제\d+조(?:의\d+)?\s*)?$', prefix)
    if unquoted:
        return unquoted[1]
    generic = re.search(r'(?:^|\s)(법|시행령|시행규칙|영|규칙)\s*$', prefix)
    if generic:
        return generic[1] + '(대상 확인 필요)'
    return own_law


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
            target = next((law for number, law in result if number == f'별표 {first}'), own_law)
            result.update((f'별표 {n}', target) for n in range(first,last+1))
    return sorted(result)


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
            target = next((law for number,law in result if number==f'제{first}조'),own_law)
            result.update((f'제{n}조',target) for n in range(first,last+1) if f'제{n}조'!=source_number)
    return sorted(result)

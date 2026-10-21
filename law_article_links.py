"""국가법령정보센터 본문 XML → 같은 법령의 조문·별표 링크가 있는 HTML.

이 파일 하나만 복사해서 사용할 수 있습니다. Python 3.10 이상, 표준 라이브러리만
사용하며 LawRAG 앱, DB, API 키, 임베딩 모델은 필요하지 않습니다.

실행 예시:
    python law_article_links.py law.xml -o law.html

다른 코드에서 사용:
    from law_article_links import xml_to_html
    html = xml_to_html(xml_bytes)

입력은 법령 본문 API(law/eflaw)의 XML 한 건입니다. 검색 목록 XML이나 행정규칙,
별표 첨부파일 자체는 처리하지 않습니다. XML의 별표 정보는 함께 읽습니다.
한 XML 안에 있는 조문·별표끼리 연결하므로 다른 개정
버전의 조문이 섞이지 않습니다. 링크 클릭 시 API 호출 없이 HTML 안에서 이동합니다.

대상 법령 판별은 기존 앱과 같은 문맥 규칙을 사용합니다. 명시된 다른 법령이나
대상이 불명확한 '법/영/같은 법'은 연결하지 않습니다. 약칭·복잡한 인용 문맥을
완전히 해석하는 법률 문법 분석기는 아니며, 타 법령 링크 확장은 포함하지 않습니다.
"""

import argparse
from dataclasses import dataclass, field
import html
from pathlib import Path
import re
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET


@dataclass
class Article:
    number: str
    title: str
    body: str
    section_id: str


@dataclass
class Annex(Article):
    source_url: str = ''


@dataclass
class Law:
    name: str
    effective: str
    articles: list[Article]
    annexes: list[Annex] = field(default_factory=list)


def _text(node: ET.Element, path: str) -> str:
    item = node.find(path)
    return ''.join(item.itertext()).strip() if item is not None else ''


def _content_text(node: ET.Element) -> str:
    # 단순 itertext()와 달리 실제 <br> 태그를 줄바꿈으로 보존합니다.
    parts = [node.text or '']
    for child in node:
        parts.append('\n' if child.tag.lower() == 'br' else _content_text(child))
        parts.append(child.tail or '')
    return ''.join(parts)


def parse_law_xml(raw: bytes) -> Law:
    """조문 번호·가지번호·제목과 항/호/목을 포함한 본문을 추출합니다."""
    # UTF-16/32로 저장된 XML의 선언도 확인합니다. 외부 엔터티는 허용하지 않습니다.
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw.replace(b'\x00', b''), re.I):
        raise ValueError('DTD 또는 엔터티 선언이 있는 XML은 지원하지 않습니다.')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('유효한 XML 파일이 아닙니다.') from exc

    name = _text(root, './/기본정보/법령명_한글')
    if not name:
        raise ValueError('법령명이 없습니다. 검색 목록이 아닌 법령 본문 XML을 입력하세요.')

    articles, seen = [], set()
    for node in root.findall('.//조문단위'):
        # 편·장 등의 표제는 조문으로 만들지 않습니다.
        if _text(node, '조문여부') == '전문':
            continue
        num = _text(node, '조문번호')
        branch = _text(node, '조문가지번호')
        if not num:
            continue
        if not re.fullmatch(r'[0-9]+', num) or (branch and not re.fullmatch(r'[0-9]+', branch)):
            raise ValueError('조문번호 또는 조문가지번호가 숫자가 아닙니다.')
        number = f'제{int(num)}조' + (f'의{int(branch)}' if branch and int(branch) else '')
        parts = []
        for child in node.iter():
            if child.tag in {'조문내용', '항내용', '호내용', '목내용'}:
                value = _content_text(child).strip()
                if value:
                    # CDATA나 이스케이프된 문자열 속 <br>도 줄바꿈으로 처리합니다.
                    parts.append(re.sub(r'<br\s*/?>', '\n', value, flags=re.I))
        body = '\n'.join(parts)
        if not body:
            continue
        if number in seen:
            raise ValueError(f'중복 조문입니다: {number}. 단일 법령 버전의 XML인지 확인하세요.')
        seen.add(number)
        articles.append(Article(number, _text(node, '조문제목'), body, f'{name}-{number}'))

    if not articles:
        raise ValueError('XML에서 본문이 있는 조문을 찾지 못했습니다.')
    return Law(name, _text(root, './/기본정보/시행일자'), articles, parse_annexes(root, name))


def parse_annexes(root: ET.Element, law_name: str) -> list[Annex]:
    """별표의 번호·제목·본문 및 공식 원문 파일 주소를 읽습니다."""
    result, seen = [], set()
    for node in root.findall('.//별표단위'):
        kind = _text(node, '별표구분')
        if kind and kind != '별표':
            continue
        num, branch = _text(node, '별표번호'), _text(node, '별표가지번호')
        if not num.isdigit():
            continue
        number = f'별표 {int(num)}' + (f'의{int(branch)}' if branch.isdigit() and int(branch) else '')
        if number in seen:
            raise ValueError(f'중복 별표입니다: {number}')
        seen.add(number)
        body_node = node.find('별표내용')
        body = _content_text(body_node).strip() if body_node is not None else ''
        body = re.sub(r'<br\s*/?>', '\n', body, flags=re.I)
        # 파일 주소/HTML만 있는 응답을 별표 본문이라고 표시하지 않습니다.
        if re.search(r'<(?:img|table|a|object|iframe)\b|https?://', body, re.I):
            body = ''
        source_url = ''
        for child in node.iter():
            if 'URL' in child.tag.upper() or '링크' in child.tag:
                candidate = ''.join(child.itertext()).strip()
                url = urljoin('https://www.law.go.kr/', candidate)
                if candidate and urlsplit(url).scheme == 'https' and urlsplit(url).hostname in {'law.go.kr', 'www.law.go.kr'}:
                    source_url = url
                    break
        result.append(Annex(number, _text(node, '별표제목') or number, body,
                            f'{law_name}-{number.replace(" ", "")}', source_url))
    return result


def reference_target(body: str, position: int, own_law: str) -> str:
    """참조 앞 문맥으로 대상 법령을 구분합니다. 기존 앱의 판별 규칙입니다."""
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


def link_article_references(article: Article, law_name: str, by_number: dict[str, Article]) -> str:
    """본문의 조 번호와 모든 별표 번호(가지번호 포함)에 내부 이동 링크를 붙입니다."""
    parts, cursor = [], 0
    for match in re.finditer(r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?|별표\s*(\d+)(?:\s*의\s*(\d+))?', article.body):
        # 외부 원문은 반드시 escape합니다. 코드가 직접 만든 <a> 태그만 HTML로 삽입합니다.
        parts.append(html.escape(article.body[cursor:match.start()]))
        if match[3]:
            number = f'별표 {int(match[3])}' + (f'의{int(match[4])}' if match[4] else '')
        else:
            number = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
        target = by_number.get(number) if reference_target(article.body, match.start(), law_name) == law_name else None
        # 같은 XML에 대상과 제목이 있어야 연결합니다. 자기 자신이나 미수집 대상은 그대로 둡니다.
        if target and target.title and number != article.number:
            label = html.escape(number + (f'({target.title})' if target.title != number else ''))
            parts.append(f'<a href="#{quote(target.section_id, safe="")}">{label}</a>')
        else:
            parts.append(html.escape(match[0]))
        # '제6조제1항제12호'에서는 조 번호만 링크가 되고 뒤의 항·호 표기는 유지됩니다.
        cursor = match.end()
    parts.append(html.escape(article.body[cursor:]))
    return ''.join(parts)


def render_html(law: Law) -> str:
    """별도 CSS/JS 없이 브라우저에서 열 수 있는 HTML을 만듭니다."""
    by_number = {article.number: article for article in [*law.articles, *law.annexes]}
    toc, sections = [], []
    for article in [*law.articles, *law.annexes]:
        heading = html.escape(article.number + (f'({article.title})' if article.title and article.title != article.number else ''))
        toc.append(f'<a href="#{quote(article.section_id, safe="")}">{heading}</a>')
        body = link_article_references(article, law.name, by_number)
        extra = ''
        if isinstance(article, Annex):
            if not article.body:
                body = '별표 본문이 확보되지 않았습니다. 원문 파일을 확인하세요.'
            if article.source_url:
                extra = f'<p><a href="{html.escape(article.source_url, quote=True)}" target="_blank" rel="noopener noreferrer">별표 원문 파일</a></p>'
        sections.append(
            f'<section id="{html.escape(article.section_id, quote=True)}">'
            f'<h2>{heading}</h2><pre>{body}</pre>{extra}</section>'
        )
    effective = law.effective
    if re.fullmatch(r'[0-9]{8}', effective):
        effective = f'{effective[:4]}.{effective[4:6]}.{effective[6:]}'
    return f'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(law.name)}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: system-ui, sans-serif; color: #334155; }}
header {{ padding: 24px; border-bottom: 1px solid #e2e8f0; text-align: center; }}
h1 {{ font-size: 24px; }} h2 {{ font-size: 18px; color: #334e8a; }}
.layout {{ display: grid; grid-template-columns: 260px minmax(0, 1fr); }}
nav {{ padding: 16px; position: sticky; top: 0; max-height: 100vh; overflow: auto; align-self: start; }}
nav a {{ display: block; padding: 8px; }}
main {{ min-width: 0; padding: 24px; }}
section {{ padding: 20px; margin-bottom: 16px; border: 1px solid #e2e8f0; border-radius: 6px; }}
section:target {{ background: #fff6cc; border-color: #eab308; }}
pre {{ font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.9; }}
a {{ color: #2563eb; }} a:focus-visible {{ outline: 2px solid #2563eb; }}
@media (max-width: 700px) {{ .layout {{ display: block; }} nav {{ position: static; max-height: 200px; }} }}
</style>
</head>
<body>
<header><h1>{html.escape(law.name)}</h1><p>시행일: {html.escape(effective or '미상')}</p></header>
<div class="layout"><nav aria-label="조문 목차">{''.join(toc)}</nav>
<main>{''.join(sections)}</main></div>
</body>
</html>'''


def xml_to_html(raw: bytes) -> str:
    """API 응답 바이트를 바로 변환할 때 사용하는 진입점입니다."""
    return render_html(parse_law_xml(raw))


def main() -> None:
    parser = argparse.ArgumentParser(description='법령 본문 XML을 조문·별표 링크가 있는 HTML로 변환')
    parser.add_argument('xml', type=Path, help='법령 본문 API에서 받은 XML 파일')
    parser.add_argument('-o', '--output', type=Path, help='출력 HTML 경로 (기본: 입력 파일명.html)')
    args = parser.parse_args()
    output = args.output or args.xml.with_suffix('.html')
    if args.xml.resolve() == output.resolve():
        parser.error('출력 경로는 입력 XML과 달라야 합니다.')
    try:
        page = xml_to_html(args.xml.read_bytes())
        output.write_text(page, encoding='utf-8')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'변환 실패: {exc}\n')
    print(f'HTML 생성 완료: {output}')


if __name__ == '__main__':
    main()

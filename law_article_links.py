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


# 숫자 있는 별표뿐 아니라 '별표와 같다', '[별표]' 같은 번호 없는 참조도 찾습니다.
# 한국어 단어 중간의 '특별표'나 '별표준'을 참조로 잘못 잡지 않도록 경계를 확인합니다.
HTML_REFERENCE_PATTERN = re.compile(
    r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?'
    r'|(?<![가-힣A-Za-z0-9_])별표(?:\s*(\d+)(?:\s*의\s*(\d+))?'
    r'|(?=$|[\s\]\)」】.,;:!?]|(?:와|과|에|의|를|는|은|가|로)(?=\s|$)))'
)


# 읽는 순서: xml_to_html → parse_law_xml/parse_annexes → render_html
# → link_article_references → reference_target.
# 파싱 단계에서는 원문 텍스트를 유지하고, 출력 단계에서만 링크를 붙입니다.


@dataclass
class Article:
    # number는 매핑 키(예: 제6조의2), section_id는 HTML 이동 목적지입니다.
    # body에는 제목만이 아니라 조·항·호·목의 내용이 들어갑니다.
    number: str
    title: str
    body: str
    section_id: str


@dataclass
class Annex(Article):
    # 별표도 번호·제목·본문·앵커 구조가 같아 Article을 상속합니다.
    # source_url은 내부 앵커가 아닌 공식 첨부파일 주소이며, 본문 없이 주소만 있을 수도 있습니다.
    source_url: str = ''


@dataclass
class Law:
    # XML 한 건을 하나의 문서로 묶습니다. 서로 다른 버전의 articles/annexes를 합치지 마세요.
    # 이 함수들은 최신 버전 여부를 조회하지 않으며 입력 XML의 시행일을 그대로 사용합니다.
    name: str
    effective: str
    articles: list[Article]
    annexes: list[Annex] = field(default_factory=list)


def _text(node: ET.Element, path: str) -> str:
    # 이름·번호 등 짧은 메타데이터용입니다. 본문 줄바꿈 처리는 _content_text가 담당합니다.
    item = node.find(path)
    return ''.join(item.itertext()).strip() if item is not None else ''


def _content_text(node: ET.Element) -> str:
    # 단순 itertext()와 달리 실제 <br> 태그를 줄바꿈으로 보존합니다.
    parts = [node.text or '']
    for child in node:
        parts.append('\n' if child.tag.lower() == 'br' else _content_text(child))
        # tail은 닫는 태그 뒤의 문자열입니다. 이를 빼면 태그 다음의 원문이 유실됩니다.
        parts.append(child.tail or '')
    return ''.join(parts)


def parse_law_xml(raw: bytes) -> Law:
    """조문 번호·가지번호·제목과 항/호/목을 포함한 본문을 추출합니다."""
    # UTF-16/32로 저장된 XML의 선언도 확인합니다. 외부 엔터티는 허용하지 않습니다.
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw.replace(b'\x00', b''), re.I):
        raise ValueError('DTD 또는 엔터티 선언이 있는 XML은 지원하지 않습니다.')
    try:
        # bytes를 그대로 넘겨 XML 선언의 인코딩을 파서가 해석하게 합니다.
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('유효한 XML 파일이 아닙니다.') from exc

    name = _text(root, './/기본정보/법령명_한글')
    if not name:
        raise ValueError('법령명이 없습니다. 검색 목록이 아닌 법령 본문 XML을 입력하세요.')

    # seen은 같은 조 번호의 중복을 감지합니다. 뒤의 by_number 사전에서 조문이
    # 덮어써지면 잘못된 본문으로 이동할 수 있으므로 중복을 발견하면 변환을 중단합니다.
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
        # 0006 같은 번호를 제6조로 정규화합니다. 가지번호 0/빈 값은 일반 조문입니다.
        number = f'제{int(num)}조' + (f'의{int(branch)}' if branch and int(branch) else '')
        parts = []
        for child in node.iter():
            # 전체 XML의 텍스트를 합치면 번호·메타데이터까지 본문에 섞이므로 내용 태그만 읽습니다.
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
    # 별표도 반드시 같은 XML에서 읽어 조문과 동일한 수집 버전에 속하게 합니다.
    return Law(name, _text(root, './/기본정보/시행일자'), articles, parse_annexes(root, name))


def parse_annexes(root: ET.Element, law_name: str) -> list[Annex]:
    """별표의 번호·제목·본문 및 공식 원문 파일 주소를 읽습니다."""
    result, seen = [], set()
    for node in root.findall('.//별표단위'):
        # 같은 컨테이너에 별지서식 등이 섞일 수 있습니다. 구분이 명시된 경우 별표만 취급합니다.
        kind = _text(node, '별표구분')
        if kind and kind != '별표':
            continue
        num, branch = _text(node, '별표번호'), _text(node, '별표가지번호')
        if not num.isdigit():
            continue
        # 별표1/별표 01 등의 본문 표현이 동일한 키 '별표 1'에 대응하도록 정규화합니다.
        # '별표 1의2'는 '별표 1'과 별개의 대상입니다.
        # 0은 번호 없는 별표의 내부 매핑 키입니다. 표시할 때만 '별표'로 바꾸어
        # 기존 HTML의 별표0 앵커와 연결 호환성을 유지합니다.
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
        # 첨부파일을 다운로드하거나 OCR하지는 않습니다. 공식 HTTPS 주소만 링크로 보관합니다.
        source_url = ''
        for child in node.iter():
            if 'URL' in child.tag.upper() or '링크' in child.tag:
                candidate = ''.join(child.itertext()).strip()
                url = urljoin('https://www.law.go.kr/', candidate)
                if candidate and urlsplit(url).scheme == 'https' and urlsplit(url).hostname in {'law.go.kr', 'www.law.go.kr'}:
                    source_url = url
                    break
        # 제목이 없으면 번호를 표시명으로 사용합니다. 본문이 없어도 별표 위치를 생성해
        # 미확보 안내와 파일 링크를 보여줄 수 있습니다. 별표 앵커에서는 번호의 공백을 뺍니다.
        result.append(Annex(number, _text(node, '별표제목') or number, body,
                            f'{law_name}-{number.replace(" ", "")}', source_url))
    return result


def reference_target(body: str, position: int, own_law: str) -> str:
    """참조 앞 문맥으로 대상 법령을 구분합니다. 기존 앱의 판별 규칙입니다."""
    # position은 '제N조' 또는 '별표 N'의 시작 위치입니다.
    # 마침표/줄바꿈 이후의 앞 문맥만 검사하므로, 이전 문장의 법령명을 계속 이어받지 않습니다.
    prefix = re.split(r'[.\n。]', body[:position])[-1]
    if re.search(r'이\s*법\s*$', prefix):
        # 예: '「다른법」과 달리 이 법 제6조'는 현재 문서의 제6조를 가리킵니다.
        return own_law
    quoted = re.findall(r'「([^」]+)」', prefix)
    if quoted:
        # 가장 가까운 명시적 법령명을 사용합니다. 실제 대상과의 이름 일치는 호출자가 검사합니다.
        return quoted[-1]
    if re.search(r'같은\s*법\s*$', prefix):
        # 지시어를 현재 문서로 단정하지 않고, 호출자가 링크 생성을 건너뛰게 합니다.
        return '같은 법(대상 확인 필요)'
    unquoted = re.search(r'([가-힣]+(?:법|시행령|시행규칙))\s*(?:제\d+조(?:의\d+)?\s*)?$', prefix)
    if unquoted:
        return unquoted[1]
    generic = re.search(r'(?:^|\s)(법|시행령|시행규칙|영|규칙)\s*$', prefix)
    if generic:
        return generic[1] + '(대상 확인 필요)'
    # 다른 법령을 가리키는 표현을 찾지 못한 인용은 같은 법령의 참조로 취급합니다.
    # 이는 문맥 규칙에 따른 기본값이며, 모든 자연어 인용을 정확히 판별한다는 뜻은 아닙니다.
    return own_law


def reference_heading(number: str, title: str) -> str:
    """번호 없는 별표의 API 키(별표 0)를 화면에서는 별표로 표시합니다."""
    display = '별표' if number == '별표 0' else number
    return display + (f'({title})' if title and title not in (number, display) else '')


def link_article_references(article: Article, law_name: str, by_number: dict[str, Article]) -> str:
    """본문의 조 번호와 모든 별표 번호(가지번호 포함)에 내부 이동 링크를 붙입니다."""
    # by_number 예: {'제6조': Article(...), '별표 2': Annex(...)}.
    # 외부 법령의 같은 번호를 잘못 연결하지 않도록 대상 법령 판별 후에 사전을 조회합니다.
    # cursor는 마지막으로 출력한 원문 위치입니다. 링크 사이의 문장도 빠짐없이 이어 붙입니다.
    parts, cursor = [], 0
    # 정규식 그룹 1/2는 조 번호/가지번호, 3/4는 별표 번호/가지번호입니다.
    # 명시적으로 적힌 참조만 변환하며 '별표 1~3'의 생략된 2·3을 새로 만들어 넣지는 않습니다.
    for match in HTML_REFERENCE_PATTERN.finditer(article.body):
        # 외부 원문은 반드시 escape합니다. 코드가 직접 만든 <a> 태그만 HTML로 삽입합니다.
        parts.append(html.escape(article.body[cursor:match.start()]))
        if match[3]:
            number = f'별표 {int(match[3])}' + (f'의{int(match[4])}' if match[4] else '')
        elif match[1]:
            number = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
        else:
            # 번호가 생략된 참조는 실제 번호 없는 별표(0)가 있을 때만 연결합니다.
            # 번호 있는 별표 중 첫 번째 것을 추측해서 선택하지 않습니다.
            number = '별표 0'
        target = by_number.get(number) if reference_target(article.body, match.start(), law_name) == law_name else None
        # 같은 XML에 대상과 제목이 있어야 연결합니다. 자기 자신이나 미수집 대상은 그대로 둡니다.
        if target and target.title and number != article.number:
            label = html.escape(reference_heading(number, target.title))
            # href의 # 뒤는 URL 인코딩하고, 목적지 section의 id는 HTML 속성 이스케이프합니다.
            # 브라우저가 fragment를 해석해 같은 문서 내 해당 section으로 이동합니다.
            parts.append(f'<a href="#{quote(target.section_id, safe="")}">{label}</a>')
        else:
            parts.append(html.escape(match[0]))
        # '제6조제1항제12호'에서는 조 번호만 링크가 되고 뒤의 항·호 표기는 유지됩니다.
        cursor = match.end()
    parts.append(html.escape(article.body[cursor:]))
    return ''.join(parts)


def render_html(law: Law) -> str:
    """별도 CSS/JS 없이 브라우저에서 열 수 있는 HTML을 만듭니다."""
    # 전체 대상을 먼저 매핑해야 뒤에 나오는 조문·별표를 앞의 본문에서도 참조할 수 있습니다.
    by_number = {article.number: article for article in [*law.articles, *law.annexes]}
    toc, sections = [], []
    for article in [*law.articles, *law.annexes]:
        # 목차와 본문 링크가 같은 section_id를 사용해야 두 경로 모두 같은 위치로 이동합니다.
        heading = html.escape(reference_heading(article.number, article.title))
        toc.append(f'<a href="#{quote(article.section_id, safe="")}">{heading}</a>')
        body = link_article_references(article, law.name, by_number)
        extra = ''
        if isinstance(article, Annex):
            # 파일 주소만 받은 별표에도 내부 이동 목적지는 존재합니다.
            # 그 위치에서 사용자가 별도 원문 파일을 열며, 이 코드가 파일 내용을 추출하지는 않습니다.
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
    # pre-wrap은 원문 줄바꿈을 유지하며, :target은 클릭한 조문·별표를 강조합니다.
    # CSS까지 포함한 단일 HTML이므로 파일을 전달받은 개발자도 별도 서버 없이 열 수 있습니다.
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
    # HTTP 수신·파일 저장은 호출자 책임입니다. 이 함수는 입력 바이트를 HTML 문자열로만 변환합니다.
    return render_html(parse_law_xml(raw))


def main() -> None:
    # 명령행 사용을 위한 입출력 래퍼입니다. 모듈로 import하면 자동 실행되지 않습니다.
    parser = argparse.ArgumentParser(description='법령 본문 XML을 조문·별표 링크가 있는 HTML로 변환')
    parser.add_argument('xml', type=Path, help='법령 본문 API에서 받은 XML 파일')
    parser.add_argument('-o', '--output', type=Path, help='출력 HTML 경로 (기본: 입력 파일명.html)')
    args = parser.parse_args()
    output = args.output or args.xml.with_suffix('.html')
    if args.xml.resolve() == output.resolve():
        # 실수로 -o에 입력 파일을 지정해 보관 XML을 덮어쓰는 것을 방지합니다.
        parser.error('출력 경로는 입력 XML과 달라야 합니다.')
    try:
        # 파싱/렌더링에 성공한 다음 저장합니다. 기존 출력 HTML이 있으면 덮어씁니다.
        page = xml_to_html(args.xml.read_bytes())
        output.write_text(page, encoding='utf-8')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'변환 실패: {exc}\n')
    print(f'HTML 생성 완료: {output}')


if __name__ == '__main__':
    main()

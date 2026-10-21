"""Law API adapter, article parser and safe, immutable HTML snapshots."""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from .references import Annex, parse_annexes, article_links, reference_target
from urllib.parse import quote
import httpx

class SetupError(ValueError):
    pass


def xml_root(raw: bytes):
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I):
        raise ValueError('DTD 및 외부 엔터티가 포함된 XML은 지원하지 않습니다.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('법령 API 응답이 유효한 XML이 아닙니다. OC와 API 이용 권한을 확인하세요.') from exc


def content_text(node):
    parts = [node.text or '']
    for child in node:
        if child.tag.lower() == 'br':
            parts.append('\n')
        else:
            parts.append(content_text(child))
        parts.append(child.tail or '')
    return ''.join(parts)


def text(node, path, default=''):
    item = node.find(path)
    return ''.join(item.itertext()).strip() if item is not None else default


@dataclass
class Article:
    number: str
    title: str
    body: str
    section_id: str
    chapter: str = ""


@dataclass
class Law:
    id: str
    name: str
    effective: str
    version: str
    articles: list[Article]
    source_url: str
    promulgated: str = ""
    kind: str = "law"
    annexes: list[Annex] = field(default_factory=list)


def parse_law(raw: bytes) -> Law:
    root = xml_root(raw)
    name = text(root, './/기본정보/법령명_한글')
    law_id = text(root, './/기본정보/법령ID')
    if not name or not law_id:
        raise ValueError('법령명/법령ID가 없습니다. law 본문 XML 응답인지 확인하세요.')
    articles = []
    headings = {}
    for node in root.findall('.//조문단위'):
        if text(node, '조문여부') == '전문':
            heading = text(node, '조문내용')
            match = re.match(r'제\s*\d+\s*([편장절관])', heading)
            if match:
                level = '편장절관'.index(match[1])
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = heading
            continue
        num = text(node, '조문번호')
        if not num:
            continue
        branch = text(node, '조문가지번호')
        number = f'제{int(num)}조' + (f'의{int(branch)}' if branch and int(branch) else '')
        title = text(node, '조문제목')
        # Content nodes only: preserve their whitespace and the paragraph hierarchy.
        parts = []
        for child in node.iter():
            if child.tag in {'조문내용', '항내용', '호내용', '목내용'}:
                value = content_text(child).strip()
                if value:
                    # API may encode line breaks as <br> inside text/CDATA.
                    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.I)
                    parts.append(value)
        body = '\n'.join(parts)
        if not body:
            continue
        section = f'{name}-{number}'
        if any(a.section_id == section for a in articles):
            raise ValueError(f'중복 조문: {section}')
        articles.append(Article(number, title, body, section, ' > '.join(headings[k] for k in sorted(headings))))
    if not articles:
        raise ValueError('수집할 조문이 없습니다.')
    return Law(law_id, name, text(root, './/기본정보/시행일자'),
               hashlib.sha256(raw).hexdigest()[:20], articles,
               'https://www.law.go.kr/법령/' + quote(name, safe=''), text(root, './/기본정보/공포일자'), annexes=parse_annexes(root, name))


def render_html(law: Law) -> str:
    esc = html.escape
    toc = ''.join(f'<a href="#{quote(a.section_id, safe="")}">{esc(a.number)} {esc(a.title)}</a>' for a in law.articles)
    def annotated_body(article):
        by_number = {a.number: a for a in law.articles}
        parts, cursor = [], 0
        for match in re.finditer(r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?', article.body):
            parts.append(esc(article.body[cursor:match.start()]))
            number = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
            target = by_number.get(number) if reference_target(article.body,match.start(),law.name)==law.name else None
            if target and target.title and number != article.number:
                label = esc(number + '(' + target.title + ')')
                parts.append(f'<a href="#{quote(target.section_id,safe="")}">{label}</a>')
            else:
                parts.append(esc(match[0]))
            cursor = match.end()
        parts.append(esc(article.body[cursor:]))
        return ''.join(parts)
    sections = ''.join(f'<section id="{esc(a.section_id, quote=True)}"><h2>{esc(a.number)} {esc(a.title)}</h2><pre>{annotated_body(a)}</pre></section>' for a in law.articles)
    for annex in law.annexes:
        body = esc(annex.body) if annex.body else '별표 본문이 확보되지 않았습니다. 원문 파일 추출이 필요합니다.'
        link = f'<a href="{esc(annex.source_url, quote=True)}" target="_blank" rel="noopener noreferrer">별표 원문 파일</a>' if annex.source_url else ''
        sections += f'<section id="{esc(annex.section_id, quote=True)}"><h2>{esc(annex.number)} {esc(annex.title)}</h2><pre>{body}</pre>{link}</section>'
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(law.name)}</title>
<style>body{{font:16px/1.8 system-ui;margin:32px;color:#203344}}nav a{{display:inline-block;margin:5px 12px}}section{{padding:20px;scroll-margin-top:16px;border-bottom:1px solid #ddd}}section:target{{background:#e4f3ff;border-left:4px solid #1687cc}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.9 monospace}}.notice{{background:#fff4d5;padding:14px}}</style>
<h1>{esc(law.name)}</h1><p>시행일: {esc(law.effective)} · <a href="{esc(law.source_url)}" target="_blank" rel="noopener noreferrer">법령정보센터 원문</a></p>
<p class="notice">API 조문 텍스트를 보존한 문서입니다. API에서 누락된 수식·이미지·별표 및 원래 페이지 배치는 복원되지 않습니다. 필요한 경우 공식 원문과 대조하세요.</p><nav>{toc}</nav>{sections}</html>'''


class LawAPI:
    def __init__(self, oc):
        self.oc = oc

    def request(self, endpoint, **params):
        if not self.oc:
            raise SetupError('.env의 LAW_API_OC에 국가법령정보 공동활용 OC를 입력하세요.')
        try:
            with httpx.Client(timeout=60) as client:
                response = client.get(f'https://www.law.go.kr/DRF/{endpoint}', params={'OC': self.oc, 'target': 'law', 'type': 'XML', **params})
                response.raise_for_status()
                return response.content
        except httpx.HTTPError:
            # Never expose the URL: query parameters contain OC.
            raise SetupError('법령 API 요청 실패: 네트워크, OC 및 공동활용 API 권한을 확인하세요.') from None

    def search(self, query, page=1, kind='law', revisions=False, display=30):
        params = {'query': query, 'display': display, 'page': page, 'target': kind}
        if kind == 'law' and revisions:
            params['nw'] = 3
        root = xml_root(self.request('lawSearch.do', **params))
        if kind == 'law':
            nodes = root.findall('.//law')
            keys = ('법령일련번호', '법령명한글', '공포일자')
        else:
            nodes = root.findall('.//admrul')
            keys = ('행정규칙일련번호', '행정규칙명', '발령일자')
        return {'total': text(root, './/totalCnt', '0'), 'page': page, 'laws': [
            {'id': text(n, keys[0]), 'name': text(n, keys[1]),
             'promulgated': text(n, keys[2]), 'effective': text(n, '시행일자'),
             'status': text(n, '현행연혁코드'), 'kind': kind}
            for n in nodes]}

    def fetch(self, mst, kind='law'):
        if kind == 'admrul':
            return self.request('lawService.do', target='admrul', ID=mst)
        return self.request('lawService.do', MST=mst)


def parse_admrul(raw: bytes) -> Law:
    """Read structured administrative-rule articles; never invent clause boundaries."""
    root = xml_root(raw)
    name = text(root, './/행정규칙명')
    rule_id = text(root, './/행정규칙ID') or text(root, './/행정규칙일련번호')
    if not name or not rule_id:
        raise ValueError('행정규칙명/식별자가 없는 응답입니다. admrul API 권한과 XML 형식을 확인하세요.')
    articles = []
    nodes = root.findall('.//조문단위') or root.findall('.//조문')
    for node in nodes:
        body = text(node, '조문내용')
        if not body:
            body = content_text(node).strip() if not list(node) else ''
        if not body or '<' in body:
            continue
        number = text(node, '조문번호')
        match = re.match(r'제\s*(\d+)\s*조(?:의\s*(\d+))?', body)
        if number.isdigit():
            label = f'제{int(number)}조'
            branch = text(node, '조문가지번호')
            if branch.isdigit() and int(branch):
                label += f'의{int(branch)}'
        elif match:
            label = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
        else:
            continue
        articles.append(Article(label, text(node, '조문제목'), body, f'{name}-{label}'))
    if not articles or len({a.number for a in articles}) != len(articles):
        raise ValueError('이 행정규칙 응답은 조 단위 구조를 확인할 수 없습니다. 원문 XML에 맞는 파서 확장이 필요합니다.')
    return Law('admrul:' + rule_id, name, text(root, './/시행일자'),
               hashlib.sha256(raw).hexdigest()[:20], articles,
               'https://www.law.go.kr/행정규칙/' + quote(name, safe=''), text(root, './/발령일자'), 'admrul', parse_annexes(root, name))

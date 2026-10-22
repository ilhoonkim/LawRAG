"""Law API adapter, article parser and safe, immutable HTML snapshots."""
import hashlib
import html
from pathlib import Path
import re
from time import monotonic
import threading
from .temporal import today_in_korea
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from .references import Annex, parse_annexes, article_links, reference_target
from urllib.parse import quote
import httpx

# 수집 진입부: LawAPI로 XML 수신 → parse_law/parse_admrul로 공통 Law 구조 변환
# → Store.ingest에서 저장·임베딩. 여기서는 외부 데이터를 해석하고 HTML을 구성합니다.
class SetupError(ValueError):
    pass


# XML은 외부 입력입니다. DTD/엔터티 확장은 허용하지 않으며 파싱 오류는
# OC가 들어갈 수 있는 요청 URL 대신 설정 확인용 메시지로 전달합니다.
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


# id는 법령 식별자, version은 수신한 원문 바이트의 SHA-256 앞 20자리입니다.
# 시행일·공포일은 API의 YYYYMMDD 형식을 유지하고 날짜 판단은 temporal/Store에서 합니다.
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
        # 편·장·절·관 표제는 검색 조문으로 만들지 않고 다음 조문의 상위 목차로 유지합니다.
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


VIEWER_CSS = Path(__file__).with_name("document_viewer.css").read_text(encoding="utf-8")
VIEWER_VERSION = '4'

# 숫자 있는 별표를 먼저 찾고, 독립적으로 쓰인 '별표', '[별표]', '별표와'도 찾습니다.
# '특별표', '별표준' 같은 단어 일부는 번호 없는 별표로 해석하지 않습니다.
HTML_REFERENCE_PATTERN = re.compile(
    r'제\s*(\d+)\s*조(?:\s*의\s*(\d+))?'
    r'|(?<![가-힣A-Za-z0-9_])별표(?:\s*(\d+)(?:\s*의\s*(\d+))?'
    r'|(?=$|[\s\]\)」】.,;:!?]|(?:와|과|에|의|를|는|은|가|로)(?=\s|$)))'
)


def reference_heading(number, title):
    # API의 0은 번호 없는 별표의 내부 키로 유지하되 화면에는 '별표'로 표시합니다.
    # 기존 DB 및 과거 출처의 '#법령명-별표0' 앵커를 바꾸지 않아 HTML만 갱신할 수 있습니다.
    display = '별표' if number == '별표 0' else number
    return display + (f'({title})' if title and title not in (number, display) else '')


def render_html(law: Law, *, reference_resolver=None, reference_date=None, delegation_links=None, reference_pending=False) -> str:
    esc = html.escape
    toc_parts, sections, target_styles = [], [], []
    last_chapter = ''
    def annotated_names(text):
        # 선언부의 「전체 법령명」도 문서 링크로 만듭니다. 조문 링크와 별도로 처리해
        # '(이하 "영"이라 한다) 제7조'의 조번호도 동일 대상의 앵커로 연결합니다.
        resolve_name = getattr(reference_resolver, 'law_name', None)
        if resolve_name is None:
            return esc(text)
        parts, cursor = [], 0
        for match in re.finditer(r'「([^」]+)」', text):
            parts.append(esc(text[cursor:match.start()]))
            target = resolve_name(match[1])
            parts.append(f'<a href="{esc(target["url"], quote=True)}">{esc(match[0])}</a>'
                         if target else esc(match[0]))
            cursor = match.end()
        parts.append(esc(text[cursor:]))
        return ''.join(parts)
    def heading(number, title):
        return reference_heading(number, title)
    def add_toc(node, index):
        label = esc(heading(node.number, node.title))
        toc_parts.append(f'<a data-section-index="{index}" href="#{quote(node.section_id, safe="")}">{label}</a>')
        # Numeric selectors keep law text out of CSS, while standalone HTML
        # still highlights the matching TOC entry without JavaScript.
        target_styles.append(f'body:has(.law-content section:nth-of-type({index}):target) .toc a[data-section-index="{index}"]' + '{background:#eaf0ff;color:#3f4b9c;font-weight:600}')
    # 본문의 '제N조', '별표 N'을 같은 수집 버전의 조문·별표와 매칭해 연결합니다.
    # 별표의 가지번호도 지원하며, 대상이 없거나 타 법령인 경우 원문 표현을 유지합니다.
    # 표시용 링크 생성과 LLM 문맥 확장(graph.py)은 별도 처리입니다.
    def annotated_body(article):
        by_number = {a.number: a for a in [*law.articles, *law.annexes]}
        parts, cursor = [], 0
        for match in HTML_REFERENCE_PATTERN.finditer(article.body):
            parts.append(annotated_names(article.body[cursor:match.start()]))
            if match[3]:
                number = f'별표 {int(match[3])}' + (f'의{int(match[4])}' if match[4] else '')
            elif match[1]:
                number = f'제{int(match[1])}조' + (f'의{int(match[2])}' if match[2] else '')
            else:
                # 번호 없는 참조는 API에서 번호 0으로 수록된 별표에만 연결합니다.
                # 별표 1만 있다고 해서 '별표'를 임의로 별표 1로 치환하지 않습니다.
                number = '별표 0'
            target = by_number.get(number) if reference_target(article.body,match.start(),law.name)==law.name else None
            if target and target.title and number != article.number:
                label = esc(reference_heading(number, target.title))
                parts.append(f'<a href="#{quote(target.section_id,safe="")}">{label}</a>')
            else:
                external = reference_resolver(article, match, number) if reference_resolver else None
                if external:
                    label = esc(reference_heading(number, external['title']))
                    parts.append(f'<a href="{esc(external["url"], quote=True)}" title="{esc(external["name"], quote=True)}">{label}</a>')
                else:
                    parts.append(esc(match[0]))
            cursor = match.end()
        parts.append(annotated_names(article.body[cursor:]))
        return ''.join(parts)
    for index, article in enumerate(law.articles, 1):
        if article.chapter and article.chapter != last_chapter:
            toc_parts.append(f'<p class="chapter-label">{esc(article.chapter)}</p>')
            sections.append(f'<h3 class="chapter-heading">{esc(article.chapter)}</h3>')
            last_chapter = article.chapter
        add_toc(article, index)
        related = (delegation_links or {}).get(article.number, [])
        connections = ''.join(f'<li>{esc(r["label"])}: <a href="{esc(r["url"], quote=True)}">'
            f'{esc(r["name"])} {esc(heading(r["number"], r["title"]))}</a></li>' for r in related)
        navigation = f'<aside class="delegation-links" aria-label="상하위 조문 연결"><strong>상하위 조문 연결</strong><ul>{connections}</ul></aside>' if connections else ''
        sections.append(f'<section id="{esc(article.section_id, quote=True)}"><h2>{esc(heading(article.number,article.title))}</h2><pre>{annotated_body(article)}</pre>{navigation}</section>')
    for index, annex in enumerate(law.annexes, len(law.articles)+1):
        add_toc(annex, index)
        body = annotated_body(annex) if annex.body else '별표 본문이 확보되지 않았습니다. 원문 파일 추출이 필요합니다.'
        link = f'<a href="{esc(annex.source_url, quote=True)}" target="_blank" rel="noopener noreferrer">별표 원문 파일</a>' if annex.source_url else ''
        sections.append(f'<section id="{esc(annex.section_id, quote=True)}"><h2>{esc(heading(annex.number,annex.title))}</h2><pre>{body}</pre>{link}</section>')
    def date_label(value):
        return f'{value[:4]}.{value[4:6]}.{value[6:]}' if re.fullmatch(r'\d{8}',value or '') else value or '미상'
    date_note = f'<span>참조 연결 기준일: {esc(reference_date.isoformat())}</span>' if reference_date else ''
    if reference_pending:
        date_note += '<span>상하위 문서 링크 준비 중 · 해당 세트의 HTML 링크 생성 완료 후 파일을 다시 열어 주세요.</span>'
    return f'''<!doctype html><html lang="ko" data-viewer-version="{VIEWER_VERSION}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(law.name)}</title>
<style>{VIEWER_CSS}{''.join(target_styles)}</style></head><body>
<header class="document-header"><h1>{esc(law.name)}</h1><p class="document-meta">[시행 {esc(date_label(law.effective))}] [공포·발령 {esc(date_label(law.promulgated))}]<a href="{esc(law.source_url,quote=True)}" target="_blank" rel="noopener noreferrer">법령정보센터 원문</a>{date_note}</p></header>
<div class="document-layout"><nav class="toc" aria-label="조문 목차"><h2 class="toc-title">조문 목차</h2>{''.join(toc_parts)}</nav>
<main class="law-content" aria-label="법령 본문">{''.join(sections)}<details class="source-note"><summary>자료 안내</summary><p>수집한 해당 버전의 API 텍스트입니다. 조문 줄바꿈과 참조 제목을 표시합니다. API에서 누락된 수식·이미지·별표 및 원래 페이지 배치는 복원되지 않습니다. 외부 원문 링크는 법령정보센터의 현재 화면으로 연결될 수 있으므로 시행일을 확인하세요.</p></details></main></div></body></html>'''



class LawAPI:
    def __init__(self, oc):
        self.oc = oc
        self._revision_cache = {}
        self._revision_lock = threading.Lock()

    def request(self, endpoint, **params):
        if not self.oc:
            raise SetupError('.env의 LAW_API_OC에 국가법령정보 공동활용 OC를 입력하세요.')
        try:
            from .http_retry import request_with_retry, RequestFailure
            with httpx.Client(timeout=60) as client:
                response = request_with_retry(lambda: client.get(f'https://www.law.go.kr/DRF/{endpoint}',
                    params={'OC': self.oc, 'target': 'law', 'type': 'XML', **params}), service='법령 API')
                response.raise_for_status()
                return response.content
        except RequestFailure as exc:
            raise SetupError(f'법령 API 요청 실패 [{exc.reason}]: 총 {exc.attempts}회 시도 후 실패했습니다.') from None
        except httpx.HTTPStatusError as exc:
            attempts = exc.response.extensions.get('request_attempts',1)
            raise SetupError(f'법령 API 요청 실패 [HTTP {exc.response.status_code}]: 총 {attempts}회 시도. '
                             'OC 및 공동활용 API 권한·서비스 상태를 확인하세요.') from None
        except httpx.HTTPError:
            # Never expose the URL: query parameters contain OC.
            raise SetupError('법령 API 요청 실패: 네트워크, OC 및 공동활용 API 권한을 확인하세요.') from None

    def delegation(self, mst):
        """3단비교 원본을 반환합니다. 요청 MST만으로 응답의 개정 버전을 단정하지 않습니다."""
        if not re.fullmatch(r'\d{1,20}', mst):
            raise ValueError('법령일련번호(MST)가 올바르지 않습니다.')
        return self.request('lawService.do', target='thdCmp', MST=mst, knd='2', type='JSON')

    def search(self, query, page=1, kind='law', revisions=False, display=30):
        if not revisions:
            return self._search_page(query, page, kind, display)
        # law is the promulgation-based search; eflaw provides effective-date
        # versions. nw=1,3 selects historical/current, excluding scheduled.
        # 법령 연혁은 시행일 기준 eflaw 결과를 모으고, 행정규칙은 admrul 결과를 모읍니다.
        # 모두 중복을 제거한 뒤 페이지를 나누어 반환합니다.
        # 현재 법령명 검색 결과 한 건을 개정 이력 전체로 오인하지 않도록 경로를 분리합니다.
        key = (query, kind)
        with self._revision_lock:
            cached = self._revision_cache.get(key)
            if cached and monotonic() - cached[0] < 60:
                rows = cached[1]
            else:
                merged = {}
                for scope in (("1,3",) if kind == 'law' else (1, 2)):
                    seen = set()
                    for current_page in range(1, 201):
                        result = self._search_page(query, current_page, kind, 100, scope)
                        items = result['laws']
                        total = int(result['total'])
                        signature = tuple((r['id'],r['effective']) for r in items)
                        if (not items and (current_page - 1) * 100 < total) or (items and signature in seen):
                            raise ValueError('API 연혁 페이지가 누락되거나 반복되어 전체 버전을 확인하지 못했습니다.')
                        seen.add(signature)
                        for row in items:
                            merged[(row['id'],row['effective'])] = row
                        if current_page * 100 >= total:
                            break
                    else:
                        raise ValueError('연혁 조회가 200페이지를 초과했습니다. 법령명을 구체적으로 입력하세요.')
                cutoff = today_in_korea().strftime('%Y%m%d')
                eligible = [r for r in merged.values() if re.fullmatch(r'\d{8}',r['effective']) and r['effective'] <= cutoff and (not r['promulgated'] or r['promulgated'] <= cutoff)]
                rows = sorted(eligible, key=lambda r: (r['effective'], r['promulgated'], r['id']), reverse=True)
                # Keep a small, short-lived cache so UI paging does not re-fetch everything.
                if len(self._revision_cache) >= 32:
                    self._revision_cache.pop(next(iter(self._revision_cache)))
                self._revision_cache[key] = (monotonic(), rows)
        return {'total': len(rows), 'page': page, 'laws': rows[(page-1)*display:page*display]}

    def hierarchy(self, mst):
        """선택한 MST의 공식 상하위법 체계도. 연관 키워드 검색 결과와 구분합니다."""
        from .hierarchy import parse_hierarchy
        if not re.fullmatch(r'\d{1,20}', mst):
            raise ValueError('체계도 조회에 유효한 법령일련번호가 필요합니다.')
        return parse_hierarchy(self.request('lawService.do', target='lsStmd', MST=mst, type='JSON'))

    def _search_page(self, query, page, kind, display, scope=None):
        params = {'query': query, 'display': display, 'page': page, 'target': 'eflaw' if kind == 'law' else kind, 'search': 1}
        if scope is not None:
            params['nw'] = scope
        elif kind == 'law':
            params['nw'] = 3
        root = xml_root(self.request('lawSearch.do', **params))
        if kind == 'law':
            nodes = root.findall('.//law')
            keys = ('법령일련번호', '법령명한글', '공포일자')
        else:
            nodes = root.findall('.//admrul')
            keys = ('행정규칙일련번호', '행정규칙명', '발령일자')
        total = text(root, './/totalCnt')
        if not total.isdigit():
            raise SetupError('법령 API 검색 응답에 건수가 없습니다. OC·API 권한 및 응답 형식을 확인하세요.')
        return {'total': total, 'page': page, 'laws': [
            {'id': text(n, keys[0]), 'name': text(n, keys[1]),
             'promulgated': text(n, keys[2]), 'effective': text(n, '시행일자'),
             'status': text(n, '현행연혁코드'), 'kind': kind, 'law_id': text(n,'법령ID'),
            'amendment': text(n, '제개정구분명')}
            for n in nodes]}

    # 선택한 개정 버전을 재현하려면 MST와 시행일(efYd)이 함께 필요합니다.
    # 시행일이 없는 기존 호출은 law 본문 API를 사용하므로 호출자가 차이를 알아야 합니다.
    def fetch(self, mst, kind='law', effective=''):
        if kind == 'admrul':
            return self.request('lawService.do', target='admrul', ID=mst)
        if effective:
            if not re.fullmatch(r'\d{8}', effective):
                raise ValueError('본문 조회에 유효한 시행일(YYYYMMDD)이 필요합니다.')
            return self.request('lawService.do', target='eflaw', MST=mst, efYd=effective)
        return self.request('lawService.do', MST=mst)


def parse_admrul(raw: bytes) -> Law:
    """Read structured articles or source-text sections without inventing clauses."""
    root = xml_root(raw)
    name = text(root, './/행정규칙명')
    rule_id = text(root, './/행정규칙ID') or text(root, './/행정규칙일련번호')
    if not name or not rule_id:
        raise ValueError('행정규칙명/식별자가 없는 응답입니다. admrul API 권한과 XML 형식을 확인하세요.')
    from .admrul_parser import parse_units
    articles = [Article(unit['number'],unit['title'],unit['body'],
                        f"{name}-{unit['number']}",unit['chapter']) for unit in parse_units(root)]
    return Law('admrul:' + rule_id, name, text(root, './/시행일자'),
               hashlib.sha256(raw).hexdigest()[:20], articles,
               'https://www.law.go.kr/행정규칙/' + quote(name, safe=''), text(root, './/발령일자'), 'admrul', parse_annexes(root, name))

"""Conservative text extraction for structured and prose administrative rules."""
import html
from html.parser import HTMLParser
import re
import xml.etree.ElementTree as ET


class RuleText(HTMLParser):
    blocks = {'p','div','li','tr','table','h1','h2','h3','h4','section','article','pre'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def newline(self):
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def handle_starttag(self, tag, attrs):
        if tag in {'script','style'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in self.blocks or tag == 'br':
            self.newline()
        elif tag in {'td','th'} and self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\t')
        elif tag == 'img':
            alt = dict(attrs).get('alt','').strip()
            self.parts.append('[이미지: ' + (alt or '원문 확인 필요') + ']')

    def handle_endtag(self, tag):
        if tag in {'script','style'} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in self.blocks:
            self.newline()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def readable(node):
    # Serialize inner markup so <p>, <br>, table cells and CDATA HTML retain
    # boundaries. Never render or execute HTML returned by the external API.
    value = (node.text or '') + ''.join(ET.tostring(child, encoding='unicode') for child in node)
    parser = RuleText()
    parser.feed(html.unescape(value).replace('\r\n','\n').replace('\r','\n'))
    parser.close()
    return ''.join(parser.parts).strip()


def contents(node):
    tags = {'조문내용','항내용','호내용','목내용'}
    parts = []
    def visit(element):
        if element.tag in tags:
            parts.append(readable(element))
        else:
            for child in element:
                visit(child)
    visit(node)
    if parts:
        return '\n'.join(p for p in parts if p)
    if (node.text or '').strip() or any(child.tag in RuleText.blocks or child.tag in {'br','span','img'} for child in node):
        return readable(node)
    return ''


# A citation such as "제11조를 적용하지 않는다" is not a section heading.
HEADING = re.compile(r'^제\s*(?P<num>\d+(?:-\d+)?)\s*조(?:\s*의\s*(?P<branch>\d+))?'
                     r'\s*(?:[（(](?P<title>[^)）\n]+)[)）](?=$|\s|[①-⑳])|(?P<deleted>삭제)(?=$|\s|[<〔\[]))')
CHAPTER = re.compile(r'^제\s*\d+\s*([편장절관])(?:의\d+)?(?:\s+.*)?$')
ADDENDUM = re.compile(r'^부\s*칙(?:\s|$|[<〔\[(])')


def label(number, branch=''):
    number = '-'.join(str(int(n)) for n in number.split('-'))
    return f'제{number}조' + (f'의{int(branch)}' if branch and int(branch) else '')


def prose_chunks(body, title='행정규칙 본문'):
    # These are source-text locations, not fabricated legal article numbers.
    chunks = []
    while body:
        end = len(body)
        if end > 2000:
            end = body.rfind('\n', 0, 2000) + 1
            if end < 1000:
                end = 2000
        part, body = body[:end], body[end:]
        if part.strip():
            first = part.strip().splitlines()[0]
            heading = first if len(first) <= 120 and re.match(r'^(?:\d+|[가-힣])[.)]\s+\S',first) else title
            chunks.append(dict(number=None,title=heading,body=part,chapter=''))
    return chunks


def split_body(body):
    units, pending, current, chapters = [], [], None, {}
    supplemental = False
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if ADDENDUM.match(stripped):
            if current:
                units.append(current)
                current = None
            supplemental = True
        if supplemental:
            pending.append(line)
            continue
        chapter = CHAPTER.match(stripped)
        if chapter:
            if current:
                units.append(current)
                current = None
            level = '편장절관'.index(chapter[1])
            chapters = {k:v for k,v in chapters.items() if k < level}
            chapters[level] = stripped
            pending.append(line)
            continue
        match = HEADING.match(stripped)
        if match and re.match(r'\s*(?:및|에\s|의\s|을\s|를\s|부터)', stripped[match.end():]):
            match = None
        if match:
            if current:
                units.append(current)
            if pending:
                units.extend(prose_chunks(''.join(pending),'서문·편장절'))
                pending = []
            current = dict(number=label(match['num'],match['branch']),title=match['title'] or '삭제',
                           body=line,chapter=' > '.join(chapters[k] for k in sorted(chapters)))
        elif current:
            current['body'] += line
        else:
            pending.append(line)
    if current:
        units.append(current)
    if pending:
        units.extend(prose_chunks(''.join(pending),'부칙 본문' if supplemental else '행정규칙 본문'))
    return units


def parse_units(root):
    nodes = root.findall('.//조문단위') or root.findall('.//조문')
    units = []
    if nodes:
        for node in nodes:
            body = contents(node)
            if not body:
                continue
            number = (node.findtext('조문번호') or '').strip()
            branch = (node.findtext('조문가지번호') or '').strip()
            if re.fullmatch(r'\d+(?:-\d+)?',number) and (not branch or branch.isdigit()):
                heading = HEADING.match(body)
                title = (node.findtext('조문제목') or '').strip() or (heading['title'] if heading else '') or ''
                units.append(dict(number=label(number,branch),title=title,body=body,chapter=''))
            else:
                units.extend(split_body(body))
    else:
        for tag in ('조문내용','행정규칙내용','본문내용','본문'):
            nodes = root.findall('.//' + tag)
            if nodes:
                break
        units = split_body('\n'.join(readable(n) for n in nodes))
    combined = '\n'.join(unit['body'] for unit in units)
    if not re.sub(r'\[이미지:[^\]]*\]', '', combined).strip():
        raise ValueError('행정규칙에 추출 가능한 본문 텍스트가 없습니다. 이미지·첨부파일 형식이면 파일 추출 또는 OCR이 필요합니다.')
    for node in root.findall('.//부칙내용'):
        body = readable(node)
        if body and body not in combined:
            units.extend(prose_chunks(body,'부칙 본문'))
    numbers = [u['number'] for u in units if u['number']]
    if len(numbers) != len(set(numbers)):
        # Repeated article numbers can belong to multiple quoted rules/addenda.
        # Preserve all source text, without guessing which duplicate governs.
        combined = '\n'.join((u['number'] + '(' + u['title'] + ')\n' if u['number'] else '') + u['body'] for u in units)
        units = prose_chunks(combined)
    for i, unit in enumerate(units,1):
        if unit['number'] is None:
            unit['number'] = f'본문 구간 {i}'
    return units

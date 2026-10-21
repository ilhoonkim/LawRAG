"""Bounded, date-aware explicit references and reverse references over stored text."""
import re
from urllib.parse import quote
from .references import article_links, annex_references, reference_target
from .store import grams


def key(row):
    return row['version'], row['law_name'], row['number']


def canonical(name):
    return re.sub(r'\s+', '',name)


def aliases(text):
    names = {}
    for name,short in re.findall(r'「([^」]+)」\s*\(이하\s*["“「]([^"”」]+)["”」]\s*(?:이)?라\s*한다',text):
        names.setdefault(short,set()).add(name)
    return {short:next(iter(values)) if len(values)==1 else short+'(대상 확인 필요)' for short,values in names.items()}


def resolve(target, own, declared):
    target = target.replace('(대상 확인 필요)','')
    if target in declared:
        return declared[target]
    base = re.sub(r'\s*시행(?:령|규칙)$','',own)
    if base != own:
        return {'법':base,'영':base+' 시행령','시행령':base+' 시행령',
                '규칙':base+' 시행규칙','시행규칙':base+' 시행규칙'}.get(target,target)
    return target


def links(row, declared):
    result = []
    for relation, values in [('references_article',article_links(row['body'],row['law_name'],row['number'])),
                             ('references_annex',annex_references(row['body'],row['law_name']))]:
        for number,law in values:
            # Store a verbatim provenance excerpt, never a model-invented edge.
            digits = re.findall(r'\d+',number)
            pattern = (r'별표\s*' if number.startswith('별표') else r'제\s*') + digits[0]
            if not number.startswith('별표'):
                pattern += r'\s*조'
            if len(digits)>1:
                pattern += r'\s*의\s*' + digits[1]
            if len(digits)==1:
                pattern += r'(?!\s*의\s*\d)'
            same_target = lambda m: canonical(resolve(reference_target(row['body'],m.start(),row['law_name']),row['law_name'],declared)) == canonical(resolve(law,row['law_name'],declared))
            match = next((m for m in re.finditer(pattern + r'(?!\d)',row['body']) if same_target(m)),None)
            if match is None:
                pattern = r'(?:제|별표)\s*(\d+)\s*(?:조)?\s*(?:부터|내지|~|∼)\s*(?:제|별표)?\s*(\d+)'
                match = next((m for m in re.finditer(pattern,row['body']) if int(m[1]) <= int(digits[0]) <= int(m[2]) and same_target(m)),None)
            if match is None:
                continue
            position = match.start()
            excerpt = row['body'][max(0,position-90):position+180]
            kind = 'applies' if relation=='references_article' and '준용' in excerpt else relation
            result.append((number,resolve(law,row['law_name'],declared),kind,excerpt))
    return result


def expand(store, evidence, *, cutoff, question='', limit=40):
    if not evidence:
        return [], []
    versions = store.temporal_versions(cutoff)['versions']
    selected = {v['version']:v for v in versions}
    # Only eligible versions participate in cross-law/reverse traversal.
    nodes, annexes, documents = {}, {}, {}
    with store.connect() as db:
        for version, law in selected.items():
            rows = [dict(r) for r in db.execute('SELECT number,title,body,section_id,chapter FROM version_articles WHERE version=? ORDER BY position',(version,))]
            documents[version] = '\n'.join(r['body'] for r in rows)
            for r in rows:
                node = {**r,'version':version,'law_name':law['name'],'effective':law['effective'],
                        'law_id':law['law_id'],'source_url':law['source_url'],'id':version+':'+r['number'],
                        'html_url':'/documents/'+version+'.html#'+quote(r['section_id'],safe='')}
                nodes[key(node)] = node
            for r in db.execute('SELECT * FROM annex_nodes WHERE version=?',(version,)):
                node = {**dict(r),'law_name':law['name'],'effective':law['effective'],
                        'law_id':law['law_id'],'id':version+':'+r['number'],'kind':'annex',
                        'source_url':r['source_url'] or law['source_url'],
                        'html_url':'/documents/'+version+'.html#'+quote(r['section_id'],safe='')}
                annexes[key(node)] = node
    named = {}
    for v in versions:
        named.setdefault(canonical(v['name']),[]).append(v)
    declared = {version:aliases(text) for version,text in documents.items()}
    edges = {k:links(node,declared.get(node['version'],{})) for k,node in nodes.items()}
    expanded, missing, additions = [dict(e) for e in evidence], [], {}
    seen = {key(e) for e in evidence}
    def attach(node, parent, relation, excerpt):
        k = key(node)
        if k in seen:
            return
        src, dst = (node,parent) if relation=='reverse_reference' else (parent,node)
        edge = {'relation':relation,'from_version':src['version'],'from_law':src['law_name'],
                'from_article':src['number'],'to_version':dst['version'],'to_law':dst['law_name'],
                'to_article':dst['number'],'excerpt':excerpt,'explicit_citation':relation!='definition_context',
                'excerpt_version':node['version'] if relation in ('reverse_reference','definition_context') else parent['version']}
        if k not in additions:
            additions[k] = {**node,'score':parent.get('score',0), 'relation':relation,
                'referenced_by':parent['number'],'referenced_by_law':parent['law_name'],
                'parent_context':parent['body'],'reference_edges':[edge], 'reference_titles':[]}
        else:
            additions[k]['reference_edges'].append(edge)
    for article in expanded:
        article['reference_titles'] = []
        # Roots must not quietly switch to another version of the same law.
        root_edges = edges.get(key(article),links(article,declared.get(article['version'],{})))
        for number,law,relation,excerpt in root_edges:
            candidates = named.get(canonical(law),[])
            target_version = article['version'] if canonical(law)==canonical(article['law_name']) else (
                candidates[0]['version'] if len(candidates)==1 else None)
            node = (annexes if relation=='references_annex' else nodes).get((target_version,
                article['law_name'] if target_version==article['version'] else candidates[0]['name'] if len(candidates)==1 else law, number))
            if node is None or not node['body'].strip():
                missing.append({'law_name':law,'number':number,'from_article':article['number'],
                    'version':article['version'],'reason':'기준일에 해당하는 참조 본문이 없거나 대상 법령이 불명확합니다.','excerpt':excerpt})
                continue
            article['reference_titles'].append({'number':number,'title':node['title'],'law_name':node['law_name'],'version':node['version']})
            attach(node,article,relation,excerpt)
        for k,node_edges in edges.items():
            for number,law,_,excerpt in node_edges:
                if number==article['number'] and canonical(law)==canonical(article['law_name']):
                    # Reverse links must point to the version valid at the same cutoff.
                    if article['version'] not in selected:
                        continue
                    attach(nodes[k],article,'reverse_reference',excerpt)
                    break
        # Explicitly label this as contextual, not an inferred legal citation edge.
        for node in nodes.values():
            if node['version']==article['version'] and re.fullmatch(r'용어의\s*정의|정의',node['title'].strip()):
                if grams(node['body']) & grams(question + article['body']):
                    attach(node,article,'definition_context',node['body'][:180])
    ordered = sorted(additions.values(),key=lambda n:(-len(grams(question)&grams(n['body'])), -n['score'],key(n)))
    for node in ordered[limit:]:
        missing.append({'law_name':node['law_name'],'number':node['number'],'version':node['version'],
                        'reason':'참조 확장 개수 제한으로 이번 검토에서 제외되었습니다.'})
    return expanded+ordered[:limit], missing

"""Order complete evidence units: a reviewed article plus its cited annexes."""


def key(row):
    return row['version'], row['law_name'], row['number']


def is_linked_annex(row):
    return bool(row.get('relation')) and (
        row.get('relation') == 'references_annex' or row.get('kind') == 'annex')


def evidence_units(evidence):
    """Highest-priority units first; share an annex with its highest-ranked owner.

    Only explicit, version-matched references establish a dependency. A name or
    number match alone must not bind an annex to another law/version.
    """
    roots = sorted((r for r in evidence if not r.get('relation') or
                    (r.get('include_in_sources') and not is_linked_annex(r))),
                   key=lambda r:(bool(r.get('relation')),-r.get('score',0),key(r)))
    rank = {key(r):i for i,r in enumerate(roots)}
    units = [[r] for r in roots]
    attached = set(rank)
    for annex in sorted((r for r in evidence if is_linked_annex(r)),
                        key=lambda r:(-r.get('score',0),key(r))):
        owners = []
        edges = annex.get('reference_edges',[])
        for edge in edges:
            target = tuple(edge.get('to_'+field) for field in ('version','law','article'))
            owner = tuple(edge.get('from_'+field) for field in ('version','law','article'))
            if edge.get('relation') == 'references_annex' and target == key(annex) and owner in rank:
                owners.append(owner)
        if not edges and annex.get('referenced_by'):
            owner = (annex.get('referenced_by_version',annex['version']),
                     annex.get('referenced_by_law',annex['law_name']),annex['referenced_by'])
            if owner in rank:
                owners.append(owner)
        if owners:
            owner = min(owners,key=rank.get)
            units[rank[owner]].append(annex)
            attached.add(key(annex))
    ordinary = sorted((r for r in evidence if key(r) not in attached),
                      key=lambda r:(-r.get('score',0),key(r)))
    return units + [[r] for r in ordinary]

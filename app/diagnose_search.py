"""운영 DB에서 후보 누락 단계를 기록합니다. 수집 자료·벡터는 변경하지 않습니다.

python -m app.diagnose_search "질문" --law "법령명" --article 제58조
--chat을 붙이면 실제 재작성·LLM 검토·추가 검색·답변까지 실행해 판정을 기록합니다.
보고서는 API 키나 조문 본문 없이 점수/순위/판정과 자료 보유 상태를 저장합니다.
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path

from .chat_pipeline import run_chat, source_key, review_limit
from .config import Settings, SEARCH_FUSION, SEARCH_THRESHOLD
from .laws import SetupError
from .main import ChatRequest
from .provider import Provider
from .store import Store
from .temporal import resolve_date


STAGES = {
    'below_threshold': '임계치 미달', 'below_body_minimum': '본문 최소 일치 조건 미달',
    'outside_field_pool': '필드별 검색 범위 밖', 'outside_candidate_limit': '검토 후보 수 제한',
    'selected': '검색 후보 선정', 'llm_rejected': 'LLM 관련성 검토에서 제외',
    'visible_source': '최종 출처 포함', 'llm_context_only': 'LLM 참고 문맥에만 포함',
    'approved_but_omitted': '검토 통과 후 문맥 예산 또는 재선정에서 제외',
}


def diagnose(store, provider, settings, question, *, mode='hybrid', chat=False,
             law='', articles=(), as_of=None):
    if isinstance(as_of,str):
        as_of = date.fromisoformat(as_of)
    cutoff, clarification = resolve_date(question, as_of)
    if clarification:
        raise ValueError(clarification)
    def matches(row):
        return (not law or law.replace(' ', '') in row['law_name'].replace(' ', '')) and (
            not articles or row['number'] in articles)
    report = {'question':question, 'as_of':cutoff.isoformat(), 'mode':mode,
              'chat_executed':chat, 'fusion':SEARCH_FUSION, 'threshold':SEARCH_THRESHOLD,
              'created_at':datetime.now(timezone.utc).isoformat()}
    timeline = store.temporal_versions(cutoff)
    active_versions = {row['version'] for row in timeline['versions']}
    with store.connect() as db:
        inventory = [dict(row) for row in db.execute('''
            SELECT v.name AS law_name, v.version, v.effective, a.number,
                   length(a.body) AS body_chars, a.model,
                   a.vector IS NOT NULL AS has_body_vector,
                   a.title_vector IS NOT NULL AS has_title_vector
            FROM law_versions v JOIN version_articles a ON a.version=v.version
            ORDER BY v.name, v.effective, a.position''') if matches(row)]
    report['inventory'] = [{**row,'active_at_cutoff':row['version'] in active_versions} for row in inventory]
    report['missing_versions'] = [row for row in timeline['missing'] if not law or law in row['name']]
    if chat:
        trace = {}
        result = run_chat(store,provider,settings,ChatRequest(question=question,as_of=cutoff,mode=mode),diagnostics=trace)
        report.update(generated=result['generated'], rewritten_query=result.get('rewritten_query'),
                      assessment=result.get('assessment'), additional_queries=result.get('additional_queries'))
    else:
        search_trace = {}
        store.search_enhanced(question,provider,cutoff=cutoff,mode=mode,
                              threshold=SEARCH_THRESHOLD,fusion=SEARCH_FUSION,
                              limit=review_limit(settings),diagnostics=search_trace)
        trace = {'searches':[search_trace], 'review_outcomes':[]}
    outcomes = {source_key(row):row['stage'] for row in trace['review_outcomes']}
    for search in trace['searches']:
        candidates = search['candidates']
        search['total_scored'] = len(candidates)
        search['selected_count'] = sum(row['stage']=='selected' for row in candidates)
        search['candidates'] = [{**row,'chat_stage':outcomes.get(source_key(row)),
                                 'stage_description':STAGES.get(outcomes.get(source_key(row),row['stage']))}
                                for row in candidates if matches(row)]
    report.update(trace)
    report['review_outcomes'] = [row for row in trace['review_outcomes'] if matches(row)]
    report['review_decisions'] = [row for row in trace.get('review_decisions',[]) if matches(row)]
    report['inventory_note'] = ('자료가 없습니다. HTML 파일 존재 여부와 DB 수집 완료 여부는 다릅니다.'
                                if not inventory else 'active_at_cutoff=false인 버전은 이 기준일의 검색 대상이 아닙니다.')
    return report


def main():
    parser = argparse.ArgumentParser(description='법령 검색 누락 단계 진단 (수집/재임베딩 없음)')
    parser.add_argument('question')
    parser.add_argument('--law',default='',help='보고서에 남길 법령명 또는 일부 이름')
    parser.add_argument('--article',action='append',default=[],help='제58조 등; 여러 번 지정 가능')
    parser.add_argument('--mode',choices=['hybrid','keyword'],default='hybrid')
    parser.add_argument('--as-of',type=date.fromisoformat,help='검색 기준일 YYYY-MM-DD')
    parser.add_argument('--chat',action='store_true',help='실제 채팅 흐름을 실행하여 LLM 제외 여부까지 확인 (AI API 호출)')
    parser.add_argument('--output',type=Path,default=Path('search_diagnostics.json'))
    args = parser.parse_args()
    settings = Settings()
    try:
        report = diagnose(Store(settings.data_dir),Provider(settings),settings,args.question,
                          mode=args.mode,chat=args.chat,law=args.law,articles=args.article,as_of=args.as_of)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    except (ValueError,SetupError,OSError) as exc:
        parser.exit(1,str(exc)+'\n')
    for index, search in enumerate(report['searches'],1):
        print(f'검색 {index}: 전체 {search["total_scored"]}개 · 후보 선정 {search["selected_count"]}개')
        for row in search['candidates']:
            print(f'{row["law_name"]} {row["number"]}: {row["stage_description"]} · '
                  f'RRF 순위 {row["retrieval_rank"]} · 통과 점수 {row["admission_score"]:.3f} · '
                  f'본문 핵심어 {", ".join(row["matched_focus_nouns"])}')
    print(f'진단 보고서: {args.output.resolve()}')


if __name__ == '__main__':
    main()

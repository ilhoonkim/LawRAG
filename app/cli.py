"""python -m app.cli search-laws / ingest / search"""
import argparse
import json
from .config import Settings, SEARCH_THRESHOLD
from .laws import LawAPI, SetupError
from .temporal import resolve_date
from .provider import Provider
from .store import Store


# 간단한 수동 조회·수집·검색 진입점입니다. search는 후보 반환까지만 수행합니다.
# 최신 3개 일괄 수집은 preload_real_estate, 실제 채팅 흐름은 chat_pipeline을 참고하세요.
def main():
    parser = argparse.ArgumentParser(description='LawRAG 수집 및 검색')
    sub = parser.add_subparsers(dest='command', required=True)
    find = sub.add_parser('search-laws'); find.add_argument('query'); find.add_argument('--page', type=int, default=1)
    ingest = sub.add_parser('ingest'); ingest.add_argument('mst'); ingest.add_argument('--keyword-only', action='store_true')
    search = sub.add_parser('search'); search.add_argument('question'); search.add_argument('--mode', choices=['hybrid', 'keyword'], default='hybrid'); search.add_argument('--threshold', type=float)
    search.add_argument('--fusion',choices=['weighted','rrf'],default='weighted')
    search.add_argument('--compare',action='store_true',help='동일 후보에서 가중합과 RRF 결과를 비교')
    search.add_argument('--rewrite',help='비교용 검색문장. 원질의도 반드시 함께 검색합니다.')
    args = parser.parse_args()
    settings = Settings(); provider = Provider(settings); store = Store(settings.data_dir); api = LawAPI(settings.law_oc)
    try:
        if args.command == 'search-laws':
            result = api.search(args.query, args.page)
        elif args.command == 'ingest':
            if not args.mst.isdigit():
                parser.error('MST는 숫자여야 합니다.')
            result = store.ingest(api.fetch(args.mst), provider, args.keyword_only, mst=args.mst)
            from .delegation import refresh
            result['delegation'] = refresh(store, api, result['version'])
        else:
            cutoff, clarification = resolve_date(args.question)
            if clarification:
                parser.error(clarification)
            result = store.search_enhanced(args.question,provider,cutoff=cutoff,
                variants=[args.rewrite] if args.rewrite else [],
                threshold=SEARCH_THRESHOLD if args.threshold is None else args.threshold,
                mode=args.mode,fusion=args.fusion,compare=args.compare)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError,SetupError) as exc:
        parser.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()

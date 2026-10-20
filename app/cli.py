"""python -m app.cli search-laws / ingest / search"""
import argparse
import json
from .config import Settings
from .laws import LawAPI
from .provider import Provider
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='LawRAG 수집 및 검색')
    sub = parser.add_subparsers(dest='command', required=True)
    find = sub.add_parser('search-laws'); find.add_argument('query'); find.add_argument('--page', type=int, default=1)
    ingest = sub.add_parser('ingest'); ingest.add_argument('mst'); ingest.add_argument('--keyword-only', action='store_true')
    search = sub.add_parser('search'); search.add_argument('question'); search.add_argument('--mode', choices=['hybrid', 'keyword'], default='hybrid'); search.add_argument('--threshold', type=float)
    args = parser.parse_args()
    settings = Settings(); provider = Provider(settings); store = Store(settings.data_dir); api = LawAPI(settings.law_oc)
    try:
        if args.command == 'search-laws':
            result = api.search(args.query, args.page)
        elif args.command == 'ingest':
            if not args.mst.isdigit():
                parser.error('MST는 숫자여야 합니다.')
            result = store.ingest(api.fetch(args.mst), provider, args.keyword_only)
        else:
            result = store.search(args.question, provider, settings.threshold if args.threshold is None else args.threshold, mode=args.mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()

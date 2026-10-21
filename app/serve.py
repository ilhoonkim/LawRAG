"""Start the same app locally or on the LAN, with one collection worker."""
import argparse
import ipaddress
import socket

import uvicorn


def lan_addresses():
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(address for address in addresses
                  if not (ipaddress.ip_address(address).is_loopback
                          or ipaddress.ip_address(address).is_link_local
                          or ipaddress.ip_address(address).is_unspecified))


def main(argv=None):
    parser = argparse.ArgumentParser(description='LawRAG 실행 (기본: 내 PC, --lan: 내부망 접속 허용)')
    parser.add_argument('--lan', action='store_true', help='IPv4 네트워크 인터페이스에서 접속 수신')
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('포트는 1~65535 범위로 입력하세요.')
    print(f'내 PC 접속: http://127.0.0.1:{args.port}', flush=True)
    if args.lan:
        for address in lan_addresses():
            print(f'내부망 접속 주소 후보: http://{address}:{args.port}', flush=True)
        print('ipconfig의 회사망 IPv4 주소를 공유하세요. 방화벽에서 해당 포트의 내부망 접근을 허용해야 합니다.', flush=True)
        print('법령·수집 내역은 공유하고, 대화 이력은 접속한 브라우저별로 저장합니다.', flush=True)
    uvicorn.run('app.main:app', host='0.0.0.0' if args.lan else '127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()

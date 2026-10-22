"""임베딩 입력을 비우거나 잘라 버리지 않고 요청 크기를 제한합니다."""

# 기본 text-embedding-3 계열의 byte-BPE는 UTF-8 바이트 수보다 많은 토큰을
# 만들지 않습니다. 8,000바이트를 보수적인 상한으로 사용해 8,191토큰 한도 아래로
# 나눕니다. 실제 토큰 수가 아니며, 별도 토크나이저 다운로드가 필요하지 않습니다.
MAX_INPUT_BYTES = 8000
BATCH_SIZE = 16  # 한 요청도 최대 128,000바이트: 300,000토큰 합계 한도보다 작습니다.


def split_text(text, max_bytes=MAX_INPUT_BYTES):
    """모든 문자를 순서대로 보존하며 가능하면 문단/공백 경계에서 나눕니다."""
    if max_bytes < 4:
        raise ValueError('UTF-8 문자 한 개를 담을 수 있도록 입력 한도는 4바이트 이상이어야 합니다.')
    encoded = text.encode('utf-8')
    chunks, start = [], 0
    while start < len(encoded):
        end = min(start + max_bytes, len(encoded))
        if end < len(encoded):
            # 한글/이모지의 UTF-8 바이트 중간에서 자르지 않습니다.
            while encoded[end] & 0xC0 == 0x80:
                end -= 1
            for separator in (b'\n', b' '):
                boundary = encoded.rfind(separator, start + (end-start)//2, end)
                if boundary >= 0:
                    end = boundary + 1
                    break
        chunks.append(encoded[start:end].decode('utf-8'))
        start = end
    return chunks

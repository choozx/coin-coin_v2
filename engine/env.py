""".env 자동 로더 (stdlib만) — 로컬에서 `source .env` 없이 환경변수를 읽게 한다.

레포 루트의 .env(gitignore됨)를 파싱해 os.environ에 채운다. 이미 있는 환경변수는
덮어쓰지 않음 → 셸/도커컴포즈가 준 값이 항상 우선(배포 환경 존중).
KEY=VALUE 형식, # 주석·빈 줄 무시, 값의 양끝 따옴표 제거.
인라인 주석(`KEY=1  # 설명`)도 값에서 잘라낸다 — docker compose와 같은 규칙(공백 뒤 #).
안 자르면 `BINANCE_TESTNET=1  # 1=테스트넷`이 "1"이 아니게 되어 테스트넷 의도가
조용히 메인넷(실돈)으로 뒤집힌다.

BINANCE_API_KEY / BINANCE_API_SECRET 같은 비밀은 여기(파일)에만 두고 코드엔 안 박는다.
"""
from __future__ import annotations

import os


def _unquote(val: str) -> str:
    """값에서 따옴표를 벗기고, 따옴표 '밖의' 인라인 주석을 잘라낸다.

    docker compose 규칙과 맞춤: 주석은 공백 뒤의 #부터 (URL의 `...#frag` 같은 건 값의 일부).
    따옴표로 감쌌으면 그 안의 #은 값 — `PASS="a # b"` 는 그대로 보존.
    """
    val = val.strip()
    if val[:1] in ('"', "'"):
        quote = val[0]
        end = val.find(quote, 1)
        return val[1:end] if end != -1 else val[1:]
    for i, ch in enumerate(val):
        if ch == "#" and (i == 0 or val[i - 1] in " \t"):
            return val[:i].strip()
    return val


def load_dotenv(path: str = ".env") -> int:
    """.env를 읽어 os.environ에 채움(기존 값은 유지). 반환: 새로 설정한 키 수."""
    if not os.path.exists(path):
        return 0
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = _unquote(val)
                if key and key not in os.environ:      # 기존(셸/컴포즈) 값 우선
                    os.environ[key] = val
                    n += 1
    except Exception:
        pass                                            # .env 로드 실패가 실행을 막으면 안 됨
    return n

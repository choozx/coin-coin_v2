""".env 자동 로더 (stdlib만) — 로컬에서 `source .env` 없이 환경변수를 읽게 한다.

레포 루트의 .env(gitignore됨)를 파싱해 os.environ에 채운다. 이미 있는 환경변수는
덮어쓰지 않음 → 셸/도커컴포즈가 준 값이 항상 우선(배포 환경 존중).
KEY=VALUE 형식, # 주석·빈 줄 무시, 값의 양끝 따옴표 제거.

BINANCE_API_KEY / BINANCE_API_SECRET 같은 비밀은 여기(파일)에만 두고 코드엔 안 박는다.
"""
from __future__ import annotations

import os


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
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:      # 기존(셸/컴포즈) 값 우선
                    os.environ[key] = val
                    n += 1
    except Exception:
        pass                                            # .env 로드 실패가 실행을 막으면 안 됨
    return n

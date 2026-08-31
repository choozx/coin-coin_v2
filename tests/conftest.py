"""테스트 공용 격리 — 테스트가 프로젝트의 data/ 를 건드리지 않게.

진입 판정 로그(engine.entry_log)는 LiveTrader 가 폴링마다 append 한다. 테스트가 실제
data/entry_log.jsonl 에 쌓으면 개발자 로컬이 오염되고(실제로 4천 줄이 쌓였다) 테스트도 느려진다.
경로만 임시 디렉터리로 돌려서, 기록 경로 자체는 그대로 검증되게 둔다.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ENTRY_LOG_PATH", os.path.join(tempfile.mkdtemp(), "entry_log.jsonl"))

"""테스트 공용 격리 — 테스트가 프로젝트의 data/ 를 건드리지 않게.

관찰용 로그(engine.entry_log / engine.fill_log)는 봇이 폴링·체결마다 append 한다. 테스트가
실제 data/ 에 쌓으면 개발자 로컬이 오염되고(entry_log 는 4천 줄, fill_log 는 100줄이 실제로
쌓였다) 테스트도 느려진다. 경로만 임시 디렉터리로 돌려서, 기록 경로 자체는 그대로 검증되게 둔다.

★ 새 로그를 추가하면 여기에도 넣을 것. 두 번 다 잊어서 두 번 다 data/ 가 오염됐다.
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("ENTRY_LOG_PATH", os.path.join(_tmp, "entry_log.jsonl"))
os.environ.setdefault("FILL_LOG_PATH", os.path.join(_tmp, "fill_log.jsonl"))

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

# control/settings/state 도 여기서 격리한다. 이 경로들은 **모듈 로드 시점**에 확정돼 함수
# 기본인자로 묶이므로(나중에 상수를 바꿔도 안 먹는다) engine.* 이 임포트되기 전에 심어야 한다.
# 예전엔 test_backtest_live_parity.py 가 제 파일 맨 위에서 직접 심었는데, 그건 **그 파일이
# 제일 먼저 임포트될 때만** 통한다 — 알파벳으로 앞서는 테스트 파일이 하나 생기자 바로 깨졌다.
# conftest 는 어떤 테스트 모듈보다 먼저 로드되므로 여기 두면 순서가 무의미해진다.
os.environ.setdefault("CONTROL_PATH", os.path.join(_tmp, "control.json"))    # 없는 파일 → trader "running"
os.environ.setdefault("SETTINGS_PATH", os.path.join(_tmp, "settings.json"))  # 없는 파일 → 가드레일 기본(끔)
os.environ.setdefault("STATE_PATH", os.path.join(_tmp, "state.json"))

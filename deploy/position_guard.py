"""배포 가드 — "지금 트레이더가 포지션을 들고 있는가"에 답한다(pull-deploy.sh 가 호출).

종료코드: 0 = 연기하라(보유 또는 **모름**) · 1 = 진행하라(없음 또는 봇이 죽음)

왜 파일로 뺐나: 예전엔 bash 안의 한 줄짜리 heredoc 이었고 테스트가 없었다. 2026-09-02 에
50번 넘게 정상 연기하던 가드가 **딱 한 번** 통과해 포지션 보유 중인 트레이더가 교체됐고,
손절/익절을 잃었다. 원인은 그 시각 트레이더 로그가 지워져 못 밝혔지만, 어떤 경로였든
공통점은 하나다 — **가드가 '모른다'를 '없다'로 해석했다.**

원칙: 모르면 연기한다. 단 영원히는 아니다 — 봇이 죽어 상태가 갱신되지 않으면 지킬 포지션도
없으므로 일정 시간 뒤엔 배포를 통과시킨다(안 그러면 죽은 봇이 제 고침을 영영 막는다).
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

STALE_MS = 300_000        # 5분. 폴링이 60초이므로 이보다 낡았으면 '지금'을 모르는 상태다.
DEAD_MS = 3_600_000       # 1시간. 이쯤 갱신이 없으면 봇이 죽은 것 → 배포를 막지 않는다.


def decide(state, now_ms: float):
    """(연기할 것인가, 사유). state=None 은 '못 읽었다'."""
    if state is None:
        return True, "상태를 못 읽음 — 모르는 채로 교체하지 않는다"
    try:
        age = now_ms - float(state.get("updatedAt") or 0)
    except (TypeError, ValueError):
        return True, "updatedAt 이 깨짐 — 모르는 채로 교체하지 않는다"
    if age > DEAD_MS:
        return False, f"상태 갱신이 {int(age / 60_000)}분째 없음 — 봇이 죽은 것으로 보고 진행"
    if age > STALE_MS:
        return True, f"상태가 {int(age / 1000)}초 낡음 — 지금 포지션을 모른다"
    if state.get("position"):
        return True, "포지션 보유 중"
    return False, "무포지션 확인"


def main(path: str = "data/state.json") -> int:
    try:
        state = json.loads(pathlib.Path(path).read_text())
    except Exception:
        state = None
    defer, why = decide(state, time.time() * 1000)
    print(why)
    return 0 if defer else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data/state.json"))

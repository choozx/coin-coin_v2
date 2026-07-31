"""워치독(데드맨 스위치) — 트레이더가 조용히 멈추면 알려준다.

트레이더가 죽으면 알림도 같이 죽어서 '침묵 = 정상'처럼 보인다(실제로 이 사각지대로 며칠을
모르고 지나갔다). 이 프로세스는 **트레이더와 별개**로 돌면서 trader 가 남기는 state.json 의
updatedAt 이 오래 안 갱신되면 웹훅으로 경고한다 — 트레이더가 죽어도 이건 살아 있으니 알림이 온다.

    python3 -m engine.watchdog

환경변수:
    STATE_PATH            감시할 상태 파일(기본 data/state.json)
    WATCHDOG_STALE_SEC    이 초 이상 무갱신이면 '멈춤'으로 판정(기본 600 = 10분, 폴 60초의 10배)
    WATCHDOG_INTERVAL_SEC 확인 주기(기본 120초)
    NOTIFY_WEBHOOK / DISCORD_BOT_TOKEN+CHANNEL  알림 경로(engine.notifier 와 동일)

가볍게(표준 라이브러리만) 유지한다 — 트레이더가 OOM 나는 저사양 인스턴스에 얹으므로.
"""
from __future__ import annotations

import json
import os
import time

from .notifier import notify


def read_updated_ms(state_path: str):
    """state.json 의 updatedAt(ms). 파일이 없으면 None, 못 읽으면 0(오래됨으로 취급)."""
    try:
        with open(state_path, encoding="utf-8") as f:
            return int(json.load(f).get("updatedAt") or 0)
    except FileNotFoundError:
        return None
    except Exception:
        return 0


def evaluate(updated_ms, now_ms: int, stale_sec: int):
    """상태 판정 → (status, age_sec). status: ok | stale | missing.

    updated_ms=None(파일 없음)→missing. 그 외엔 나이(now-updated)가 stale_sec 초과면 stale.
    """
    if updated_ms is None:
        return "missing", None
    age = max(0.0, (now_ms - int(updated_ms)) / 1000.0)
    return ("stale" if age > stale_sec else "ok"), age


def alert_for(prev, cur, age_sec):
    """상태 전이 → 보낼 알림 메시지(또는 None=알림 불필요).

    처음(prev=None)에 이미 stale/missing 이면 바로 알린다 — 워치독이 켜질 때 트레이더가 이미
    죽어 있는 경우(지금 상황)를 놓치지 않기 위함. ok 로 복구되면 회복 알림.
    """
    if cur == prev:
        return None
    if cur == "stale":
        mins = int((age_sec or 0) // 60)
        return (f"⚠️ 트레이더 응답 없음 — state.json 이 {mins}분째 갱신되지 않았습니다. "
                f"봇이 멈춘 것으로 보입니다(크래시/OOM/정지 확인).")
    if cur == "missing":
        return "⚠️ 트레이더 상태 파일(state.json) 없음 — 봇이 한 번도 안 돌았거나 파일이 유실됐습니다."
    if cur == "ok" and prev in ("stale", "missing"):
        return "✅ 트레이더 복구됨 — 상태 갱신이 재개됐습니다."
    return None                                   # None→ok(정상 기동)은 조용히


def startup_text(status, age_sec, stale_sec: int) -> str:
    """워치독 기동 시 보낼 '현재 트레이더 상태' 한 줄. 배포되는 순간 봇 생사를 바로 알린다."""
    mins = int((age_sec or 0) // 60)
    cur = {"ok": f"정상 (마지막 갱신 {mins}분 전)",
           "stale": f"⚠️ 응답 없음 ({mins}분째 무갱신)",
           "missing": "⚠️ 상태 파일 없음"}.get(status, status)
    return f"🐕 워치독 시작 — 현재 트레이더 {cur}. {stale_sec // 60}분 이상 무갱신이면 경고합니다."


def run() -> None:
    state_path = os.environ.get("STATE_PATH", "data/state.json")
    stale_sec = int(os.environ.get("WATCHDOG_STALE_SEC", "600"))
    interval = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "120"))
    print(f"[워치독] 시작 — {state_path} 를 {interval}초마다 확인, {stale_sec // 60}분 무갱신 시 경고", flush=True)
    # 기동 즉시 현재 상태를 한 번 보고(배포 확인 + 지금 봇이 살았는지). 이후엔 '전이'에만 알림.
    status, age = evaluate(read_updated_ms(state_path), int(time.time() * 1000), stale_sec)
    notify(startup_text(status, age, stale_sec), category="system")
    print(f"[워치독] 기동 보고: {status} (age={age})", flush=True)
    prev = status
    while True:
        time.sleep(interval)
        status, age = evaluate(read_updated_ms(state_path), int(time.time() * 1000), stale_sec)
        msg = alert_for(prev, status, age)
        if msg:
            # 응답없음/파일없음 경고엔 [상태] 버튼(회복 알림엔 불필요)
            btns = ["status"] if status in ("stale", "missing") else None
            notify(msg, category="system", buttons=btns)
            print(f"[워치독] {status}: {msg}", flush=True)
        prev = status


def main() -> None:
    from .env import load_dotenv
    load_dotenv()
    run()


if __name__ == "__main__":
    main()

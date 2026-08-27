"""워치독(데드맨 스위치) — 트레이더·수집기가 조용히 멈추면 알려준다.

트레이더가 죽으면 알림도 같이 죽어서 '침묵 = 정상'처럼 보인다(실제로 이 사각지대로 며칠을
모르고 지나갔다). 이 프로세스는 **트레이더와 별개**로 돌면서 trader 가 남기는 state.json 의
updatedAt 이 오래 안 갱신되면 웹훅으로 경고한다 — 트레이더가 죽어도 이건 살아 있으니 알림이 온다.

    python3 -m engine.watchdog

수집기도 같은 이유로 감시한다: 수집기는 state.json 을 안 남기고 프로세스가 떠 있어도 봉이
안 쌓이면 죽은 것과 같다 — 캐시의 마지막 봉이 얼마나 뒤처졌는지로 판정한다(candle_health).
일부러 멈춰둔 경우(control.json 의 collector=paused)는 경고하지 않는다.

환경변수:
    STATE_PATH            감시할 상태 파일(기본 data/state.json)
    WATCHDOG_STALE_SEC    이 초 이상 무갱신이면 '멈춤'으로 판정(기본 600 = 10분, 폴 60초의 10배)
    WATCHDOG_INTERVAL_SEC 확인 주기(기본 120초)
    CANDLE_DB_PATH        감시할 캔들 캐시(기본 data/candles.db)
    COLLECT_STALE_MIN     마지막 봉이 이 분을 넘게 뒤처지면 '수집 멈춤'(기본 10)
    WATCH_COLLECTOR       0 이면 수집 감시 끔(기본 켬)
    CONTROL_PATH          수집기 멈춤 여부를 읽을 제어 파일
    NOTIFY_WEBHOOK / DISCORD_BOT_TOKEN+CHANNEL  알림 경로(engine.notifier 와 동일)

가볍게(표준 라이브러리만) 유지한다 — 트레이더가 OOM 나는 저사양 인스턴스에 얹으므로.
"""
from __future__ import annotations

import json
import os
import time

from . import candle_health
from . import control
from .notifier import notify, routing_summary


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


def collect_status(candle_db: str, stale_min: float, control_path: str):
    """수집 상태 한 번 판정 → (status, worst_row).

    수집기를 일부러 멈춰뒀으면 'paused' — 이 상태는 alert_for 의 어느 분기에도 안 걸려
    조용히 지나간다(내가 멈춘 걸 경고받을 이유가 없다).
    """
    if control.service_state("collector", control_path) == "paused":
        return "paused", None
    return candle_health.evaluate(candle_health.symbol_rows(candle_db), stale_min)


def run() -> None:
    state_path = os.environ.get("STATE_PATH", "data/state.json")
    stale_sec = int(os.environ.get("WATCHDOG_STALE_SEC", "600"))
    interval = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "120"))
    candle_db = os.environ.get("CANDLE_DB_PATH", candle_health.DEFAULT_DB)
    collect_stale_min = float(os.environ.get("COLLECT_STALE_MIN", "10"))
    control_path = os.environ.get("CONTROL_PATH", control.DEFAULT_PATH)
    watch_collect = os.environ.get("WATCH_COLLECTOR", "1") != "0"
    print(f"[워치독] 시작 — {state_path} 를 {interval}초마다 확인, {stale_sec // 60}분 무갱신 시 경고", flush=True)
    if watch_collect:
        print(f"[워치독] 수집 감시 — {candle_db}, 마지막 봉 {collect_stale_min:g}분 초과 시 경고", flush=True)
    print(f"[워치독] 알림 라우팅 — {routing_summary(('system',))}", flush=True)   # 워치독은 시스템 알림만 보낸다

    # 기동 즉시 현재 상태를 한 번 보고(배포 확인 + 지금 봇이 살았는지). 이후엔 '전이'에만 알림.
    status, age = evaluate(read_updated_ms(state_path), int(time.time() * 1000), stale_sec)
    lines, prev_collect = [startup_text(status, age, stale_sec)], None
    if watch_collect:
        prev_collect, cworst = collect_status(candle_db, collect_stale_min, control_path)
        lines.append(candle_health.startup_text(prev_collect, cworst, collect_stale_min))
    notify("\n".join(lines), category="system")
    print(f"[워치독] 기동 보고: 트레이더={status}(age={age}) 수집={prev_collect}", flush=True)

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

        if watch_collect:
            cstatus, cworst = collect_status(candle_db, collect_stale_min, control_path)
            cmsg = candle_health.alert_for(prev_collect, cstatus, cworst, collect_stale_min)
            if cmsg:
                notify(cmsg, category="system")
                print(f"[워치독] 수집 {cstatus}: {cmsg}", flush=True)
            prev_collect = cstatus


def main() -> None:
    from .env import load_dotenv
    load_dotenv()
    run()


if __name__ == "__main__":
    main()

"""캔들 수집 건강도 — candles.db 를 sqlite3 로 직접 읽는다. stdlib 만.

왜 candle_store 를 안 쓰나: 그쪽은 numpy 와 binance_data 까지 끌어온다. 워치독(mem_limit
80m)과 디스코드봇(200m)에 그걸 얹을 수는 없는데, 여기서 알고 싶은 건 '마지막 봉이 언제냐'
하나뿐이라 집계 쿼리 한 방이면 끝난다. 그래서 조회 전용으로 따로 뒀다.

수집기(engine.collector)는 트레이더의 state.json 같은 상태 파일을 남기지 않는다. 그래서
'수집이 살아 있나'는 캐시의 마지막 봉이 얼마나 뒤처졌는지로 판정하는 게 유일하고 가장
정직한 지표다 — 프로세스가 떠 있어도 실제로 봉이 안 쌓이면 죽은 것과 같다.
"""
from __future__ import annotations

import os
import sqlite3
import time

DEFAULT_DB = "data/candles.db"
MINUTE_MS = 60_000


def symbol_rows(db_path: str = None, now_ms: int = None) -> list:
    """심볼별 캐시 현황 → [{symbol, count, first_ms, last_ms, gap_min}] (심볼 오름차순).

    DB 가 없거나 못 읽으면 [] — 수집기가 아직 안 돌았거나 파일이 유실된 경우다(예외 대신
    빈 목록: 감시자가 조회 실패로 죽으면 감시 자체가 사라진다).
    """
    path = db_path or os.environ.get("CANDLE_DB_PATH") or DEFAULT_DB
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not os.path.exists(path):
        return []
    try:
        # 읽기 전용(mode=ro)으로 연다 — 수집기가 쓰는 DB 를 감시자가 잠그면 안 된다.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            "SELECT symbol, COUNT(*), MIN(open_time), MAX(open_time) FROM candle "
            "GROUP BY symbol ORDER BY symbol").fetchall()
        conn.close()
    except Exception:
        return []
    return [{"symbol": s, "count": c, "first_ms": mn, "last_ms": mx,
             "gap_min": None if mx is None else (now - mx) / MINUTE_MS}
            for s, c, mn, mx in rows]


def worst(rows: list):
    """가장 뒤처진 심볼 행 — 하나라도 밀리면 수집에 문제가 있는 것이므로 최악을 대표로 본다."""
    cand = [r for r in rows if r.get("gap_min") is not None]
    return max(cand, key=lambda r: r["gap_min"]) if cand else None


def evaluate(rows: list, stale_min: float):
    """수집 상태 판정 → (status, worst_row). status: ok | stale | empty.

    empty : 캐시에 심볼이 하나도 없음(수집기 미기동 / candles.db 유실)
    stale : 가장 뒤처진 심볼이 stale_min 분을 넘김
    """
    w = worst(rows)
    if w is None:
        return "empty", None
    return ("stale" if w["gap_min"] > stale_min else "ok"), w


def alert_for(prev, cur, worst_row, stale_min: float):
    """상태 전이 → 알림 메시지(또는 None=알림 불필요).

    트레이더 감시(watchdog.alert_for)와 같은 규칙: 전이에만 알린다. 같은 상태가 이어지는
    동안 2분마다 같은 경고를 쏘면 알림 자체를 안 보게 된다.
    'paused'(수집기를 일부러 멈춰둠)는 어느 분기에도 안 걸려 조용히 지나간다.
    """
    if cur == prev:
        return None
    if cur == "stale":
        row = worst_row or {}
        return (f"⚠️ 캔들 수집 멈춤 — {row.get('symbol', '?')} 마지막 봉이 "
                f"{int(row.get('gap_min') or 0)}분 전입니다(임계 {int(stale_min)}분). 수집기 확인 필요.")
    if cur == "empty":
        return "⚠️ 캔들 캐시 비어 있음 — 수집기가 한 번도 안 돌았거나 candles.db 가 유실됐습니다."
    if cur == "ok" and prev in ("stale", "empty"):
        return "✅ 캔들 수집 복구됨 — 캐시 갱신이 재개됐습니다."
    return None                                   # None→ok(정상 기동)·paused 는 조용히


def startup_text(status, worst_row, stale_min: float) -> str:
    """워치독 기동 시 붙이는 수집 상태 한 줄 — 배포 즉시 수집 생사도 같이 보고한다."""
    if status == "empty":
        return "📊 캔들 수집 — ⚠️ 캐시 비어 있음"
    row = worst_row or {}
    head = "정상" if status == "ok" else "⚠️ 멈춤"
    return (f"📊 캔들 수집 — {head} (가장 뒤처진 {row.get('symbol', '?')} "
            f"{int(row.get('gap_min') or 0)}분 전, 임계 {int(stale_min)}분)")

"""캔들 수집 감시 — 판정·전이 알림·표시 검증(네트워크·numpy 없음).

핵심: 수집기는 상태 파일을 안 남긴다. 프로세스가 떠 있어도 봉이 안 쌓이면 죽은 것과 같으므로
'마지막 봉이 얼마나 뒤처졌나'로만 판정한다. 알림은 전이 시에만(스팸 방지), 일부러 멈춰둔
동안(paused)은 조용해야 한다.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import candle_health as ch          # noqa: E402
from engine import discord_views as v           # noqa: E402

NOW = 1_700_000_000_000
MIN = 60_000


def _db(rows) -> str:
    """(symbol, open_time) 목록으로 임시 candles.db 생성 → 경로."""
    path = os.path.join(tempfile.mkdtemp(), "candles.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE candle(symbol TEXT, open_time INTEGER, open REAL, high REAL,"
                 " low REAL, close REAL, volume REAL, PRIMARY KEY(symbol, open_time))")
    conn.executemany("INSERT INTO candle VALUES (?,?,1,1,1,1,1)", rows)
    conn.commit()
    conn.close()
    return path


def test_symbol_rows_reports_gap():
    path = _db([("BTCUSDC", NOW - 3 * MIN), ("BTCUSDC", NOW - 2 * MIN),
                ("ETHUSDC", NOW - 40 * MIN)])
    rows = ch.symbol_rows(path, now_ms=NOW)
    assert [r["symbol"] for r in rows] == ["BTCUSDC", "ETHUSDC"]      # 심볼 오름차순
    assert rows[0]["count"] == 2 and rows[0]["gap_min"] == 2          # 마지막 봉 2분 전
    assert rows[1]["gap_min"] == 40


def test_missing_or_broken_db_is_empty_not_raise():
    """감시자가 조회 실패로 죽으면 감시 자체가 사라진다 → 예외 대신 빈 목록."""
    assert ch.symbol_rows("/nope/none.db", now_ms=NOW) == []
    broken = os.path.join(tempfile.mkdtemp(), "candles.db")
    with open(broken, "w") as f:
        f.write("not a database")
    assert ch.symbol_rows(broken, now_ms=NOW) == []
    empty = _db([])                                                   # 테이블은 있는데 행이 없음
    assert ch.symbol_rows(empty, now_ms=NOW) == []


def test_evaluate_uses_worst_symbol():
    """한 심볼만 밀려도 수집에 문제가 있는 것 → 최악을 대표로 본다."""
    path = _db([("BTCUSDC", NOW - 1 * MIN), ("ETHUSDC", NOW - 45 * MIN)])
    rows = ch.symbol_rows(path, now_ms=NOW)
    status, worst = ch.evaluate(rows, stale_min=10)
    assert status == "stale" and worst["symbol"] == "ETHUSDC"
    assert ch.evaluate(rows, stale_min=60)[0] == "ok"                 # 임계를 늘리면 ok
    assert ch.evaluate([], stale_min=10) == ("empty", None)


def test_alert_only_on_transition_and_silent_when_paused():
    worst = {"symbol": "BTCUSDC", "gap_min": 45}
    assert ch.alert_for("stale", "stale", worst, 10) is None          # 같은 상태 → 조용
    msg = ch.alert_for("ok", "stale", worst, 10)
    assert msg and "BTCUSDC" in msg and "45분" in msg
    assert "복구" in ch.alert_for("stale", "ok", None, 10)
    assert ch.alert_for(None, "ok", None, 10) is None                 # 정상 기동은 조용
    # 일부러 멈춰둔 동안은 어느 방향으로든 조용 — 내가 멈춘 걸 경고받을 이유가 없다.
    assert ch.alert_for("ok", "paused", None, 10) is None
    assert ch.alert_for("paused", "ok", None, 10) is None


def test_empty_cache_alert():
    assert "비어 있음" in ch.alert_for("ok", "empty", None, 10)


def test_startup_text():
    assert "정상" in ch.startup_text("ok", {"symbol": "BTCUSDC", "gap_min": 1}, 10)
    assert "⚠️ 멈춤" in ch.startup_text("stale", {"symbol": "BTCUSDC", "gap_min": 45}, 10)
    assert "비어 있음" in ch.startup_text("empty", None, 10)


def test_collect_text_marks_stale_symbols():
    rows = [{"symbol": "BTCUSDC", "count": 41832, "first_ms": NOW - 30 * 86400_000,
             "last_ms": NOW - MIN, "gap_min": 1},
            {"symbol": "ETHUSDC", "count": 1200, "first_ms": NOW - 86400_000,
             "last_ms": NOW - 45 * MIN, "gap_min": 45}]
    out = v.collect_text(rows, paused=False, db_bytes=8_400_000, stale_min=10)
    assert "수집중" in out and "8.4 MB" in out
    assert "41,832개" in out
    btc, eth = [l for l in out.split("\n") if "BTCUSDC" in l][0], [l for l in out.split("\n") if "ETHUSDC" in l][0]
    assert not btc.startswith("⚠️") and eth.startswith("⚠️")          # 임계 넘긴 심볼만 표시
    assert "멈춤" in v.collect_text([], paused=True) and "비어 있습니다" in v.collect_text([], paused=True)


if __name__ == "__main__":
    import traceback
    fns = [x for k, x in sorted(globals().items()) if k.startswith("test_") and callable(x)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

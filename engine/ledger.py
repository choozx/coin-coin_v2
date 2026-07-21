"""매매 원장 — 청산된 거래를 append-only로 영구 기록(data/trades.db).

캔들(candles.db)은 재다운로드 가능한 캐시지만, 매매기록은 잃으면 복구 불가한 원본이라
'절대 덮어쓰지 않는' 별도 SQLite 파일에 쌓는다. 봇이 청산할 때마다 INSERT.
재시작 시 원장을 읽어 잔고·이력을 복원 → 프로세스가 죽어도 기록·잔고가 안 사라진다.

- mode(paper/live)로 분리 저장 → 페이퍼와 실돈 기록이 절대 안 섞임.
- strategy 컬럼 → 전략 전환 기능과 맞물려 '어느 전략이 친 거래인지' 성과 비교 가능.
- 작아서(몇 KB~) 통째로 S3 등에 백업하기 쉬움(캔들 100MB와 분리한 이유).
"""
from __future__ import annotations

import os
import sqlite3

LEDGER_PATH = os.environ.get("LEDGER_PATH", "data/trades.db")

_COLS = ("ts", "mode", "symbol", "side", "entry_time", "entry_price", "exit_time",
         "exit_price", "qty", "leverage", "pnl", "fees", "funding", "reason",
         "strategy", "equity_after")


def _conn(path=LEDGER_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")       # 봇 쓰기 + 대시보드 읽기 동시 안전
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("""CREATE TABLE IF NOT EXISTS trade(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, mode TEXT, symbol TEXT, side INTEGER,
        entry_time INTEGER, entry_price REAL, exit_time INTEGER, exit_price REAL,
        qty REAL, leverage INTEGER, pnl REAL, fees REAL, funding REAL,
        reason TEXT, strategy TEXT, equity_after REAL)""")
    return c


def record(trade, symbol: str, strategy: str, mode: str, equity_after: float,
           db_path=LEDGER_PATH) -> int:
    """청산된 거래 한 건을 원장에 append. trade = executor.ClosedTrade. 반환: row id."""
    conn = _conn(db_path)
    row = (int(trade.exit_time), mode, symbol, int(trade.side),
           int(trade.entry_time), float(trade.entry_price), int(trade.exit_time),
           float(trade.exit_price), float(trade.qty), int(trade.leverage),
           float(trade.pnl), float(trade.fees), float(trade.funding),
           trade.reason, strategy, float(equity_after))
    cur = conn.execute(
        f"INSERT INTO trade({','.join(_COLS)}) VALUES({','.join('?' * len(_COLS))})", row)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def load(db_path=LEDGER_PATH, mode: str = None, strategy: str = None, limit: int = None) -> list:
    """원장 조회 → dict 리스트(오래된→최신, 삽입순). 대시보드/복원용."""
    q = f"SELECT id,{','.join(_COLS)} FROM trade"
    where, params = [], []
    if mode:
        where.append("mode=?"); params.append(mode)
    if strategy:
        where.append("strategy=?"); params.append(strategy)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    try:
        conn = _conn(db_path)
        rows = conn.execute(q, params).fetchall()
        conn.close()
    except Exception:
        return []
    keys = ("id",) + _COLS
    return [dict(zip(keys, r)) for r in rows]


def stats(db_path=LEDGER_PATH, mode: str = None) -> dict:
    """원장 집계 — 전체 + 전략별 성과(승률·손익비·MDD 등)."""
    rows = load(db_path, mode=mode)
    overall = _agg(rows)
    by_strat = {}
    for r in rows:
        by_strat.setdefault(r["strategy"] or "-", []).append(r)
    overall["byStrategy"] = [{"strategy": k, **_agg(v)} for k, v in by_strat.items()]
    return overall


def _agg(rows: list) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "wins": 0, "winRate": 0.0, "totalPnl": 0.0,
                "profitFactor": 0.0, "maxDrawdown": 0.0, "avgPnl": 0.0}
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = -sum(p for p in pnls if p < 0)
    total = sum(pnls)
    # MDD: 누적손익 곡선의 최고점 대비 최대 낙폭(절대금액)
    cum = peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return {
        "n": n, "wins": len(wins), "winRate": round(len(wins) / n * 100, 1),
        "totalPnl": round(total, 2), "avgPnl": round(total / n, 2),
        "profitFactor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "maxDrawdown": round(mdd, 2),
    }

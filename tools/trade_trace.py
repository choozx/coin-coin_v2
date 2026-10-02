"""한 구간의 원장·체결 로그를 시간순으로 펼친다 — 거래 하나가 어떻게 기록됐는지 추적용.

왜: 대조(fill_audit)는 '어긋났다'까지만 말한다. 왜 어긋났는지는 그 시각의 원장 행(수량 포함)과
체결 로그(기대 수량 vs 실제 수량)를 나란히 봐야 나온다. 실제로 테스트넷에서 포지션 하나가
원장에 두 건(같은 진입가, 다른 진입 시각)으로 남은 걸 쫓으려고 만들었다.

읽기 전용. 배포 없이 EC2 에서 돌리려면:
    git show origin/main:tools/trade_trace.py | docker compose exec -T trader \\
        python - --data /app/data --from "2026-09-21 00:00" --to "2026-09-23 00:00"
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3

UTC = datetime.timezone.utc


def _ms(s: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).replace(tzinfo=UTC).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"시각 형식 오류: {s!r} (예: '2026-09-21 00:00')")


def _t(ms) -> str:
    return datetime.datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="구간 원장·체결 로그 추적")
    ap.add_argument("--data", default="data")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--mode", default=None, help="원장 버킷 필터(paper|testnet|live), 기본 전체")
    a = ap.parse_args()
    lo, hi = _ms(a.start), _ms(a.end)

    print(f"=== 원장 — 진입 또는 청산이 {a.start} ~ {a.end} UTC 에 걸친 거래 ===")
    db = os.path.join(a.data, "trades.db")
    try:
        c = sqlite3.connect(db)
        q = ("SELECT id,mode,side,entry_time,entry_price,exit_time,exit_price,qty,leverage,"
             "pnl,fees,funding,reason FROM trade WHERE exit_time>=? AND entry_time<=?")
        args = [lo, hi]
        if a.mode:
            q += " AND mode=?"; args.append(a.mode)
        rows = c.execute(q + " ORDER BY id", args).fetchall()
    except sqlite3.Error as e:
        print(f"  읽기 실패: {e} (WAL 이라 읽기에도 쓰기 권한이 필요 — 컨테이너 안에서 돌릴 것)")
        rows = []
    for (i, mode, side, et, ep, xt, xp, qty, lev, pnl, fees, fnd, rsn) in rows:
        print(f"  #{i} {mode} {'롱' if side == 1 else '숏'} x{lev}  진입 {_t(et)} @{ep:.2f}  "
              f"청산 {_t(xt)} @{xp:.2f}  수량 {qty}  손익 {pnl:+.2f}  수수료 {fees:.4f}  "
              f"펀딩 {fnd or 0:+.4f}  {rsn}")
    if not rows:
        print("  (없음)")

    print(f"\n=== 체결 로그 — {a.start} ~ {a.end} UTC ===")
    path = os.path.join(a.data, "fill_log.jsonl")
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    x = json.loads(ln)
                except ValueError:
                    continue
                if not lo <= int(x.get("at") or 0) <= hi:
                    continue
                n += 1
                print(f"  {_t(x['at'])} {x.get('kind')}/{x.get('reason') or '-'} {x.get('side')}  "
                      f"기대 {x.get('expectedPrice')} x {x.get('expectedQty')}  →  "
                      f"체결 {x.get('price')} x {x.get('qty')}  "
                      f"(maker {x.get('makerQty')} / taker {x.get('takerQty')}, 주문 {x.get('orders')}건, "
                      f"fee {x.get('fee')})")
    except OSError as e:
        print(f"  읽기 실패: {e}")
    if not n:
        print("  (없음)")
    print("\n읽는 법: 같은 포지션의 진입 체결 수량 > 원장 수량이면 잔량이 거래소에 남았던 것이다.")


if __name__ == "__main__":
    main()

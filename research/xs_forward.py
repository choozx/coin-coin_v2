"""XS MOM 전진 기록(종이 매매) — 주문 없이, 고정 규칙대로 매주 롱·숏 목록을 미리 적고 다음 주에 결과를 확정한다.

규칙(exp_XS_cross_section.py 사전 고정과 같음 — 바꾸지 말 것):
  리밸런스  월요일 일봉 종가 = 화요일 00:00 UTC(한국 화 09:00). 그 종가로 체결 가정.
  대상      USDT 무기한 COIN 거래 중 · 상장 ≥ 30일 · 최근 30개 일봉 거래대금 중앙 ≥ $5M · 스테이블 제외.
  신호      지난 28일 수익률(월요일 종가 / 28일 전 종가 − 1). 상위 10% 롱 · 하위 10% 숏 · 동일 가중 · 총 노출 1.
  손익      가격 + 실제 펀딩(롱은 +펀딩을 냄) − 회전 × 0.10%. 폐지되면 마지막 종가.
기록은 결과를 보기 전에 남긴다(append-only JSONL) — 나중에 목록을 고칠 수 없다.

    python3 research/xs_forward.py --record     # 화요일 00:00 UTC 이후 1회(놓쳤으면 아무 때나 — 가장 최근 월요일 봉 기준)
    python3 research/xs_forward.py --report     # 주별 결과 · 누적 · 이번 주 미실현
  cron(EC2, UTC): 5 0 * * 2  cd ~/auto_trading && python3 research/xs_forward.py --record >> ~/xs_forward.log 2>&1
결과: data/xs_forward/ (못 쓰면 ~/xs_forward/) 의 xs_forward_book.jsonl. 키 불필요(공개 API 만).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 규칙(대상·신호·목록)은 engine.xs_mom 한 곳에 — 실매매 서비스(engine.xs_bot)와 같은 함수를 쓴다.
from engine.xs_mom import COST, D, _get, daily, last_monday_bar, signal  # noqa: E402

DIRS = (os.path.join(ROOT, "data", "xs_forward"), os.path.expanduser("~/xs_forward"))
BOOK = "xs_forward_book.jsonl"
EXPECT = (0.20, 0.40, 0.63)                     # 연 기대치: 보수 하한 · 보수 상한 · 백테스트


def _fmt(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")


def _book_path(write=False):
    for d in DIRS:
        p = os.path.join(d, BOOK)
        if os.path.exists(p):
            return p
    if not write:
        return None
    for d in DIRS:
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, BOOK)
            with open(p, "a"):
                pass
            return p
        except OSError:
            continue
    sys.exit("기록할 곳이 없어요(data/xs_forward, ~/xs_forward 둘 다 못 씀)")


def _read():
    p = _book_path()
    if not p:
        return []
    with open(p) as f:
        return [json.loads(x) for x in f if x.strip()]










def _exit_price(sym, t0, o):
    k = daily(sym, t0, o)
    if o in k:
        return k[o][0], False
    if k:
        return k[max(k)][0], True                  # 폐지·거래 중단 → 마지막 종가
    return None, True


def _funding(sym, a, b):
    rows = _get("fundingRate", {"symbol": sym, "startTime": a + 1, "endTime": b, "limit": 1000})
    time.sleep(0.65)                               # fundingRate 5분 500회 한도
    return sum(float(r["fundingRate"]) for r in rows)


def settle(prev, o):
    """prev 기록의 포지션을 월요일 봉 o 종가로 확정."""
    a, b = prev["t"] + D, o + D                    # 보유 구간(종가 시각 기준)
    legs, gone = {}, []
    for side, sgn in (("long", 1), ("short", -1)):
        rs = []
        for s, p0 in prev[side].items():
            p1, dead = _exit_price(s, prev["t"], o)
            if p1 is None:
                rs.append(0.0)
                gone.append(s)
                continue
            if dead:
                gone.append(s)
            rs.append(sgn * (p1 / p0 - 1 - _funding(s, a, b)))
        legs[side] = float(np.mean(rs)) if rs else 0.0
    return dict(gross=0.5 * legs["long"] + 0.5 * legs["short"], long=legs["long"], short=legs["short"],
                weeks=round((o - prev["t"]) / (7 * D), 2), delisted=gone)


def _weights(rec):
    w = {}
    if rec:
        for s in rec["long"]:
            w[s] = w.get(s, 0) + 0.5 / rec["k"]
        for s in rec["short"]:
            w[s] = w.get(s, 0) - 0.5 / rec["k"]
    return w


def record():
    book = _read()
    o = last_monday_bar(int(time.time() * 1000))
    if book and book[-1]["t"] >= o:
        print(f"이미 기록됨: 월요일 봉 {_fmt(book[-1]['t'])} (다음 리밸런스 {_fmt(o + 8 * D)} 00:00 UTC)")
        return
    print(f"리밸런스: 월요일 봉 {_fmt(o)} 종가(= {_fmt(o + D)} 00:00 UTC)", flush=True)
    prev = book[-1] if book else None
    res = settle(prev, o) if prev else None
    rec = signal(o)
    w0, w1 = _weights(prev), _weights(rec)
    turn = sum(abs(w1.get(s, 0) - w0.get(s, 0)) for s in set(w0) | set(w1))
    rec.update(turn=round(turn, 4), cost=round(turn * COST, 6), recorded=int(time.time() * 1000), prev_result=res)
    p = _book_path(write=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if res:
        net = res["gross"] - prev["cost"]
        print(f"지난주({_fmt(prev['t'])}~) 확정: 순 {net*100:+.2f}% (가격+펀딩 {res['gross']*100:+.2f} · 비용 {prev['cost']*100:.2f}) · "
              f"롱 {res['long']*100:+.2f} · 숏 {res['short']*100:+.2f}" + (f" · 폐지 {res['delisted']}" if res["delisted"] else ""))
    print(f"이번 주: 대상 {rec['n']}개 · 롱 {rec['k']} · 숏 {rec['k']} · 회전 {turn:.2f}")
    print("  롱: " + ", ".join(rec["long"]))
    print("  숏: " + ", ".join(rec["short"]))
    print(f"저장: {p}")


def report():
    book = _read()
    if not book:
        print("기록 없음 — --record 먼저")
        return
    rows = []
    for prev, cur in zip(book, book[1:]):
        r = cur["prev_result"]
        rows.append((prev["t"], r["gross"] - prev["cost"], r["long"], r["short"], prev["cost"], r["weeks"], r["delisted"]))
    print(f"XS MOM 전진 기록 · 시작 {_fmt(book[0]['t'])} 봉 · 확정 {len(rows)}주")
    eq = 1.0
    for t, net, lg, sh, cst, wk, dl in rows:
        eq *= 1 + net
        print(f"  {_fmt(t)}~ {wk:.0f}주 · 순 {net*100:+6.2f}% · 롱 {lg*100:+6.2f} · 숏 {sh*100:+6.2f} · 비용 {cst*100:.2f} · 누적 {eq*100-100:+6.1f}%"
              + (f" · 폐지 {','.join(dl)}" if dl else ""))
    if rows:
        v = np.array([x[1] for x in rows])
        weeks = sum(x[5] for x in rows)
        e = np.cumprod(1 + v)
        mdd = (e / np.maximum.accumulate(e) - 1).min()
        print(f"  주 평균 {v.mean()*100:+.2f}% · 연환산(단순) {v.sum()/weeks*52*100:+.1f}% · 이익 주 {np.mean(v>0):.0%} · 최대낙폭 {mdd*100:+.1f}%")
        print(f"  기대치(연): 보수 +{EXPECT[0]:.0%}~+{EXPECT[1]:.0%} · 백테스트 +{EXPECT[2]:.0%}"
              f" — {len(rows)}주로는 판정 불가(주 표준편차 ~5~6% → 26주는 지나야 대략적인 방향)")
    cur = book[-1]
    try:
        px = {x["symbol"]: float(x["price"]) for x in _get("ticker/price", {})}
        lg = np.mean([px[s] / p - 1 for s, p in cur["long"].items() if s in px])
        sh = np.mean([-(px[s] / p - 1) for s, p in cur["short"].items() if s in px])
        print(f"  이번 주({_fmt(cur['t'])} 봉~) 미실현(가격만): {0.5*lg*100+0.5*sh*100:+.2f}% · 롱 {lg*100:+.2f} · 숏 {sh*100:+.2f}")
    except Exception as e:                         # 시세 조회 실패해도 기록 요약은 보여 준다
        print(f"  이번 주 미실현: 시세 조회 실패 {e}")


def main():
    if "--record" in sys.argv:
        record()
    elif "--report" in sys.argv:
        report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

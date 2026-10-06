"""T4 — 4시간봉 SuperTrend 파라미터 탐색. **고르기는 BTC, 검증은 본 적 없는 코인 55개.**

요청(2026-10-06): "4시간봉에 가장 잘 맞는 파라미터를 찾아달라."
T 는 BTC 앞 두 구간에서 고르고 셋째 구간으로 검증했다 — 검증 표본이 '한 구간·30여 거래'뿐이라
약하고, 4h ST(14,2.5)+필터 재확인에서 보듯 그 구간을 본 뒤엔 오염된다. 그래서 검증을 **자산 축**으로 옮긴다.

규칙(결과 보기 전 고정):
  격자  기간(7·10·14·20·30) × 배수(1.5·2·2.5·3·4) × 필터(없음·HawkEye+QQE) × 방향(롱숏·롱만) = 100
  ① 고르기  BTCUSDT 6.9년 세 구간 각각 엔진 백테스트(taker·실펀딩·1배). 세 구간 모두 거래 ≥10 인 조합 중
            **min(구간1, 구간2, 구간3) 수익이 최대인 하나.** (한 구간 대박이 아니라 셋 다 버틴 것)
  ② 검증  그 하나를 **숫자 그대로** 알트 55개(1h→4h, 2021-01~2026-08)에. 각 코인 정밀 귀무(lib.precise_null).
            통과 = p95 초과 코인 **≥ 7/55**(우연 기대 2.75, P(X≥7)≈2%) **그리고** 백분위 중앙 ≥ 60.
            ※ 알트끼리 상관이 있어 이항 근사는 관대한 쪽이다 — 문턱을 더 낮출 이유는 없다.
  ③ 실패면 그게 결론. 상위 5개의 알트 성적은 참고로만 찍는다(그걸로 다시 고르지 않는다).

    python3 -u -m research.exp_T4_params
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                     # noqa: E402
from engine import null_model as nm                       # noqa: E402
from engine.candles import Candles                        # noqa: E402
from research import lib                                  # noqa: E402
from research.exp_K_live_preset import one_way_fee        # noqa: E402
from research.exp_T_supertrend_search import _ym, build, segments  # noqa: E402

PERIODS = (7, 10, 14, 20, 30)
MULTS = (1.5, 2.0, 2.5, 3.0, 4.0)
FILTS = (False, True)
SIDES = ("both", "long")
GRID = list(itertools.product(PERIODS, MULTS, FILTS, SIDES))
MIN_TRADES = 10
NEED_BEATS = 7
NEED_PCTL = 60.0

_W: dict = {}


def _init():
    _W["data"] = {i: lib.load("BTCUSDT", start_ms=a, end_ms=b) for i, (a, b) in enumerate(segments())}


def _run(args):
    c, i = args
    base, fs = _W["data"][i]
    m = lib.backtest(base, build("4h", *c), "BTCUSDT", funding_schedule=fs)
    return c, i, m.total_return_pct, m.num_trades


def label(c):
    p, mu, f, s = c
    return f"ST({p},{mu}) {'HawkEye+QQE' if f else '필터없음':11} {'롱숏' if s == 'both' else '롱만'}"


def alt_bases():
    conn = sqlite3.connect(cs.DB_PATH)
    syms = sorted(r[0][:-3] for r in conn.execute(
        "SELECT DISTINCT symbol FROM candle WHERE symbol LIKE '%\\_1H' ESCAPE '\\'") if r[0] != "BTCUSDT_1H")
    out = []
    for s in syms:
        d = np.array(conn.execute("SELECT open_time,open,high,low,close,volume,taker_buy FROM candle "
                                  "WHERE symbol=? ORDER BY open_time", (s + "_1H",)).fetchall(), dtype=float)
        # 엔진은 1분봉 베이스 전제다(마감 = open_time+1분 이 상위봉 경계, 체결 시각 = ot+1분 — 아니면
        # signal_close_index 가 멈춘다). 1h 봉을 **그 시간의 마지막 1분**으로 옮겨 1분봉으로 표현한다:
        # 버킷은 그대로고, 마감·체결 시각이 실제 봉 끝(정시)에 선다. 봉 안 경로는 1h 해상도로 거칠다.
        d[:, 0] += 59 * 60_000
        base = Candles(d[:, 0].astype(np.int64), d[:, 1], d[:, 2], d[:, 3], d[:, 4], d[:, 5],
                       timeframe_min=1, taker_buy=d[:, 6])
        try:
            fs = cs.funding_schedule(s, int(base.open_time[0]), int(base.open_time[-1])) or None
        except Exception:
            fs = None
        out.append((s, base, fs))
    return out


def on_alts(c, alts):
    rows = []
    for s, base, fs in alts:
        m = lib.backtest(base, build("4h", *c), s, funding_schedule=fs)
        if not m.num_trades:
            rows.append((s, 0.0, 0, 0.0, False))
            continue
        g = nm.gate(m.total_return_pct, lib.precise_null(m, base, 240, one_way_fee(m)))
        rows.append((s, m.total_return_pct, m.num_trades, g["percentile"], g["beatsNull"]))
    return rows


def main() -> int:
    segs = segments()
    workers = max(1, (os.cpu_count() or 2) - 1)
    print(f"T4 · 4h SuperTrend 파라미터 · 고르기 BTCUSDT 세 구간 · 검증 알트 55 · 격자 {len(GRID)} · 워커 {workers}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}")
    res = {}
    with ProcessPoolExecutor(workers, initializer=_init) as ex:
        for c, i, r, n in ex.map(_run, [(c, i) for c in GRID for i in (0, 1, 2)], chunksize=2):
            res[(c, i)] = (r, n)
    ok = [c for c in GRID if all(res[(c, i)][1] >= MIN_TRADES for i in (0, 1, 2))]
    ok.sort(key=lambda c: -min(res[(c, i)][0] for i in (0, 1, 2)))
    allpos = sum(all(res[(c, i)][0] > 0 for i in (0, 1, 2)) for c in ok)
    print(f"\n① 고르기 — 세 구간 모두 양수 {allpos}/{len(GRID)}. 상위 10 (min 기준):")
    for c in ok[:10]:
        r = [res[(c, i)] for i in (0, 1, 2)]
        print(f"  {label(c):34} " + " / ".join(f"{x[0]:+7.1f}% ({x[1]:2d})" for x in r)
              + f"   min {min(x[0] for x in r):+.1f}%")
    pick = ok[0]
    print(f"\n  ★ 선택: {label(pick)}")

    alts = alt_bases()
    print(f"\n② 검증 — 알트 {len(alts)}개, 숫자 그대로 (1h→4h, {_ym(int(alts[0][1].open_time[0]))}~)")
    rows = on_alts(pick, alts)
    for s, r, n, p, b in rows:
        print(f"  {s:12} {r:+8.1f}%  거래 {n:3d}  정밀귀무 백분위 {p:3.0f} {'✅' if b else ''}")
    beats = sum(x[4] for x in rows)
    pct = float(np.median([x[3] for x in rows]))
    rets = np.array([x[1] for x in rows])
    passed = beats >= NEED_BEATS and pct >= NEED_PCTL
    print(f"\n  p95 초과 {beats}/{len(rows)} (문턱 {NEED_BEATS}) · 백분위 중앙 {pct:.0f} (문턱 {NEED_PCTL:.0f}) · "
          f"수익 양수 {int((rets > 0).sum())}/{len(rows)} · 수익 중앙 {np.median(rets):+.1f}%")
    print(f"  판정: {'✅ 통과 — 다른 코인에서도 우연보다 낫다' if passed else '❌ 실패 — BTC 에서 고른 파라미터가 다른 코인에선 우연 수준'}")

    print("\n③ 참고(판정에 안 씀) — BTC 상위 2~5위의 알트 성적")
    for c in ok[1:5]:
        rr = on_alts(c, alts)
        print(f"  {label(c):34} p95 초과 {sum(x[4] for x in rr)}/{len(rr)} · 백분위 중앙 "
              f"{np.median([x[3] for x in rr]):.0f} · 수익 중앙 {np.median([x[1] for x in rr]):+.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

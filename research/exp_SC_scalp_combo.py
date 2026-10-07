"""SC — 스캘핑 조합: 사용자 동적 레버리지 + SuperTrend 뒤집힘 + RSI 필터 + 이동평균 필터 (5m·15m).

요청(2026-10-07): "동적 레버리지 + 슈퍼트렌드 + RSI 필터 + 이동평균선 필터로 최적의 스캘핑 전략. 15분봉, 짧게는 5분봉."
사전 예상(정직하게): 낮음. 레버리지는 엣지를 만들지 않고(수수료도 노출만큼 커진다), 15m 이하 SuperTrend 는 수수료 0 에서도
랜덤 미달(T), 필터는 거래당 bp 를 못 올렸다(E·R). 다만 이번엔 **사용자 설정 그대로**(라이브 프리셋 동적 레버리지·쿨다운·
펀딩창 회피, 엔진 백테스트, 강제청산 포함)이고 **다른 코인 1분봉(ETH·SOL)으로 검증**한다 — 지금까지 15m 이하는 BTC 안에서만 봤다.

격자(결과 전 고정) 2×3×3×3×2 = 108:
  TF 5m·15m · ST (10,3)·(14,2.5)·(20,4) · RSI 필터 none·mom(롱 RSI>50, 숏 <50)·noext(롱 RSI<70, 숏 >30)
  · 이평 필터 none·ema200·sma1d(하루치 SMA: 15m 96 / 5m 288 — 롱은 종가>MA, 숏은 <) · 방향 롱숏·롱만
  진입 = ST 뒤집힘 AND 필터, 청산 = ST 반대 뒤집힘(supertrendExit). 사이징·필터 = 라이브 프리셋 그대로.
판정(결과 전 고정):
  ① 고르기 — BTCUSDT 세 구간(엔진, taker·실펀딩·강제청산) 모두 거래 ≥30 인 조합 중 min(구간 수익) 최대 하나
  ② 검증 — 그 하나를 숫자 그대로 ETHUSDT·SOLUSDT 1분봉(2021-01~)에: **두 코인 모두 수익 > 0 이고 정밀 귀무 p95 초과**

    python3 -u -m research.exp_SC_scalp_combo
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                        # noqa: E402
from engine import null_model as nm                          # noqa: E402
from research import lib                                     # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402

ROOT = __file__.rsplit("/", 2)[0]
LIVE = json.load(open(os.path.join(ROOT, "presets/examples/live-strategy.json")))
LIVE = LIVE.get("preset", LIVE)
TFS = ("5m", "15m")
STS = ((10, 3.0), (14, 2.5), (20, 4.0))
RSIS = ("none", "mom", "noext")
MAS = ("none", "ema200", "sma1d")
SIDES = ("both", "long")
GRID = list(itertools.product(TFS, STS, RSIS, MAS, SIDES))
MIN_TRADES = 30
DAY_BARS = {"5m": 288, "15m": 96}
_W: dict = {}


def build(tf, st, rf, mf, side, symbol="BTCUSDT"):
    p, mu = st
    STD = {"indicator": "SUPERTREND_DIR", "period": p, "params": {"multiplier": mu}}
    RSI = {"indicator": "RSI", "period": 14}
    CLOSE = {"source": "close"}
    MA = ({"indicator": "EMA", "period": 200} if mf == "ema200" else
          {"indicator": "SMA", "period": DAY_BARS[tf]} if mf == "sma1d" else None)

    def rule(s):
        kids = [{"cross": "crossOver" if s > 0 else "crossUnder", "left": STD, "right": 0}]
        if rf == "mom":
            kids.append({"left": RSI, "cmp": ">" if s > 0 else "<", "right": 50})
        elif rf == "noext":
            kids.append({"left": RSI, "cmp": "<" if s > 0 else ">", "right": 70 if s > 0 else 30})
        if MA:
            kids.append({"left": CLOSE, "cmp": ">" if s > 0 else "<", "right": MA})
        return {"side": "long" if s > 0 else "short", "when": {"op": "AND", "children": kids}}

    rules = [rule(1)] + ([rule(-1)] if side == "both" else [])
    return {"schemaVersion": "1.0", "name": "SC",
            "market": {"exchange": "binance-futures", "symbol": symbol, "timeframe": tf, "direction": side},
            "entry": {"op": "OR", "children": [r["when"] for r in rules]}, "entryRules": rules,
            "exit": {"supertrendExit": {"period": p, "multiplier": mu, "as": "exit"}},
            "sizing": LIVE["sizing"], "filter": LIVE.get("filter", {}), "execution": LIVE.get("execution", {})}


def label(c):
    tf, st, rf, mf, side = c
    return f"{tf:>3} ST{st} RSI:{rf:5} 이평:{mf:6} {'롱숏' if side == 'both' else '롱만'}"


def _init():
    _W["data"] = {i: lib.load("BTCUSDT", start_ms=a, end_ms=b) for i, (a, b) in enumerate(segments())}


def _run(args):
    c, i = args
    base, fs = _W["data"][i]
    m = lib.backtest(base, build(*c), "BTCUSDT", funding_schedule=fs)
    return c, i, m.total_return_pct, m.num_trades, m.num_liquidations, m.max_drawdown_pct


def main() -> int:
    segs = segments()
    workers = max(1, (os.cpu_count() or 2) - 1)
    print(f"SC · 스캘핑 조합(동적 레버리지·ST·RSI·이평) · 고르기 BTCUSDT 세 구간 · 검증 ETH·SOL · 격자 {len(GRID)} · 워커 {workers}")
    print(f"  사이징(라이브 그대로): 증거금 {LIVE['sizing']['size']['value']}% · 티어 "
          + ", ".join(f"≤{t['maxBalance']:.0f}$ {t['leverage']}배" for t in LIVE['sizing']['leverageTiers']))
    res = {}
    with ProcessPoolExecutor(workers, initializer=_init) as ex:
        for c, i, r, n, liq, mdd in ex.map(_run, [(c, i) for c in GRID for i in (0, 1, 2)], chunksize=2):
            res[(c, i)] = (r, n, liq, mdd)
    for tf in TFS:
        sub = [c for c in GRID if c[0] == tf]
        print(f"\n  {tf}: 조합 중앙 " + " / ".join(f"{np.median([res[(c, i)][0] for c in sub]):+6.1f}%" for i in (0, 1, 2))
              + f" · 세 구간 모두 양수 {sum(all(res[(c, i)][0] > 0 for i in (0, 1, 2)) for c in sub)}/{len(sub)}"
              + f" · 강제청산 중앙 {np.median([res[(c, 0)][2] for c in sub]):.0f}건/구간")
        for name, pos, vals in (("RSI", 2, RSIS), ("이평", 3, MAS)):
            print(f"    {name} 필터별 조합 중앙: " + " · ".join(
                f"{v} " + "/".join(f"{np.median([res[(c, i)][0] for c in sub if c[pos] == v]):+.0f}" for i in (0, 1, 2))
                for v in vals))
    ok = [c for c in GRID if all(res[(c, i)][1] >= MIN_TRADES for i in (0, 1, 2))]
    ok.sort(key=lambda c: -min(res[(c, i)][0] for i in (0, 1, 2)))
    print(f"\n① 고르기 — 상위 8 (min 기준, 괄호 = 거래·강제청산·MDD)")
    for c in ok[:8]:
        print(f"  {label(c):46} " + " / ".join(
            f"{res[(c, i)][0]:+7.1f}% ({res[(c, i)][1]},{res[(c, i)][2]},{res[(c, i)][3]:.0f}%)" for i in (0, 1, 2)))
    pick = ok[0]
    print(f"  ★ 선택: {label(pick)}")

    print("\n② 검증 — 숫자 그대로 ETH·SOL (2021-01~)")
    passed = True
    for sym in ("ETHUSDT", "SOLUSDT"):
        st = cs.stats(sym)
        if not st["count"] or st["min"] > 1612137600000:          # 2021-02 이전부터 있어야
            print(f"  {sym}: 데이터 부족 {st} — 수집 먼저")
            passed = False
            continue
        base, fs = lib.load(sym, start_ms=max(st["min"], 1609459200000), end_ms=st["max"])
        m = lib.backtest(base, build(*pick, symbol=sym), sym, funding_schedule=fs)
        g = nm.judge(m, base, pick[0], 10000.0)
        bh = (base.close[-1] / base.close[0] - 1) * 100
        better = 2000 - round(g["percentile"] * 20) if g else None
        print(f"  {sym}: 수익 {m.total_return_pct:+.1f}% (보유 {bh:+.0f}%) · 거래 {m.num_trades} · 강제청산 {m.num_liquidations} · "
              f"MDD {m.max_drawdown_pct:.0f}% · 랜덤 2,000번 중 {better}번이 더 잘 범 {'✅' if g and g['beatsNull'] else '❌'}")
        passed &= bool(m.total_return_pct > 0 and g and g["beatsNull"])
    print(f"\n  판정: {'✅ 통과' if passed else '❌ 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

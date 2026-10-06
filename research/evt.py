"""이벤트 스터디 하네스 — '조건이 뜨면 진입 → 고정 보유 → 청산' 형 스캘핑 가설을 빠르게 훑는다.

왜 엔진(lib.backtest)이 아니라 이걸 쓰나.
  W(청산 연쇄 역추세)·X(시간대)·Y(스퀴즈 돌파)·Z(스윕) 는 진입 조건이 프리셋 스키마로
  표현이 안 되거나(시각·전일 고저·체결 쏠림) 격자 수백 개를 엔진으로 돌리면 맥 부하에서
  2시간 제한에 걸린다(T·U 에서 겪음). 그래서 1단계 '신호가 있는가'는 여기서 본다.

★ 회계는 engine/null_model.simulate 와 **같은 식**이다:
    per-trade = 1 + side·(close[i+h]/close[i] − 1) − 2·fee,   자본 100%·1배·복리
  전략과 귀무가 '언제 들어가나' 말고는 다르지 않게 하려는 것(README 판정 원칙 — 귀무는
  무엇을 제거하는가로 설계). 진입은 신호봉 **종가**, 청산은 h 봉 뒤 종가. 포지션은 하나
  (보유 중 뜬 신호는 버린다). 펀딩은 양쪽 다 무시(보유가 분~시간 단위라 작다).

판정(T·U 와 같은 사전 고정, 결과 보기 전에 정함):
  ① 후보 = 구간1·2 모두 수익 > 0 · 거래 ≥ MIN_TRADES · 각 구간 귀무 best-of-N p95 초과
     (+ 롱숏이 섞이면 '같은 시각·방향만 섞은' 귀무 p95 도 초과 — 변동성 정합 귀무)
  ② 후보 중 min(구간1, 구간2) 최대 하나 → ③ 홀드아웃(구간3) 1회: 수익 > 0 + 귀무 p95 초과
  ④ gross(수수료 0)에서 먼저 판정. 실패하면 net 은 볼 필요가 없다. 문턱을 낮추지 않는다.

귀무 두 개:
  · 시간 귀무 — 같은 횟수·보유·방향 구성으로 **아무 시각**에나 진입(engine/null_model).
  · 방향 귀무 — **같은 시각**에 들어가되 롱/숏 라벨만 섞는다. 이벤트는 변동성이 큰 시각에
    몰리는데 시간 귀무는 평온한 시각도 뽑아 분포가 좁아진다 → 거짓 양성 쪽으로 기운다.
    방향 귀무가 그 구멍을 막는다(롱만·숏만인 규칙엔 정의 안 됨 → 시간 귀무만).
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import binance_math as bm                     # noqa: E402
from engine import null_model as nm                       # noqa: E402
from engine.candles import TIMEFRAME_MINUTES, resample    # noqa: E402
from research import lib                                  # noqa: E402
from research.exp_T_supertrend_search import _ym, segments  # noqa: E402

SYMBOL = "BTCUSDT"
TAKER = bm.DEFAULT_TAKER_FEE
MIN_TRADES = 30
SAMPLES = 2000

_BASE: dict = {}
_BARS: dict = {}


def base(seg: int):
    if seg not in _BASE:
        a, b = segments()[seg]
        _BASE[seg] = lib.load(SYMBOL, start_ms=a, end_ms=b, with_funding=False)[0]
    return _BASE[seg]


def bars(tf: str, seg: int):
    """구간 seg 의 tf 봉. 캐시."""
    if (tf, seg) not in _BARS:
        _BARS[(tf, seg)] = resample(base(seg), TIMEFRAME_MINUTES[tf])
    return _BARS[(tf, seg)]


# ── 롤링(과거만) ───────────────────────────────────────────────────────────
def past_mean(x: np.ndarray, w: int) -> np.ndarray:
    """t 시점 값 = x[t-w:t] 평균(현재 봉 제외 = 룩어헤드 없음). 앞 w 개는 NaN."""
    cs = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    out = np.full(len(x), np.nan)
    out[w:] = (cs[w:-1] - cs[:-w - 1]) / w
    return out


def past_std(x: np.ndarray, w: int) -> np.ndarray:
    m = past_mean(x, w)
    m2 = past_mean(x * x, w)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def day_bars_of(b) -> int:
    """하루치 봉 수 — 롤링 창 기본값."""
    return 1440 // b.timeframe_min


# ── 트레이드 ───────────────────────────────────────────────────────────────
def pick(sig_idx: np.ndarray, sig_side: np.ndarray, hold: int, n: int):
    """신호 → 실제 진입(포지션 하나: 보유 중 신호는 버린다). 반환 (idx, side)."""
    order = np.argsort(sig_idx, kind="stable")
    sig_idx, sig_side = sig_idx[order], sig_side[order]
    keep = []
    nxt = -1
    for j, i in enumerate(sig_idx):
        if i >= nxt and i + hold < n:
            keep.append(j)
            nxt = i + hold
    keep = np.asarray(keep, dtype=np.int64)
    return sig_idx[keep], sig_side[keep]


def per_trade_raw(close: np.ndarray, idx: np.ndarray, hold: int) -> np.ndarray:
    return close[idx + hold] / close[idx] - 1.0


def total_return(raw: np.ndarray, side: np.ndarray, fee: float) -> float:
    pt = np.clip(1.0 + side * raw - 2 * fee, 0.0, None)
    return float((np.prod(pt) - 1.0) * 100.0)


class Result:
    __slots__ = ("ret", "n", "n_long", "hold", "tf", "raw", "side", "mean_bp")

    def __init__(self, tf, close, idx, side, hold, fee):
        self.tf, self.hold, self.side = tf, hold, side
        self.raw = per_trade_raw(close, idx, hold) if len(idx) else np.zeros(0)
        self.n = len(idx)
        self.n_long = int((side > 0).sum())
        self.ret = total_return(self.raw, side, fee) if self.n else 0.0
        self.mean_bp = float((side * self.raw).mean() * 1e4) if self.n else 0.0


def run(tf: str, seg: int, signal_fn, combo, hold: int, fee: float) -> Result:
    b = bars(tf, seg)
    sig_idx, sig_side = signal_fn(b, combo)
    idx, side = pick(np.asarray(sig_idx, dtype=np.int64), np.asarray(sig_side, dtype=np.float64),
                     hold, len(b.close))
    return Result(tf, b.close, idx, side, hold, fee)


# ── 귀무 ───────────────────────────────────────────────────────────────────
def time_null(r: Result, seg: int, fee: float, seed: int = 0) -> np.ndarray:
    """같은 횟수·보유·롱숏 구성, 아무 시각 진입. engine/null_model.simulate 를 그대로 쓴다."""
    out = None
    for n, s in ((r.n_long, "long"), (r.n - r.n_long, "short")):
        if n <= 0:
            continue
        d = 1.0 + nm.simulate(base(seg), TIMEFRAME_MINUTES[r.tf], n, r.hold, s, leverage=1,
                              size_fraction=1.0, samples=SAMPLES, seed=seed, taker_fee=fee) / 100.0
        out = d if out is None else out * d
    return (out - 1.0) * 100.0 if out is not None else np.zeros(1)


def side_null(r: Result, fee: float, seed: int = 0):
    """같은 시각, 방향 라벨만 섞기. 한쪽 방향뿐이면 None."""
    if r.n_long in (0, r.n):
        return None
    rng = np.random.default_rng(seed)
    sides = np.tile(r.side, (SAMPLES, 1))
    sides = rng.permuted(sides, axis=1)
    pt = np.clip(1.0 + sides * r.raw[None, :] - 2 * fee, 0.0, None)
    return (pt.prod(axis=1) - 1.0) * 100.0


def judge(r: Result, seg: int, fee: float, n_combos: int, min_bp: float = None) -> dict:
    g = nm.gate(r.ret, time_null(r, seg, fee), n_combos=n_combos)
    sn = side_null(r, fee)
    if sn is not None:
        sg = nm.gate(r.ret, sn, n_combos=n_combos)
        g["sideP95"] = sg["bestOfNP95"]
        g["beatsSide"] = sg["beatsBestOfN"]
    else:
        g["sideP95"] = None
        g["beatsSide"] = True
    g["ok"] = bool(g["beatsBestOfN"] and g["beatsSide"] and r.ret > 0 and r.n >= MIN_TRADES
                   and (min_bp is None or r.mean_bp >= min_bp))
    return g


# ── 탐색 → 선택 → 홀드아웃 ─────────────────────────────────────────────────
def search(title: str, grid: list, signal_fn, label_fn, show_top: int = 12, min_bp: float = None) -> dict:
    """grid 원소 = (tf, ..., hold) — 첫 칸이 tf, 마지막 칸이 보유 봉 수.

    min_bp — 경제적 문턱: 거래당 gross bp 가 이 값 이상이어야 후보·홀드아웃 통과(두 구간 모두).
    표본이 수만이면 0.2bp 도 '유의'하다(Y1·Z2) — 비용을 넘을 크기인지는 따로 물어야 한다.
    """
    segs = segments()
    print(f"{title} · {SYMBOL} · 격자 {len(grid)} · 귀무 best-of-{len(grid)}"
          + (f" · 거래당 문턱 {min_bp}bp" if min_bp is not None else ""))
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}{'  (홀드아웃 — 선택 후 1회)' if i == 2 else ''}")
    out = {}
    for stage, fee in (("gross(수수료 0)", 0.0), ("net(taker 왕복 10bp)", TAKER)):
        print(f"\n── {stage} " + "─" * 50, flush=True)
        res = {(c, i): run(c[0], i, signal_fn, c, c[-1], fee) for c in grid for i in (0, 1)}
        tfs = sorted({c[0] for c in grid}, key=lambda t: TIMEFRAME_MINUTES[t])
        for tf in tfs:
            sub = [c for c in grid if c[0] == tf and res[(c, 0)].n >= MIN_TRADES
                   and res[(c, 1)].n >= MIN_TRADES]
            if not sub:
                print(f"  {tf:>3}: 거래 {MIN_TRADES}건 이상 조합 없음")
                continue
            r0 = [res[(c, 0)].ret for c in sub]
            r1 = [res[(c, 1)].ret for c in sub]
            bp = [min(res[(c, 0)].mean_bp, res[(c, 1)].mean_bp) for c in sub]
            print(f"  {tf:>3}: 중앙 {np.median(r0):+8.1f}% / {np.median(r1):+8.1f}%  "
                  f"두 구간 양수 {sum(a > 0 and b > 0 for a, b in zip(r0, r1))}/{len(sub)}  "
                  f"거래당 최고(두 구간 중 낮은 쪽) {max(bp):+.1f}bp")
        cand = [c for c in grid if all(res[(c, i)].ret > 0 and res[(c, i)].n >= MIN_TRADES for i in (0, 1))]
        if min_bp is not None:
            nb = len(cand)
            cand = [c for c in cand if min(res[(c, 0)].mean_bp, res[(c, 1)].mean_bp) >= min_bp]
            print(f"  두 구간 양수 {nb} 중 거래당 ≥{min_bp}bp(두 구간 모두): {len(cand)}")
        cand.sort(key=lambda c: -min(res[(c, 0)].ret, res[(c, 1)].ret))
        bar = "·문턱" if min_bp is not None else ""
        print(f"  후보(두 구간 양수·거래 ≥{MIN_TRADES}{bar}): {len(cand)}/{len(grid)}")
        passed = []
        if cand:
            print(f"\n  {'조합':40} {'구간1':>8} {'거래':>5} {'bp':>6} {'구간2':>8} {'거래':>5} {'bp':>6}"
                  f"   시간귀무 best-of-N p95 · 방향귀무")
        for n, c in enumerate(cand[:60]):
            g = [judge(res[(c, i)], i, fee, len(grid), min_bp) for i in (0, 1)]
            ok = all(x["ok"] for x in g)
            if ok:
                passed.append(c)
            if n < show_top or ok:
                a, b = res[(c, 0)], res[(c, 1)]
                side = " / ".join("-" if x["sideP95"] is None else f"{x['sideP95']:+.0f}" for x in g)
                print(f"  {label_fn(c):40} {a.ret:+7.1f}% {a.n:5d} {a.mean_bp:+6.1f} {b.ret:+7.1f}% {b.n:5d} "
                      f"{b.mean_bp:+6.1f}   {g[0]['bestOfNP95']:+7.1f} / {g[1]['bestOfNP95']:+7.1f} · {side}"
                      f"  {'✅' if ok else '❌'}", flush=True)
        if not passed:
            print(f"\n  ★ {stage}: 사전 규칙 ①을 통과한 조합이 없다 → 홀드아웃을 열지 않는다.")
            out[stage] = None
            if fee == 0.0:
                print("  (gross 에서 신호가 없으면 net 은 더 볼 필요가 없지만 비교용으로 찍는다)")
            continue
        pk = passed[0]
        r3 = run(pk[0], 2, signal_fn, pk, pk[-1], fee)
        g3 = judge(r3, 2, fee, 1, min_bp)
        print(f"\n  ★ 선택: {label_fn(pk)} → 홀드아웃: {r3.ret:+.1f}% · 거래 {r3.n} · 거래당 {r3.mean_bp:+.1f}bp")
        print(f"    시간귀무 중앙 {g3['nullMedian']:+.1f}% · p95 {g3['nullP95']:+.1f}% · 백분위 "
              f"{g3['percentile']:.0f}" + (f" · 방향귀무 p95 {g3['sideP95']:+.1f}%" if g3['sideP95'] is not None else ""))
        print(f"    판정: {'✅ 통과' if g3['ok'] else '❌ 홀드아웃 실패'}")
        out[stage] = (pk, r3, g3)
    return out

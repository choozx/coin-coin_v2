"""펀딩 정산 전후 시장 상태 — "정산 직후 1초도 안 돼 유동성이 난리"를 숫자로 (2026-10-07, 사용자 관찰).

M(펀딩 횡단면)이 실제로 굴러가는가의 질문. M 은 정산 순간을 노리지 않고 며칠 들고 있으며 8시간마다
일부(~13%)만 교체한다 — 그래서 묻는 건 "교체를 언제 하면 비용이 평소 수준인가".

입력: research/mm_collect.py --depth 로 정산 시각(t0)을 끼고 받은 data/mm/<run>/.
구간(초, t0 기준): 평소[−1800,−300) · −5분 · −1분 · 직전5초 · 직후1초 · 1~10초 · 10~60초 · 1~5분 · 5~30분.
구간마다(심볼별 → 심볼 중앙):
  스프레드(bp)            bookTicker 이벤트 중앙
  1초 가격 변동(bp)       100ms 격자 중간값의 1초 변화 절댓값 평균
  체결 금액($/초)
  $1만 시장가 비용(bp)    depth10 스냅샷으로 걸어 본 매수·매도 평균(반스프레드 포함), 10단계로 모자라면 제외·집계
  10단계 호가($)
평소 대비 배수로도 낸다.

    python3 -u -m research.funding_window data/mm/<run> [t0 'YYYY-MM-DDTHH:MM']
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import sys

import numpy as np

from research.mm_markout import load_csv

WINDOWS = [("평소(−30~−5분)", -1800, -300), ("−5~−1분", -300, -60), ("−60~−5초", -60, -5),
           ("직전 5초", -5, 0), ("직후 0~1초", 0, 1), ("1~10초", 1, 10), ("10~60초", 10, 60),
           ("1~5분", 60, 300), ("5~30분", 300, 1800)]


def mid_grid(bt, bid, ask, t0, lo, hi, step_ms=100):
    g = np.arange(t0 + lo * 1000, t0 + hi * 1000, step_ms)
    j = np.searchsorted(bt, g, side="right") - 1
    ok = j >= 0
    m = np.full(len(g), np.nan)
    m[ok] = (bid[j[ok]] + ask[j[ok]]) / 2
    return g, m


def per_symbol(run, sym, t0):
    bk = load_csv(os.path.join(run, f"{sym}_book.csv"))
    tr = load_csv(os.path.join(run, f"{sym}_trade.csv"))
    dp_path = os.path.join(run, f"{sym}_depth.csv")
    dp = load_csv(dp_path) if os.path.exists(dp_path) else np.zeros((0, 7))
    if len(bk) < 100:
        return None
    bk = bk[np.argsort(bk[:, 0], kind="stable")]
    bt, bid, ask = bk[:, 0], bk[:, 1], bk[:, 3]
    spr = (ask - bid) / ((ask + bid) / 2) * 1e4
    out = {}
    for name, lo, hi in WINDOWS:
        a, b = t0 + lo * 1000, t0 + hi * 1000
        sel = (bt >= a) & (bt < b)
        # 구간 시작 시점의 상태도 포함(짧은 구간에서 갱신이 없으면 직전 값이 그 구간의 스프레드다)
        j0 = np.searchsorted(bt, a, side="right") - 1
        sp = np.concatenate(([spr[j0]] if j0 >= 0 else [], spr[sel]))
        g, m = mid_grid(bt, bid, ask, t0, lo, hi)
        k = max(1, int(round(1000 / 100)))
        vol = np.nanmean(np.abs(m[k:] / m[:-k] - 1)) * 1e4 if len(m) > k else np.nan
        if hi - lo <= 1:                                   # 1초 구간은 '그 1초 동안의 변화'
            g2, m2 = mid_grid(bt, bid, ask, t0, lo - 0.1, hi)
            vol = abs(m2[-1] / m2[0] - 1) * 1e4 if len(m2) > 1 and m2[0] == m2[0] else np.nan
        ts = (tr[:, 0] >= a) & (tr[:, 0] < b)
        usd_s = (tr[ts, 1] * tr[ts, 2]).sum() / (hi - lo)
        ds = (dp[:, 0] >= a - 500) & (dp[:, 0] < b) if len(dp) else np.zeros(0, bool)
        c10 = dp[ds][:, 1:3] if ds.any() else np.zeros((0, 2))
        short = int((c10 < 0).sum())
        c10 = c10[c10 >= 0]
        out[name] = dict(spread=float(np.median(sp)) if len(sp) else np.nan, vol=float(vol), usd_s=float(usd_s),
                         cost10k=float(np.median(c10)) if len(c10) else np.nan,
                         depth=float(np.median(dp[ds][:, 5:7].sum(axis=1))) if ds.any() else np.nan,
                         short=short)
    return out


def main() -> int:
    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning)     # 빈 구간의 nanmedian 경고
    run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("data/mm/*"))[-1]
    if len(sys.argv) > 2:
        t0 = int(dt.datetime.strptime(sys.argv[2], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    else:                                                  # 수집 구간 안의 첫 8시간 정산
        first = min(load_csv(f)[0, 0] for f in glob.glob(os.path.join(run, "*_book.csv")))
        t0 = int((first // 28_800_000 + 1) * 28_800_000)
    syms = sorted({os.path.basename(f)[:-9] for f in glob.glob(os.path.join(run, "*_book.csv"))})
    res = {s: r for s in syms if (r := per_symbol(run, s, t0))}
    print(f"펀딩 정산 전후 · t0 = {dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc):%Y-%m-%d %H:%M} UTC · {len(res)}심볼")
    print("  값 = 심볼 중앙 (괄호 = 평소 대비 배수).  1만$ 비용 = 시장가 매수·매도 평균(반스프레드 포함)\n")
    print(f"  {'구간':16} {'스프레드bp':>14} {'1초변동bp':>14} {'체결$/초':>16} {'1만$비용bp':>14} {'10단계호가$':>16} {'호가부족':>6}")
    base = {k: np.nanmedian([r["평소(−30~−5분)"][k] for r in res.values()]) for k in ("spread", "vol", "usd_s", "cost10k", "depth")}
    for name, _, _ in WINDOWS:
        v = {k: np.nanmedian([r[name][k] for r in res.values()]) for k in base}
        sh = sum(r[name]["short"] for r in res.values())
        cell = lambda k, f: f"{v[k]:{f}} ({v[k] / base[k]:4.1f}×)" if base[k] else f"{v[k]:{f}}"
        print(f"  {name:16} {cell('spread', '6.2f'):>14} {cell('vol', '6.2f'):>14} {cell('usd_s', '9,.0f'):>16} "
              f"{cell('cost10k', '6.2f'):>14} {cell('depth', '9,.0f'):>16} {sh:6d}")
    print("\n  심볼별 1만$ 비용(bp): 평소 → 직후 0~1초 → 1~10초 → 1~5분")
    for s, r in sorted(res.items(), key=lambda kv: -np.nan_to_num(kv[1]["직후 0~1초"]["cost10k"])):
        c = [r[w]["cost10k"] for w in ("평소(−30~−5분)", "직후 0~1초", "1~10초", "1~5분")]
        print(f"    {s:12} " + " → ".join("  -  " if np.isnan(x) else f"{x:5.2f}" for x in c))
    return 0


if __name__ == "__main__":
    sys.exit(main())

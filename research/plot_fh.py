"""FH 산점도 — x=|펀딩|, y=정산 분봉(T~T+1분)의 |종가-시가|. + 받는 쪽 기준 부호 있는 버전. matplotlib 필요(저장소 의존성 아님).
import os
import sqlite3
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter
FMT = FuncFormatter(lambda v, _: f"{v:g}")

ROOT = "/Users/dale/toy/auto_trading"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "research", "figures")
os.makedirs(OUT, exist_ok=True)

SURF, INK, INK2, GRID, BLUE, REF = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#2a78d6", "#8a8984"
plt.rcParams.update({"font.family": "AppleGothic", "axes.unicode_minus": False, "figure.facecolor": SURF,
                     "axes.facecolor": SURF, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
                     "ytick.color": INK2, "text.color": INK, "font.size": 11})

M = 60_000
c = sqlite3.connect(os.path.join(ROOT, "data", "xvenue.db"))
win = {}
for s, ft, t, o, cl in c.execute("SELECT symbol, ft, t, open, close FROM fh_1m"):
    win.setdefault((s, ft), {})[(t - ft) // M] = (o, cl)
c.close()
cf = sqlite3.connect(os.path.join(ROOT, "data", "candles.db"))
rate = {}
for s, t, r in cf.execute("SELECT symbol, funding_time, rate FROM funding WHERE funding_time >= 1704067200000"):
    rate[(s, int(t) // M * M)] = float(r)
cf.close()

F, G, S = [], [], []
for k, w in win.items():
    if k not in rate or 0 not in w or -1 not in w or 1 not in w:
        continue
    r = rate[k]
    o, cl = w[0]
    F.append(abs(r)); G.append(abs(cl / o - 1))
    S.append(-np.sign(r) * (w[1][0] / w[-1][0] - 1))     # 받는 쪽 기준 T-1분 시가 → T+1분 시가
F, G, S = np.array(F) * 100, np.array(G) * 100, np.array(S) * 100
n = len(F)

# ── 1. 요청: |펀딩| vs 정산 분봉 |종가 - 시가| ─────────────────────────────
FLOOR = 0.001
fig, ax = plt.subplots(figsize=(9, 6.5), dpi=150)
gy = np.maximum(G, FLOOR)
ax.scatter(F, gy, s=9, c=BLUE, alpha=0.30, linewidths=0)
lim = (0.045, max(F.max(), G.max()) * 1.3)
ax.plot(lim, lim, color=REF, lw=1.5, ls="--")
ax.text(lim[1] / 1.6, lim[1] / 1.25, "갭 = 펀딩", color=INK2, ha="right", fontsize=10)
ax.set_xscale("log"); ax.set_yscale("log"); ax.xaxis.set_major_formatter(FMT); ax.yaxis.set_major_formatter(FMT)
ax.set_xlim(lim); ax.set_ylim(FLOOR * 0.8, lim[1])
ax.grid(True, which="major", color=GRID, lw=0.8); ax.set_axisbelow(True)
for sp in ("top", "right"):
    ax.spines[sp].set_visible(False)
ax.set_xlabel("|펀딩비| (%, 정산 1회)")
ax.set_ylabel("정산 분봉 |종가 - 시가| (%, T ~ T+1분)")
above = np.mean(G > F)
zero = np.mean(G == 0)
ax.set_title(f"정산 직후 1분 움직임 vs 펀딩비 — 2024-01~ · 56심볼 · {n:,}건", loc="left", fontsize=13, pad=12)
lines = [f"갭이 펀딩보다 큰 비율: {above:.0%}"]
for lo, hi in ((0.05, 0.1), (0.1, 0.2), (0.2, 0.5), (0.5, 100)):
    m = (F >= lo) & (F < hi)
    lines.append(f"  |펀딩| {lo:g}~{hi:g}%: {np.mean(G[m] > F[m]):.0%} ({m.sum():,}건)" if hi < 100
                 else f"  |펀딩| ≥{lo:g}%: {np.mean(G[m] > F[m]):.0%} ({m.sum():,}건)")
lines.append(f"갭 0(시가=종가) {zero:.1%}는 바닥선에 표시")
ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, va="top", fontsize=9.5, color=INK2,
        bbox=dict(boxstyle="round,pad=0.5", fc=SURF, ec=GRID))
fig.tight_layout()
p1 = os.path.join(OUT, "fh_abs_gap_vs_funding.png")
fig.savefig(p1); plt.close(fig)

# ── 2. 부호 있는 버전: 받는 쪽 손익(가격) vs |펀딩| ─────────────────────────
fig, ax = plt.subplots(figsize=(9, 6.5), dpi=150)
ax.scatter(F, np.clip(S, -3, 3), s=9, c=BLUE, alpha=0.30, linewidths=0, label="정산 1건")
xs = np.logspace(np.log10(0.05), np.log10(F.max()), 50)
ax.plot(xs, -xs, color=REF, lw=1.5, ls="--", label="가격 손실 = 펀딩 (이 선보다 위면 남음, 비용 전)")
edges = np.array([0.05, 0.07, 0.1, 0.14, 0.2, 0.3, 0.5, 0.8, 1.5, 5])
mid, med = [], []
for lo, hi in zip(edges[:-1], edges[1:]):
    m = (F >= lo) & (F < hi)
    if m.sum() >= 20:
        mid.append(np.median(F[m])); med.append(np.median(S[m]))
ax.plot(mid, med, color=INK, lw=2, marker="o", ms=5, label="펀딩 구간별 중앙값")
ax.legend(loc="lower left", frameon=True, facecolor=SURF, edgecolor=GRID, fontsize=9.5)
ax.axhline(0, color=GRID, lw=1)
ax.set_xscale("log"); ax.xaxis.set_major_formatter(FMT); ax.set_xlim(0.045, 3.0); ax.set_ylim(-3.05, 3.05)
ax.grid(True, which="major", color=GRID, lw=0.8); ax.set_axisbelow(True)
for sp in ("top", "right"):
    ax.spines[sp].set_visible(False)
ax.set_xlabel("|펀딩비| (%, 정산 1회)")
ax.set_ylabel("받는 쪽 가격 손익 (%, T-1분 → T+1분, ±3% 에서 자름)")
ax.set_title("정산을 걸치면 가격이 펀딩과 비슷한 만큼 반대로 간다 — 받는 쪽 기준", loc="left", fontsize=13, pad=12)
fig.tight_layout()
p2 = os.path.join(OUT, "fh_signed_move_vs_funding.png")
fig.savefig(p2); plt.close(fig)
print(p1); print(p2); print(f"n={n} 갭>펀딩 {above:.1%}")

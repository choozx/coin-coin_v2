"""FH2 그림 — 특징별로 나눈 '받는 쪽 순손익 vs |펀딩|'. matplotlib 필요(저장소 의존성 아님).

배경 점 = 정산 1건(T-1분 → T+1분, 펀딩 + 가격 - 0.10%), 선 = 그룹별 |펀딩| 구간 평균(15건 미만 구간은 생략).
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from research.exp_FH2_traits import SPLIT, build   # noqa: E402

OUT = os.path.join(ROOT, "research", "figures")
SURF, INK, INK2, GRID, DOT = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0", "#c9c8c2"
CAT = ["#2a78d6", "#eb6834", "#1baf7a"]
plt.rcParams.update({"font.family": "AppleGothic", "axes.unicode_minus": False, "figure.facecolor": SURF,
                     "axes.facecolor": SURF, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
                     "ytick.color": INK2, "text.color": INK, "font.size": 10})
FMT = FuncFormatter(lambda v, _: f"{v:g}")
EDGES = np.array([0.05, 0.07, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.01])

rows = build()
for x in rows:
    x["net"] = (x["f"] + x["price"] - 0.0010) * 100
    x["fp"] = x["f"] * 100
q = np.quantile([x["vol"] for x in rows], [1 / 3, 2 / 3])

PANELS = [
    ("기간", [("전반 2024-01~2025-06", lambda x: x["ft"] < SPLIT), ("후반 2025-07~", lambda x: x["ft"] >= SPLIT)]),
    ("펀딩 방향", [("펀딩 - (받는 쪽 롱)", lambda x: x["sign"] < 0), ("펀딩 + (받는 쪽 숏)", lambda x: x["sign"] > 0)]),
    ("정산 주기", [("1시간", lambda x: x["gap"] <= 1), ("2~4시간", lambda x: 2 <= x["gap"] <= 5),
                ("8시간", lambda x: x["gap"] >= 6)]),
    ("직전 24시간 거래대금", [(f"하위 1/3 (<${q[0]/1e6:.0f}M)", lambda x: x["vol"] < q[0]),
                       ("중간 1/3", lambda x: q[0] <= x["vol"] < q[1]),
                       (f"상위 1/3 (>${q[1]/1e6:.0f}M)", lambda x: x["vol"] >= q[1])]),
    ("직전 24시간 가격 (받는 쪽 기준)", [("-3% 미만 (불리하게 움직였음)", lambda x: x["ret"] < -0.03),
                                ("±3%", lambda x: -0.03 <= x["ret"] < 0.03),
                                ("+3% 초과 (유리하게 움직였음)", lambda x: x["ret"] >= 0.03)]),
    ("연속성", [("직전 정산은 평범", lambda x: not x["streak"]), ("직전도 같은 방향 극단", lambda x: x["streak"])]),
]

fig, axes = plt.subplots(3, 2, figsize=(13, 13.5), dpi=140, sharex=True, sharey=True)
allf = np.array([x["fp"] for x in rows]); alln = np.array([x["net"] for x in rows])
for ax, (title, groups) in zip(axes.flat, PANELS):
    ax.scatter(allf, np.clip(alln, -2, 2), s=4, c=DOT, alpha=0.35, linewidths=0)
    ax.axhline(0, color=INK2, lw=1)
    for (name, pred), col in zip(groups, CAT):
        sub = [x for x in rows if pred(x)]
        xs, ys, es = [], [], []
        for lo, hi in zip(EDGES[:-1], EDGES[1:]):
            v = np.array([x["net"] for x in sub if lo <= x["fp"] < hi])
            if len(v) >= 15:
                xs.append(np.median([x["fp"] for x in sub if lo <= x["fp"] < hi]))
                ys.append(v.mean()); es.append(v.std() / np.sqrt(len(v)))
        xs, ys, es = map(np.array, (xs, ys, es))
        ax.fill_between(xs, ys - 2 * es, ys + 2 * es, color=col, alpha=0.12, linewidth=0)
        ax.plot(xs, ys, color=col, lw=2, marker="o", ms=4, label=f"{name} ({len(sub):,}건)")
    ax.set_xscale("log"); ax.xaxis.set_major_formatter(FMT)
    ax.set_ylim(-1.0, 1.0); ax.set_xlim(0.045, 2.3)
    ax.grid(True, which="major", color=GRID, lw=0.7); ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.set_title(title, loc="left", fontsize=11.5)
    ax.legend(loc="lower left", fontsize=8.5, frameon=True, facecolor=SURF, edgecolor=GRID)
for ax in axes[-1]:
    ax.set_xlabel("|펀딩비| (%, 정산 1회)")
for ax in axes[:, 0]:
    ax.set_ylabel("받는 쪽 순손익 (%, 수수료 0.10% 후)")
fig.suptitle("정산 확인 후 청산(T-1분 → T+1분): 어떤 정산이 0 위에 있나 — 선 = 구간 평균, 띠 = ±2 표준오차",
             x=0.01, ha="left", fontsize=13)
fig.tight_layout(rect=(0, 0, 1, 0.97))
p = os.path.join(OUT, "fh2_traits.png")
fig.savefig(p)
print(p)

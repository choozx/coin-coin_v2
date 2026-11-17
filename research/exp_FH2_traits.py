"""FH2 — 펀딩 수확을 '정산 확인 후 청산'(HFT 와 안 싸움)으로 할 때, 어떤 정산을 골라야 하나.

FH 결과: 정산 후 0.1~2초에 가격이 펀딩만큼 반대로 간다 → 평균으론 남는 게 없다. 그런데 산점도
(research/figures/fh_signed_move_vs_funding.png)에서 구간별 중앙값이 '가격 손실 = 펀딩' 선보다 위에 있는 구간이 있었다.
**정산 전에 알 수 있는 특징**으로 그런 정산을 골라낼 수 있는가.

가정: 진입 T−1분 시가 · 청산 T+1분 시가(FUNDING_FEE 확인 후, 최대 1분) · 받는 쪽 · 비용 0.10% / 0.20%.
데이터: FH 의 fh_1m (2024-01~ · M 유니버스 56심볼 · |펀딩| ≥ 0.05% 정산 7,087건).

특징(전부 T 이전 정보):
  크기   |펀딩|
  방향   펀딩 + (받는 쪽 숏) / − (받는 쪽 롱)
  주기   직전 정산과의 간격 1h · 4h · 8h (짧아진 건 과열 종목)
  유동성 직전 24h 거래대금(1h봉 close×volume 합)
  급등락 직전 24h 가격 변동을 받는 쪽 기준으로(+ 면 받는 쪽에 유리하게 이미 움직였음)
  연속   직전 정산도 같은 부호로 |펀딩| ≥ 0.05% 였나

커브핏 방지: 전반(2024-01~2025-06)과 후반(2025-07~)을 따로 보고, **둘 다 같은 쪽**인 특징만 믿는다.

    python3 -u -m research.exp_FH2_traits
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs
from research.exp_FH_funding_harvest import conn, events

M, H = 60_000, 3_600_000
SPLIT = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
COSTS = (0.0010, 0.0020)


def build():
    """이벤트별 (특징 dict, 받는 쪽 순손익 전 가격+펀딩)."""
    c = conn()
    win = {}
    for s, ft, t, o in c.execute("SELECT symbol, ft, t, open FROM fh_1m"):
        win.setdefault((s, ft), {})[(t - ft) // M] = o
    c.close()
    cc = sqlite3.connect(cs.DB_PATH)
    fund = {}
    for s, t, r in cc.execute("SELECT symbol, funding_time, rate FROM funding WHERE funding_time >= 1700000000000 "
                              "ORDER BY symbol, funding_time"):
        fund.setdefault(s, []).append((int(t) // M * M, float(r)))
    ev = events()
    syms = {s for s, _, _ in ev}
    hourly = {}
    for s in syms:
        rows = cc.execute("SELECT open_time, close, volume FROM candle WHERE symbol=? ORDER BY open_time",
                          (s + "_1H",)).fetchall()
        hourly[s] = (np.array([r[0] for r in rows], dtype=np.int64), np.array([r[1] for r in rows]),
                     np.array([r[1] * (r[2] or 0) for r in rows]))
    cc.close()
    out = []
    for s, ft, r in ev:
        w = win.get((s, ft))
        if not w or -1 not in w or 1 not in w:
            continue
        side = -np.sign(r)
        price = side * (w[1] / w[-1] - 1)
        fl = fund[s]
        i = next((k for k in range(len(fl) - 1, -1, -1) if fl[k][0] == ft), None)
        if i is None or i == 0:
            continue
        gap_h = round((fl[i][0] - fl[i - 1][0]) / H)
        prev = fl[i - 1][1]
        ts, cl, qv = hourly[s]
        j = np.searchsorted(ts, ft, side="left")              # T 이전 마지막 1h 봉까지(룩어헤드 없음: 봉 시작 < T 이고 끝 ≤ T)
        if j < 25:
            continue
        vol24 = qv[j - 24:j].sum()
        ret24 = side * (cl[j - 1] / cl[j - 25] - 1)
        out.append(dict(ft=ft, sym=s, f=abs(r), sign=int(np.sign(r)), gap=gap_h, vol=vol24, ret=ret24,
                        streak=int(np.sign(prev) == np.sign(r) and abs(prev) >= 0.0005), price=price))
    return out


def show(rows, key, bins, label, fmt):
    print(f"\n■ {label}   (건당 순손익 % = 펀딩 + 가격 − 비용 0.10% · [전반 | 후반] · 건수 · 이익 비율)")
    for lo, hi, name in bins:
        cells = []
        for half in (0, 1):
            sel = [x for x in rows if lo <= key(x) < hi and (x["ft"] >= SPLIT) == bool(half)]
            if len(sel) < 15:
                cells.append(f"{'—':>28}")
                continue
            net = np.array([x["f"] + x["price"] - COSTS[0] for x in sel]) * 100
            se = net.std() / np.sqrt(len(net))
            cells.append(f"{net.mean():+6.3f}±{se:.3f} ({len(sel):4d}, {np.mean(net > 0):3.0%})")
        print(f"   {name:16} " + " | ".join(cells))


def main():
    rows = build()
    n0 = sum(x["ft"] < SPLIT for x in rows)
    print(f"FH2 · 정산 확인 후 청산(T−1분 → T+1분) · {len(rows):,}건 (전반 {n0:,} · 후반 {len(rows)-n0:,})")
    print("   ±는 표준오차. 두 반쪽 모두 0보다 확실히 위(평균 − 2×표준오차 > 0)여야 쓸 만하다.")
    show(rows, lambda x: x["f"], [(0.0005, 0.001, "0.05~0.1%"), (0.001, 0.002, "0.1~0.2%"), (0.002, 0.003, "0.2~0.3%"),
                                   (0.003, 0.005, "0.3~0.5%"), (0.005, 0.01, "0.5~1%"), (0.01, 0.0199, "1~2%"),
                                   (0.0199, 1, "상한 2%")], "펀딩 크기", None)
    show(rows, lambda x: x["sign"], [(-1, 0, "펀딩 − (받는 쪽 롱)"), (1, 2, "펀딩 + (받는 쪽 숏)")], "펀딩 방향", None)
    show(rows, lambda x: x["gap"], [(0, 2, "1시간 주기"), (2, 6, "2~4시간 주기"), (6, 99, "8시간 주기")], "정산 주기", None)
    q = np.quantile([x["vol"] for x in rows], [1 / 3, 2 / 3])
    show(rows, lambda x: x["vol"], [(0, q[0], f"하위 1/3 (<${q[0]/1e6:.0f}M)"), (q[0], q[1], "중간 1/3"),
                                    (q[1], 1e18, f"상위 1/3 (>${q[1]/1e6:.0f}M)")], "직전 24h 거래대금", None)
    show(rows, lambda x: x["ret"], [(-9, -0.10, "받는 쪽에 −10% 미만"), (-0.10, -0.03, "−10~−3%"), (-0.03, 0.03, "±3%"),
                                    (0.03, 0.10, "+3~+10%"), (0.10, 99, "+10% 초과")], "직전 24h 가격(받는 쪽 기준)", None)
    show(rows, lambda x: x["streak"], [(0, 1, "직전 정산은 평범"), (1, 2, "직전도 같은 방향 극단")], "연속성", None)
    # 크기 × 방향
    for sg, nm in ((-1, "펀딩 −"), (1, "펀딩 +")):
        sub = [x for x in rows if x["sign"] == sg]
        show(sub, lambda x: x["f"], [(0.0005, 0.002, "0.05~0.2%"), (0.002, 0.005, "0.2~0.5%"), (0.005, 0.0199, "0.5~2%"),
                                     (0.0199, 1, "상한 2%")], f"크기 × 방향 — {nm}", None)


if __name__ == "__main__":
    main()

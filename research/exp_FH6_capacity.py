"""FH6 규모 — 처리 직후 청산(T+0.5초)을 건당 얼마까지 넣을 수 있나, 하루에 얼마나 되나. (데이터: fh6_vol, --collect-vol)

과거 호가창은 없다 → **실제 체결량**으로 소화 가능 규모를 어림한다.
  청산 흐름  T+0.5~1.0초에 '받는 쪽이 청산하는 방향'(받는 쪽 롱이면 매도 테이커) 체결대금 = 그 순간 같은 쪽으로 던진 양.
             내 청산이 그 흐름의 10% 이하면 가격을 크게 밀지 않는다고 본다(경험칙 — 실측으로 바꿀 것).
  진입 흐름  T−15~−5초 전체 체결대금 — 진입은 급하지 않아 지정가로 나눌 수 있지만 같은 10% 로 본다.
  건당 한도 = min(두 흐름 × 10%).  · 가격 충격 어림: T+0.5~0.6초 청산 방향 체결의 VWAP 이 T+0.5초 마지막 체결가보다 얼마나 나쁜가.
하루 기대치(최근 6개월, 실전 조건 |펀딩| ≥ 0.5% · 주기 4h·8h): 건수 · 건당 순(%) · 한도 × 순 의 합.

    python3 -u -m research.exp_FH6_capacity
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from research.exp_FH5_post_trend import conn as fh5_conn, events as fh5_events
from research.xv_data import DB

COST = 0.0010
SHARE = 0.10
RECENT = int(datetime(2026, 5, 18, tzinfo=timezone.utc).timestamp() * 1000)


def main():
    c = sqlite3.connect(DB)
    tr = defaultdict(list)
    for s, ft, t, p, q, bm in c.execute("SELECT symbol, ft, t, price, qty, buyer_maker FROM fh6_vol ORDER BY symbol, ft, t"):
        tr[(s, ft)].append((t - ft, p, q, bm))
    c.close()
    f5 = fh5_conn()
    ev = fh5_events(f5)
    gap, last_t = {}, {}
    for s, t, r in f5.execute("SELECT symbol, t, rate FROM fh5_funding ORDER BY symbol, t"):
        t = int(t) // 60_000 * 60_000
        if s in last_t:
            gap[(s, t)] = round((t - last_t[s]) / 3_600_000)
        last_t[s] = t
    f5.close()

    rows = []
    for s, ft, r in ev:
        a = tr.get((s, ft))
        if not a:
            continue
        a = np.array(a)
        tt, pp, qq, bm = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        usd = pp * qq
        d = -np.sign(r)                                   # 받는 쪽: +1 롱 · −1 숏
        closing = (bm == 1) if d > 0 else (bm == 0)       # 롱 청산 = 매도 테이커(매수자가 메이커)
        pre = tt < -10_000
        if not pre.any():
            continue
        p0 = pp[np.where(tt <= -10_000)[0][-1]]
        i1 = np.where(tt <= 500)[0]
        if not len(i1):
            continue
        p1 = pp[i1[-1]]
        net = abs(r) + d * (p1 / p0 - 1) - COST
        w_exit = (tt > 500) & (tt <= 1000) & closing
        w_ent = (tt >= -15_000) & (tt < -5_000)
        flow_exit, flow_ent = usd[w_exit].sum(), usd[w_ent].sum()
        w_imp = (tt > 500) & (tt <= 600) & closing
        imp = (d * ((usd[w_imp].sum() / qq[w_imp].sum()) / p1 - 1)) if w_imp.any() else np.nan
        g = gap.get((s, ft), 0)
        ivl = 1 if g <= 2 else (4 if g <= 5 else 8)
        rows.append(dict(s=s, ft=ft, f=abs(r), ivl=ivl, net=net, flow_exit=flow_exit, flow_ent=flow_ent,
                         cap=SHARE * min(flow_exit, flow_ent), imp=imp))
    print(f"FH6 규모 · {len(rows):,}건 (|펀딩| ≥ 0.5%, M 밖 심볼) · 흐름 = 실제 체결대금, 한도 = 흐름 × {SHARE:.0%}")

    def show(sel, title):
        if not sel:
            return
        fe = np.array([x["flow_exit"] for x in sel]); fn = np.array([x["flow_ent"] for x in sel])
        cap = np.array([x["cap"] for x in sel]); imp = np.array([x["imp"] for x in sel]) * 100
        net = np.array([x["net"] for x in sel]) * 100
        print(f"\n■ {title} · {len(sel):,}건 · 건당 순 {net.mean():+.3f}%")
        print(f"   청산 흐름(T+0.5~1초, 청산 방향) 중앙 ${np.median(fe):,.0f} · 25% ${np.percentile(fe,25):,.0f} · 75% ${np.percentile(fe,75):,.0f}")
        print(f"   진입 흐름(T−15~−5초, 전체)      중앙 ${np.median(fn):,.0f} · 25% ${np.percentile(fn,25):,.0f} · 75% ${np.percentile(fn,75):,.0f}")
        print(f"   건당 한도(10%)                   중앙 ${np.median(cap):,.0f} · 25% ${np.percentile(cap,25):,.0f} · 75% ${np.percentile(cap,75):,.0f}")
        print(f"   청산 직후 100ms 청산 방향 체결 VWAP 이 T+0.5초 가격보다 불리한 정도: 중앙 {np.nanmedian(imp):+.3f}% · "
              f"75% {np.nanpercentile(imp,75):+.3f}%")
        for lim in (50, 200, 1000, 5000):
            ok = cap >= lim
            print(f"   건당 ${lim:>5} 넣을 수 있는 정산 {ok.mean():5.1%} · 그 정산들의 순 {net[ok].mean() if ok.any() else float('nan'):+.3f}%")

    show(rows, "전체")
    real = [x for x in rows if x["ivl"] >= 4]
    show(real, "실전 조건(주기 4h·8h)")
    rec = [x for x in real if x["ft"] >= RECENT]
    show(rec, "실전 조건 · 최근 6개월(2026-05-18~)")
    if rec:
        days = (max(x["ft"] for x in rec) - RECENT) / 86_400_000
        byT = defaultdict(int)
        for x in rec:
            byT[x["ft"]] += 1
        print(f"\n■ 하루 기대치(최근 6개월 · 실전 조건) — {days:.0f}일")
        print(f"   정산 {len(rec)/days:.1f}건/일 · 같은 시각에 겹친 평균 {np.mean(list(byT.values())):.1f}개 · 최대 {max(byT.values())}개")
        for cap_lim in (100, 500, 2000, 10_000):
            usd = sum(x["net"] * min(x["cap"], cap_lim) for x in rec)
            used = np.mean([min(x["cap"], cap_lim) for x in rec])
            print(f"   건당 상한 ${cap_lim:>6} (한도보다 작게): 하루 ${usd/days:+8.2f} · 월 ${usd/days*30:+9.1f} · "
                  f"건당 평균 투입 ${used:,.0f}")
        print("   ※ 순은 마지막 체결가 기준(비용 0.10% 포함) — 처리 지연(펀딩 못 받음)·실제 슬리피지는 미반영")


if __name__ == "__main__":
    main()

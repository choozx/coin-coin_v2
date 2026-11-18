"""XS — 알트 횡단면 롱숏(시장 중립): 모멘텀 · 단기 반전 · 펀딩 팩터 (사용자 선택, 2026-11-18).

방향을 맞히지 않는다 — 같은 날 코인들을 줄 세워 위쪽을 사고 아래쪽을 판다(달러 중립). 시장 전체 등락은 상쇄된다.
근거: 코인 횡단면 모멘텀(1~4주)·단기 반전(1일)·캐리(펀딩) 효과를 보고한 연구들이 있다. 그러나 비용·최근성은 따로 확인해야 한다.

━━ 사전 고정 (2026-11-18 작성 · 결과 보기 전) ━━
데이터  USDT 무기한 COIN 전체(TRADING+SETTLING — 폐지 포함) 일봉 · 펀딩(fh5_funding + candles.db funding) · 2024-01-01 ~ 2026-11-16.
대상    그날 기준 상장 ≥ 30일 · 직전 30일 일 거래대금 중앙 ≥ $5M · 스테이블 제외.
포트    매 리밸런스일 종가(00:00 UTC)에 신호 → 같은 종가 체결 가정. 상위 10% 롱 · 하위 10% 숏(또는 반대), 동일 가중,
        총 노출 1(롱 0.5 + 숏 0.5), 1배. 보유 중 폐지되면 마지막 종가로 청산. 펀딩은 실제로 주고받는다(롱은 +펀딩을 냄).
비용    회전 1단위당 0.10%(수수료 0.05 + 슬리피지 0.05) · Σ|Δ가중치| × 0.10%.
전략(3개, 동시에 판정)
  MOM   매주 월요일 일봉 종가(= 화 00:00 UTC) · 지난 28일 수익률 상위 롱 / 하위 숏 · 7일 보유
  REV   매일 · 지난 1일 수익률 하위 롱 / 상위 숏 · 1일 보유
  FUND  매주 월요일 일봉 종가 · 지난 7일 펀딩 합 하위 롱 / 상위 숏(펀딩 받기 쪽) · 7일 보유
합격(전략마다, 넷 다)  ① 기간 수익의 t ≥ 2.4 (3개 동시 검정이라 2 → 2.4)  ② 반기 2025-H2 · 2026-H1 · 2026-H2 모두 > 0
                      ③ 가장 좋은 기간 1개를 빼도 누적 > 0  ④ 10% 대신 20%(5분위)로 해도 연 수익 > 0
참고(판정 아님)  비용 0 · 롱만/숏만 다리 · 거래대금 문턱 $20M.

    python3 -u -m research.exp_XS_cross_section --collect
    python3 -u -m research.exp_XS_cross_section
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs
from research.exp_FH5_post_trend import _get
from research.xv_data import DB

D = 86_400_000
START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp() * 1000)
FETCH_FROM = START - 60 * D
COST = 0.0010
MINQ, AGE = 5e6, 30
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "USD1", "RLUSD", "XUSD", "PYUSD", "BFUSD"}
HALVES = [("2024-H1", 2024, 1), ("2024-H2", 2024, 7), ("2025-H1", 2025, 1), ("2025-H2", 2025, 7), ("2026-H1", 2026, 1), ("2026-H2", 2026, 7)]


def _ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS xs_1d (symbol TEXT, t INTEGER, close REAL, qvol REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS xs_sym (symbol TEXT PRIMARY KEY, base TEXT, onboard INTEGER, done INTEGER)")
    return c


def collect():
    c = conn()
    info = _get("exchangeInfo", {})
    for s in info["symbols"]:
        if (s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("underlyingType") == "COIN"
                and s.get("status") in ("TRADING", "SETTLING")):
            c.execute("INSERT OR IGNORE INTO xs_sym VALUES (?,?,?,0)", (s["symbol"], s["baseAsset"], int(s.get("onboardDate", 0))))
    c.commit()
    todo = [r[0] for r in c.execute("SELECT symbol FROM xs_sym WHERE done=0")]
    print(f"심볼 {c.execute('SELECT COUNT(*) FROM xs_sym').fetchone()[0]}개 · 받을 것 {len(todo)}개", flush=True)
    for n, s in enumerate(todo, 1):
        cur = FETCH_FROM
        while cur < END + D:
            rows = _get("klines", {"symbol": s, "interval": "1d", "startTime": cur, "endTime": END + D, "limit": 1000})
            time.sleep(0.12)
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO xs_1d VALUES (?,?,?,?)", [(s, int(k[0]), float(k[4]), float(k[7])) for k in rows])
            cur = int(rows[-1][0]) + D
            if len(rows) < 1000:
                break
        c.execute("UPDATE xs_sym SET done=1 WHERE symbol=?", (s,))
        c.commit()
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}", flush=True)
    c.close()


def load():
    c = conn()
    syms = [(s, b, ob) for s, b, ob in c.execute("SELECT symbol, base, onboard FROM xs_sym") if b not in STABLE]
    days = np.arange(FETCH_FROM, END + D, D, dtype=np.int64)
    idx = {s: j for j, (s, _, _) in enumerate(syms)}
    C = np.full((len(days), len(syms)), np.nan)
    Q = np.full((len(days), len(syms)), np.nan)
    for s, t, cl, q in c.execute("SELECT symbol, t, close, qvol FROM xs_1d"):
        if s in idx and FETCH_FROM <= t <= END:
            i = (t - FETCH_FROM) // D
            C[i, idx[s]], Q[i, idx[s]] = cl, q
    c.close()
    Fd = np.zeros((len(days), len(syms)))                    # 일봉 i (t_i ~ t_i+D) 안에 정산된 펀딩 합
    def add(s, t, r):
        if s in idx and FETCH_FROM <= t < END + D:
            Fd[(t - FETCH_FROM) // D, idx[s]] += r
    f5 = sqlite3.connect(DB)
    for s, t, r in f5.execute("SELECT symbol, t, rate FROM fh5_funding"):
        add(s, int(t), float(r))
    f5.close()
    m = {s for s in idx}
    cc = sqlite3.connect(cs.DB_PATH)
    have5 = set()
    for s, t, r in cc.execute("SELECT symbol, funding_time, rate FROM funding"):
        if s in m:
            have5.add(s)
            add(s, int(t), float(r))
    cc.close()
    age = np.array([[(t - ob) / D for ob in [x[2] for x in syms]] for t in days])
    return days, [s for s, _, _ in syms], C, Q, Fd, age


def run(days, C, Q, Fd, age, kind, frac=0.10, cost=COST, minq=MINQ, legs="both"):
    n, m = C.shape
    look, hold, step = {"MOM": (28, 7, 7), "REV": (1, 1, 1), "FUND": (7, 7, 7)}[kind]
    i0 = int(np.searchsorted(days, START))
    if step == 7:                                              # 월요일 맞추기
        while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != 0:
            i0 += 1
    w = np.zeros(m)
    out = []
    for i in range(i0, n - hold, step):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - look]) & (age[i] >= AGE)
        qmed = np.nanmedian(Q[i - 29:i + 1], axis=0)
        live &= np.nan_to_num(qmed) >= minq
        js = np.where(live)[0]
        if len(js) < 20:
            continue
        if kind == "MOM":
            sig = C[i, js] / C[i - look, js] - 1                     # 높을수록 롱
        elif kind == "REV":
            sig = -(C[i, js] / C[i - 1, js] - 1)                    # 많이 빠질수록 롱
        else:
            sig = -Fd[i - look + 1:i + 1, js].sum(0)                # 펀딩 낮을수록 롱
        k = max(1, int(len(js) * frac))
        order = np.argsort(-sig)
        L, S = js[order[:k]], js[order[-k:]]
        nw = np.zeros(m)
        if legs in ("both", "long"):
            nw[L] = 0.5 / k
        if legs in ("both", "short"):
            nw[S] = -0.5 / k
        turn = np.abs(nw - w).sum()
        # 보유 수익: 폐지면 마지막 종가
        r = np.zeros(m)
        for j in np.where(nw != 0)[0]:
            seg = C[i:i + hold + 1, j]
            last = np.where(~np.isnan(seg))[0][-1]
            r[j] = seg[last] / seg[0] - 1 - Fd[i + 1:i + 1 + last, j].sum()   # 롱 기준: 가격 − 낸 펀딩
        ret = (nw * r).sum() - turn * cost
        # 다음 리밸런스 전 가중치 표류는 무시(동일 가중 재설정)
        w = nw
        out.append((int(days[i]), ret, turn))
    return out


def judge(res, title, extra=None):
    t = np.array([x[0] for x in res]); r = np.array([x[1] for x in res])
    if len(r) < 10:
        print(f"\n■ {title}: {len(r)}기간")
        return False
    per_yr = len(r) / ((t[-1] - t[0]) / D / 365)
    tstat = r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
    eq = np.cumprod(1 + r)
    mdd = (eq / np.maximum.accumulate(eq) - 1).min()
    halves = []
    for i, (nm, y, m) in enumerate(HALVES):
        a = _ms(y, m)
        b = _ms(*HALVES[i + 1][1:]) if i + 1 < len(HALVES) else 1 << 62
        hv = r[(t >= a) & (t < b)]
        halves.append((nm, hv))
    c1 = tstat >= 2.4
    c2 = all(len(h) and h.sum() > 0 for _, h in halves[3:])
    c3 = np.prod(1 + np.delete(r, r.argmax())) - 1 > 0
    c4 = extra is None or extra > 0
    print(f"\n■ {title} · {len(r)}기간 · 연 {per_yr:.0f}회")
    print(f"   연 수익(단순) {r.mean()*per_yr*100:+.1f}% · 누적 {eq[-1]*100-100:+.1f}% · t {tstat:+.2f} · 최대낙폭 {mdd*100:+.1f}% · "
          f"이익 기간 {np.mean(r>0):.0%} · 평균 회전 {np.mean([x[2] for x in res]):.2f}")
    print("   반기 합: " + " · ".join(f"{nm} {h.sum()*100:+.1f}%" if len(h) else f"{nm} -" for nm, h in halves))
    print(f"   ① t≥2.4 {'✅' if c1 else '❌'} · ② 25H2·26H1·26H2 {'✅' if c2 else '❌'} · ③ 최고 기간 빼도 {'✅' if c3 else '❌'} · "
          f"④ 5분위 {'✅' if c4 else '❌'}" + (f"({extra*100:+.1f}%)" if extra is not None else ""))
    return c1 and c2 and c3 and c4


def main():
    if "--collect" in sys.argv:
        collect()
        return
    days, syms, C, Q, Fd, age = load()
    print(f"심볼 {len(syms)}개 · {len(days)}일")
    for kind, title in (("MOM", "MOM 28일 모멘텀 · 주간"), ("REV", "REV 1일 반전 · 일간"), ("FUND", "FUND 펀딩 팩터 · 주간")):
        q5 = run(days, C, Q, Fd, age, kind, frac=0.20)
        q5y = np.mean([x[1] for x in q5]) * (365 if kind == "REV" else 52)
        ok = judge(run(days, C, Q, Fd, age, kind), f"{title} · 사전 고정", extra=q5y)
        print(f"   판정: {'✅ 합격' if ok else '❌ 불합격'}")
        judge(run(days, C, Q, Fd, age, kind, cost=0), f"{title} · 참고: 비용 0")
        judge(run(days, C, Q, Fd, age, kind, legs="long"), f"{title} · 참고: 롱 다리만")
        judge(run(days, C, Q, Fd, age, kind, legs="short"), f"{title} · 참고: 숏 다리만")
        judge(run(days, C, Q, Fd, age, kind, minq=2e7), f"{title} · 참고: 거래대금 ≥ $20M")


if __name__ == "__main__":
    main()

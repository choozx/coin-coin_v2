"""FH 처리 시각 측정 — 최소 수량 포지션을 여러 정산 동안 그냥 들고, 정산마다 펀딩이 실제로 처리된 시각을 모은다.

왜: FH6(처리 직후 T+0.5초 예약 청산)은 '처리가 T+0.5초보다 늦는 정산'에서 펀딩 없이 하락만 맞는다.
바이낸스 FAQ 는 처리가 최대 15초 늦을 수 있다고 한다. 프로브 실측은 3회뿐(T+264~374ms) → 분포·꼬리가 필요하다.
방법: 정산마다 /fapi/v1/income(FUNDING_FEE) 의 time(= 거래소가 처리한 시각) − 정산 시각 T.
      주문은 처음 진입·마지막 청산 한 번씩뿐(심볼당 ~$6, 받는 쪽). 1h 주기 심볼이면 하루 24번 표본.

안전: --mainnet --go 둘 다 있어야 주문 · 포지션당 명목 상한 20 USDT · 이미 포지션 있는 심볼은 건너뜀 ·
      끝나거나 Ctrl+C·kill(SIGTERM) 이면 이 스크립트가 연 포지션만 정리 · BTCUSDC 제외.
키: fh_probe 와 같다(BINANCE_MAINNET_API_KEY / SECRET).

    python research/fh_proc_watch.py --mainnet --plan                 # 고를 심볼만(주문 없음)
    nohup python research/fh_proc_watch.py --mainnet --go --hours=24 > ~/proc_watch.log 2>&1 &
    python research/fh_proc_watch.py --report                         # 쌓인 기록 요약(주문 없음, 키 불필요)
옵션: --n=5(심볼 수) · --symbols=A,B,C(직접 지정) · --hours=24
결과: data/fh_probe/ (못 쓰면 ~/fh_probe/) 의 proc_watch_*.jsonl — 한 줄 = 정산 1회 × 심볼 1개.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "--mainnet" not in sys.argv and "--report" not in sys.argv:
    sys.exit("실서버 전용이에요: --mainnet 을 붙여 주세요(--report 는 키 없이 기록만 요약)")

from research.fh_probe import (EXCLUDE, LEVERAGE, MAX_NOTIONAL, ROOT, Api, _env, _filters,  # noqa: E402
                               _flatten, _order, _positions, step_round_up)

H = 3_600_000
MIN_QVOL = 20_000_000
DIRS = (os.path.join(ROOT, "data", "fh_probe"), os.path.expanduser("~/fh_probe"))


def _arg(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def _append(name, rec):
    for d in DIRS:
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, name)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return p
        except OSError:
            continue
    raise OSError("기록할 곳이 없어요(data/fh_probe · ~/fh_probe)")


def pick(api, n, fixed=None):
    """자동 선택: 1h 주기 n−2 개 + 4h·8h 한 개씩(거래대금 큰 순, 없으면 1h 로 채움). 지정 시엔 그 심볼들."""
    info = {s["symbol"]: s for s in api.call("GET", "/fapi/v1/exchangeInfo")["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    prem = {p["symbol"]: p for p in api.call("GET", "/fapi/v1/premiumIndex") if p["symbol"] in info}
    ivl = {x["symbol"]: int(x.get("fundingIntervalHours", 8)) for x in api.call("GET", "/fapi/v1/fundingInfo")}
    qv = {t["symbol"]: float(t["quoteVolume"]) for t in api.call("GET", "/fapi/v1/ticker/24hr")}

    def make(s):
        if s in EXCLUDE or s not in prem:
            return None
        step, minq, mn = _filters(info, s)
        px = float(prem[s]["markPrice"])
        qty = step_round_up(max(minq, max(mn * 1.2, 6.0) / px), step)
        if qty * px > MAX_NOTIONAL:
            return None
        rate = float(prem[s]["lastFundingRate"])
        return dict(symbol=s, qty=qty, mark=px, rate=rate, interval=ivl.get(s, 8), qvol=qv.get(s),
                    side="SELL" if rate > 0 else "BUY", next=int(prem[s]["nextFundingTime"]))

    if fixed:
        return [x for x in map(make, fixed) if x]
    pools = {}
    for s in sorted(prem, key=lambda s: -qv.get(s, 0)):
        if qv.get(s, 0) >= MIN_QVOL:
            x = make(s)
            if x:
                pools.setdefault(x["interval"], []).append(x)
    out = pools.get(4, [])[:1] + pools.get(8, [])[:1]
    out = pools.get(1, [])[:max(0, n - len(out))] + out
    return out[:n]


def report(paths=None):
    paths = paths or sorted(p for d in DIRS for p in glob.glob(os.path.join(d, "proc_watch_*.jsonl")))
    recs = []
    for p in paths:
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            if r.get("kind") == "fund":
                recs.append(r)
    got = [r for r in recs if r.get("offset") is not None]
    print(f"처리 시각 기록 {len(recs)}건(펀딩 붙음 {len(got)} · 안 붙음/조회 실패 {len(recs) - len(got)}) · 파일 {len(paths)}개")
    if not got:
        return
    o = np.array([r["offset"] for r in got])
    print(f"   T 대비 처리 시각(ms): 최소 {o.min():.0f} · 10% {np.percentile(o, 10):.0f} · 중앙 {np.median(o):.0f} · "
          f"90% {np.percentile(o, 90):.0f} · 99% {np.percentile(o, 99):.0f} · 최대 {o.max():.0f}")
    print("   늦은 비율: " + " · ".join(f">{t}ms {np.mean(o > t):.1%}" for t in (400, 500, 700, 1000, 5000)))
    for iv in sorted({r["interval"] for r in got}):
        a = np.array([r["offset"] for r in got if r["interval"] == iv])
        print(f"   {iv}h 주기 {len(a):4d}건 중앙 {np.median(a):.0f} · 90% {np.percentile(a, 90):.0f} · 최대 {a.max():.0f}")
    by = {}
    for r in got:
        by.setdefault(r["symbol"], []).append(r["offset"])
    print("   심볼별 중앙: " + " · ".join(f"{s} {np.median(v):.0f}" for s, v in sorted(by.items(), key=lambda x: np.median(x[1]))))
    # 같은 정산 안에서 심볼 순서가 일정한가(순차 처리 흔적)
    byT = {}
    for r in got:
        byT.setdefault(r["T"], []).append((r["offset"], r["symbol"]))
    orders = [tuple(s for _, s in sorted(v)) for v in byT.values() if len(v) >= 3]
    if orders:
        from collections import Counter
        top, cnt = Counter(orders).most_common(1)[0]
        print(f"   같은 정산 안 처리 순서: 가장 흔한 순서 {' < '.join(top)} — {cnt}/{len(orders)}회")
        gaps = [np.diff(sorted(o for o, _ in v)).mean() for v in byT.values() if len(v) >= 2]
        print(f"   같은 정산 안 심볼 간 간격 평균 {np.mean(gaps):.0f}ms")


def main():
    if "--report" in sys.argv:
        return report()
    api = Api(*_env())
    rtt, off = api.sync()
    print(f"[mainnet] 오프셋 {off:+.1f}ms · 왕복 {rtt:.1f}ms")
    n = int(_arg("n", "5"))
    fixed = [s.strip().upper() for s in _arg("symbols", "").split(",") if s.strip()] or None
    picks = pick(api, n, fixed)
    if api.key:
        pos = _positions(api)
        busy = [p["symbol"] for p in picks if abs(float(pos.get(p["symbol"], {}).get("positionAmt", 0))) > 0]
        if busy:
            print(f"   이미 포지션이 있어 뺌: {busy}")
        picks = [p for p in picks if p["symbol"] not in busy]
    elif "--go" in sys.argv:
        sys.exit("키가 없어요 — --go 는 키가 필요해요")
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")
    print(f"심볼 {len(picks)}개 (합계 명목 ≈ ${sum(p['qty'] * p['mark'] for p in picks):.0f})")
    for p in picks:
        print(f"   {p['symbol']:14} {p['interval']}h 주기 · {p['side']:4} {p['qty']} (≈${p['qty']*p['mark']:.1f}) · "
              f"펀딩 {p['rate']*100:+.4f}% · 다음 정산 {f(p['next'])} · 24h ${(p['qvol'] or 0)/1e6:.0f}M")
    if "--go" not in sys.argv:
        print("(--plan: 주문 없음. 실행은 --go)")
        return
    hours = float(_arg("hours", "24"))
    dual = api.call("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition", False)
    name = f"proc_watch_{datetime.now(timezone.utc):%Y%m%d_%H%M}.jsonl"

    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    opened = []
    try:
        for p in picks:
            try:
                br = api.call("GET", "/fapi/v1/leverageBracket", dict(symbol=p["symbol"]), signed=True)[0]["brackets"]
                api.call("POST", "/fapi/v1/leverage",
                         dict(symbol=p["symbol"], leverage=min(LEVERAGE, max(int(b["initialLeverage"]) for b in br))),
                         signed=True)
                r = _order(api, dual, p["symbol"], p["side"], p["qty"], pos_side="SHORT" if p["side"] == "SELL" else "LONG")
                opened.append(p)
                _append(name, dict(kind="open", t=int(api.now()), symbol=p["symbol"], side=p["side"], qty=p["qty"],
                                   avgPrice=r.get("avgPrice"), interval=p["interval"]))
            except Exception as e:      # noqa: BLE001
                print(f"   ⚠ {p['symbol']} 진입 실패 — 제외: {repr(e)[:120]}", flush=True)
        if not opened:
            sys.exit("연 포지션이 없어요 — 중단")
        end = api.now() + hours * H
        print(f"진입 {len(opened)}개 · {hours:g}시간 측정 · 기록 {name}", flush=True)
        n_ok = n_miss = 0
        while True:
            prem = {x["symbol"]: x for x in api.call("GET", "/fapi/v1/premiumIndex")}
            T = min(int(prem[p["symbol"]]["nextFundingTime"]) for p in opened)
            if T > end:
                break
            due = [p for p in opened if int(prem[p["symbol"]]["nextFundingTime"]) == T]
            time.sleep(max(0, (T + 90_000 - api.now()) / 1000))          # 정산 + 90초 뒤 조회(처리 최대 15초 + 여유)
            api.sync(5)
            for p in due:
                rec = dict(kind="fund", T=T, symbol=p["symbol"], interval=p["interval"], offset=None)
                try:
                    inc = api.call("GET", "/fapi/v1/income", dict(symbol=p["symbol"], incomeType="FUNDING_FEE",
                                                                 startTime=T - 60_000, endTime=T + 90_000), signed=True)
                    if inc:
                        x = inc[-1]
                        rec.update(offset=int(x["time"]) - T, income=float(x["income"]))
                    fr = api.call("GET", "/fapi/v1/fundingRate", dict(symbol=p["symbol"], startTime=T - 60_000, limit=1))
                    if fr:
                        rec.update(rate=float(fr[0]["fundingRate"]), rate_stamp=int(fr[0]["fundingTime"]) - T)
                    amt = float(_positions(api).get(p["symbol"], {}).get("positionAmt", 0))
                    rec["held"] = abs(amt) > 0
                except Exception as e:      # noqa: BLE001
                    rec["error"] = repr(e)[:160]
                _append(name, rec)
                n_ok += rec["offset"] is not None
                n_miss += rec["offset"] is None
            got = [f"{p['symbol']}" for p in due]
            print(f"{f(T)} 정산 · {', '.join(got)} 기록 · 누적 붙음 {n_ok} · 없음 {n_miss}", flush=True)
    except KeyboardInterrupt:
        print("중단 신호 — 정리합니다", flush=True)
    finally:
        if opened:
            left = _flatten(api, dual, [p["symbol"] for p in opened])
            time.sleep(1)
            still = [s for s in left if abs(float(_positions(api).get(s, {}).get("positionAmt", 0))) > 0]
            print(f"정리: 청산 주문 {len(left)}개 · 남은 포지션 {still or '없음'}", flush=True)
            _append(name, dict(kind="close", t=int(api.now()), left=still))
    report()


if __name__ == "__main__":
    main()

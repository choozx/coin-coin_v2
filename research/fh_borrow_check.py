"""FH3 보조 — 펀딩 음수 코인의 현물 숏(마진 차입)이 지금 실제로 되는가. **조회만, 주문·차입 없음.**

FH3 결과: 정산을 1초만 걸치는 '선물 롱 + 현물 숏' 헤지는 수수료 전 건당 +0.44%(553/554건이 펀딩 음수).
현물 숏은 교차 마진에서 코인을 빌려 파는 것 → 극단 펀딩일 때 빌릴 물량이 있나, 이자가 얼마로 뛰나가 관건.

조회 항목(실서버 · 읽기 권한만 필요):
  · 지금 펀딩이 기준 이하(기본 −0.1%)인 USDT 무기한 → 기초 코인(1000PEPE → PEPE)
  · /sapi/v1/margin/allAssets            교차 마진 차입 가능 여부(isBorrowable)
  · /sapi/v1/margin/maxBorrowable        지금 빌릴 수 있는 수량(amount) · 계정 한도(borrowLimit)
        amount 는 마진 계정 담보에도 묶인다 → 담보 0 이면 0 이 정상. 몇 USDT 를 교차 마진 지갑에 옮겨 두면
        '담보로 계산한 값'과 비교해 풀에 물량이 있는지 보인다(풀이 비면 −3045 'system does not have enough asset').
  · /sapi/v1/margin/next-hourly-interest-rate   다음 1시간 이자(차입은 최소 1시간치 부과)
  · /sapi/v1/margin/interestRateHistory  최근 30일 일 이자 → 극단 펀딩 날에 이자가 뛰었나(같은 날 최저 펀딩과 나란히)

키: fh_probe 와 같은 BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET (.env 또는 환경변수).
    마진 엔드포인트가 −2015/권한 오류면 API 관리에서 그 키의 'Enable Reading' 만 있으면 되는지 확인(거래 권한 불필요).

    python research/fh_borrow_check.py                  # 지금 펀딩 ≤ −0.1% 인 코인들
    python research/fh_borrow_check.py --th=0.05        # 기준 −0.05%
    python research/fh_borrow_check.py --assets=KAVA,AXS,ONT   # 지정 코인(펀딩 무관)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAPI, FAPI = "https://api.binance.com", "https://fapi.binance.com"
MAX_ASSETS = 15
DAY = 86_400_000


def _arg(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def _env():
    vals = dict(os.environ)
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    k, s = vals.get("BINANCE_MAINNET_API_KEY"), vals.get("BINANCE_MAINNET_API_SECRET")
    if not k or not s:
        sys.exit("BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET 가 .env 나 환경변수에 없어요")
    return k, s.encode()


class Api:
    def __init__(self, key, secret):
        self.key, self.secret, self.offset = key, secret, 0

    def get(self, base, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000 + self.offset)
            params["recvWindow"] = 10000
        q = urllib.parse.urlencode(params)
        if signed:
            q += "&signature=" + hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"{base}{path}" + (f"?{q}" if q else ""),
                                     headers={"X-MBX-APIKEY": self.key, "User-Agent": "research-client/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path} → {e.code} {e.read()[:200].decode(errors='replace')}")

    def sync(self):
        t0 = time.time() * 1000
        st = self.get(SAPI, "/api/v3/time")["serverTime"]
        self.offset = st - (t0 + time.time() * 1000) / 2


def _base(sym):
    b = sym[:-4]
    for p in ("1000000", "1000"):
        if b.startswith(p) and len(b) > len(p):
            return b[len(p):], int(p)
    return b, 1


def candidates(api, th):
    prem = api.get(FAPI, "/fapi/v1/premiumIndex")
    info = {x["symbol"]: int(x.get("fundingIntervalHours", 8)) for x in api.get(FAPI, "/fapi/v1/fundingInfo")}
    out = {}
    for x in prem:
        s = x["symbol"]
        if not s.endswith("USDT"):
            continue
        r = float(x["lastFundingRate"])
        if r <= -th:
            a, mult = _base(s)
            if a not in out or r < out[a]["rate"]:
                out[a] = dict(asset=a, symbol=s, rate=r, interval=info.get(s, 8), mark=float(x["markPrice"]) / mult,
                              next=int(x["nextFundingTime"]))
    return sorted(out.values(), key=lambda c: c["rate"])


def main():
    api = Api(*_env())
    api.sync()
    th = float(_arg("th", "0.1")) / 100
    fixed = _arg("assets")
    cands = candidates(api, -1 if fixed else th)                       # 지정 시엔 전부 받아 고른다
    if fixed:
        want = [a.strip().upper() for a in fixed.split(",") if a.strip()]
        cands = [c for c in cands if c["asset"] in want] + \
                [dict(asset=a, symbol="-", rate=0.0, interval=0, mark=0.0, next=0)
                 for a in want if a not in {c["asset"] for c in cands}]
    if not cands:
        sys.exit(f"지금 펀딩 ≤ −{th*100:g}% 인 USDT 무기한이 없어요 (--th= 로 낮추거나 --assets= 로 지정)")

    try:
        acct = api.get(SAPI, "/sapi/v1/margin/account", signed=True)
    except RuntimeError as e:
        sys.exit(f"마진 계정 조회 실패: {e}\n   −2015 = 키 권한·IP 제한 · 마진 계정을 한 번도 안 열었으면 웹/앱에서 '마진' 활성화 필요")
    coll = float(acct.get("totalCollateralValueInUSDT") or 0)
    print(f"교차 마진 계정: 담보 ${coll:.2f} · 마진 레벨 {acct.get('marginLevel')} · 차입 허용 {acct.get('borrowEnabled')}")
    if coll < 1:
        print("   ⚠ 담보 0 → 아래 '지금 빌릴 수 있는 양'은 0 이 정상. 풀 물량을 보려면 교차 마진 지갑에 몇 USDT 를 옮긴 뒤 다시.")

    borrowable = {}
    try:
        for a in api.get(SAPI, "/sapi/v1/margin/allAssets"):
            borrowable[a["assetName"]] = bool(a.get("isBorrowable"))
    except RuntimeError as e:
        print(f"   allAssets 조회 실패({e}) — 차입 가능 여부 칸은 비워요")

    if borrowable:
        skip = [c["asset"] for c in cands if c["asset"] not in borrowable]
        if skip:
            print(f"   마진 미상장이라 뺌(현물 숏 불가): {', '.join(skip)}")
        cands = [c for c in cands if c["asset"] in borrowable]
    cands = cands[:MAX_ASSETS]
    names = [c["asset"] for c in cands]
    hourly = {}
    try:
        for x in api.get(SAPI, "/sapi/v1/margin/next-hourly-interest-rate",
                         {"assets": ",".join(n for n in names if borrowable.get(n, not borrowable)), "isIsolated": "FALSE"},
                         signed=True):
            hourly[x["asset"]] = float(x["nextHourlyInterestRate"])
    except RuntimeError as e:
        print(f"   다음 1시간 이자 조회 실패({e})")

    now = int(time.time() * 1000)
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M")
    print(f"\n{'코인':8} {'선물':14} {'펀딩/회':>9} {'주기':>4} {'다음':>5} {'마진':>4} {'지금 빌릴 수 있는 양':>22} "
          f"{'계정 한도':>12} {'1h 이자':>8} {'펀딩−1h이자':>10}")
    rows = []
    for c in cands:
        a = c["asset"]
        b = borrowable.get(a, False if borrowable else None)          # 목록에 없으면 마진 미상장(주식 토큰 등)
        amt = lim = None
        err = ""
        if b is not False:
            try:
                mb = api.get(SAPI, "/sapi/v1/margin/maxBorrowable", {"asset": a}, signed=True)
                amt, lim = float(mb.get("amount", 0)), float(mb.get("borrowLimit", 0) or 0)
            except RuntimeError as e:
                err = str(e).split("→", 1)[-1].strip()[:40]
            time.sleep(0.3)
        hr = hourly.get(a)
        usd = f"{amt:,.0f}개 (${amt*c['mark']:,.0f})" if amt is not None and c["mark"] else (f"{amt:,.0f}개" if amt is not None else err or "-")
        edge = abs(c["rate"]) - hr if hr is not None and c["rate"] else None
        print(f"{a:8} {c['symbol']:14} {c['rate']*100:+8.3f}% {c['interval']:>3}h {f(c['next']) if c['next'] else '-':>5} "
              f"{'✅' if b else ('❌' if b is False else '?'):>4} {usd:>22} {(f'{lim:,.0f}' if lim else '-'):>12} "
              f"{(f'{hr*100:.4f}%' if hr is not None else '-'):>8} {(f'{edge*100:+.3f}%' if edge is not None else '-'):>10}")
        rows.append(dict(c, borrowable=b, amount=amt, limit=lim, hourly=hr, err=err))

    print("\n■ 최근 30일: 일 이자율(사용자 VIP 기준) vs 같은 날 최저 펀딩 — 극단 펀딩 날 이자가 뛰었나")
    for r in rows:
        if r["borrowable"] is False or r["symbol"] == "-":
            continue
        try:
            hist = api.get(SAPI, "/sapi/v1/margin/interestRateHistory",
                           {"asset": r["asset"], "startTime": now - 29 * DAY, "endTime": now}, signed=True)
            fund = api.get(FAPI, "/fapi/v1/fundingRate", {"symbol": r["symbol"], "startTime": now - 30 * DAY,
                                                          "limit": 1000})
        except RuntimeError as e:
            print(f"   {r['asset']:8} 조회 실패 {e}")
            continue
        fmin = {}
        for x in fund:
            d = int(x["fundingTime"]) // DAY
            fmin[d] = min(fmin.get(d, 0.0), float(x["fundingRate"]))
        days = sorted({int(h["timestamp"]) // DAY: float(h["dailyInterestRate"]) for h in hist}.items())
        if not days:
            print(f"   {r['asset']:8} 이자 기록 없음")
            continue
        rates = [v for _, v in days]
        ext = sorted(((fmin.get(d, 0.0), v, d) for d, v in days))[:3]     # 펀딩 가장 극단인 3일
        cells = " · ".join(f"{datetime.fromtimestamp(d*86400, timezone.utc):%m-%d} 펀딩 {fr*100:+.2f}% 이자 {v*100:.3f}%/일"
                           for fr, v, d in ext)
        print(f"   {r['asset']:8} 일 이자 최저 {min(rates)*100:.3f}% · 중앙 {sorted(rates)[len(rates)//2]*100:.3f}% · "
              f"최고 {max(rates)*100:.3f}%   | 극단 펀딩 날: {cells}")
        time.sleep(0.2)

    os.makedirs(os.path.join(ROOT, "data", "fh_probe"), exist_ok=True)
    out = os.path.join(ROOT, "data", "fh_probe", f"borrow_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json")
    try:
        json.dump(dict(t=now, collateral=coll, rows=rows), open(out, "w"), ensure_ascii=False, indent=1)
    except OSError:
        out = os.path.expanduser(f"~/fh_probe_borrow_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json")
        json.dump(dict(t=now, collateral=coll, rows=rows), open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n저장 {out}")


if __name__ == "__main__":
    main()

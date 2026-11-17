"""FH 프로브 — 정산 직후 몇 ms 에 나가도 펀딩이 붙는가 · FUNDING_FEE 이벤트는 정산 후 언제 오나.

정산 한 번에 코인 여러 개를 최소 수량으로 '받는 쪽'에 잡고, 거래소 시각 기준 서로 다른 시점에 청산한다.
  대조군 T−1초(펀딩 없어야 정상) · T+50ms · +100ms · +200ms · +500ms · +1초 · +5초
청산은 거래소 시계로 맞춘다(서버 시각 오프셋 측정 + 왕복 지연 절반만큼 일찍 보냄). 실제 처리 시각은 주문 응답의
updateTime(거래소 시각)으로 기록한다. 사용자 데이터 스트림의 ACCOUNT_UPDATE(m=FUNDING_FEE) 의 E·T·받은 시각을 남기고,
끝나면 /fapi/v1/income(FUNDING_FEE) 로 실제로 붙은 펀딩을 대조한다.

네트워크:
  기본 = 테스트넷(전부 8h 주기, 펀딩 0.01% 안팎).
  --mainnet = 실서버. **실제 돈.** 포지션당 명목 상한 MAX_NOTIONAL(20 USDT), 대상은 그 정산에 |예상 펀딩| ≥ 0.01%(큰 순) 이고
              24h 거래대금 ≥ 1천만$ 인 USDT 무기한(1h·4h·8h 주기 모두). 진입 전에 그 심볼에 포지션이 있으면 거부.
키(.env 또는 환경변수):
  테스트넷 BINANCE_TESTNET_API_KEY/SECRET (또는 BINANCE_TESTNET=1 + BINANCE_API_KEY/SECRET)
  실서버   BINANCE_MAINNET_API_KEY/SECRET   — 선물 거래 권한만, 출금 권한 끔

    python research/fh_probe.py --mainnet --plan     # 다음 측정 정산·심볼·수량만(주문 없음, 키 없어도 됨)
    python research/fh_probe.py --mainnet --smoke    # 지금 바로 최소 1건 열고 3초 뒤 닫기(주문·청산·웹소켓 확인)
    python research/fh_probe.py --mainnet --go       # 다음 측정 정산에 실행(진입 T−30초, 끝나면 정리)
    python research/fh_probe.py --mainnet --go --delays=100,500,1000   # 다른 지연 조합
결과: data/fh_probe/<망>_<정산시각>.json + 요약 표.  (websocket-client 필요 — 저장소 의존성 아님)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MAINNET = "--mainnet" in sys.argv
REST = "https://fapi.binance.com" if MAINNET else "https://testnet.binancefuture.com"
WS = "wss://fstream.binance.com/ws/" if MAINNET else "wss://stream.binancefuture.com/ws/"
NET = "mainnet" if MAINNET else "testnet"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "fh_probe")
EXCLUDE = {"BTCUSDC"}                                   # 테스트넷 라이브 봇 심볼
DELAYS = (-1000, 50, 200, 1000, 100, 500, 5000)         # 심볼이 모자라면 앞에서부터(대조군·핵심 지연 우선)
for _a in sys.argv:                                     # --delays=100,500,1000 로 바꿔 돌릴 수 있다(정시마다 다른 지연)
    if _a.startswith("--delays="):
        DELAYS = tuple(int(x) for x in _a.split("=", 1)[1].split(","))
ENTRY_LEAD = 30_000
MAX_NOTIONAL = 20.0 if MAINNET else 100.0               # 실서버는 포지션당 20 USDT 상한
MIN_RATE = 0.0001 if MAINNET else 0.00001                # |펀딩| 큰 순으로 고르되, 심볼 수(지연 7종)를 채우려 0.01% 까지 허용
MIN_QVOL = 10_000_000 if MAINNET else 0
TESTNET_POOL = ("ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "LTCUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT",
                "BCHUSDT", "DOTUSDT", "TRXUSDT", "BTCUSDT", "NEARUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT", "UNIUSDT",
                "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "AAVEUSDT", "XLMUSDT")


def _env():
    vals = dict(os.environ)
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if MAINNET:
        k, s = vals.get("BINANCE_MAINNET_API_KEY"), vals.get("BINANCE_MAINNET_API_SECRET")
        name = "BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET"
    else:
        k, s = vals.get("BINANCE_TESTNET_API_KEY"), vals.get("BINANCE_TESTNET_API_SECRET")
        if (not k or not s) and str(vals.get("BINANCE_TESTNET", "")).lower() in ("1", "true", "yes"):
            k, s = vals.get("BINANCE_API_KEY"), vals.get("BINANCE_API_SECRET")
        name = "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"
    if not k or not s:
        if "--plan" in sys.argv:
            return "", ""
        sys.exit(f"{name} 가 .env 나 환경변수에 없어요")
    return k, s


class Api:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()
        self.offset = 0.0         # 서버시각 − 로컬시각(ms)
        self.rtt = 0.0

    def now(self):
        return time.time() * 1000 + self.offset

    def call(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(self.now())
            params["recvWindow"] = 5000
            q = urllib.parse.urlencode(params)
            q += "&signature=" + hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        else:
            q = urllib.parse.urlencode(params)
        url = f"{REST}{path}" + (f"?{q}" if q else "")
        req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": self.key,
                                                                  "User-Agent": "research-client/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} → {e.code} {e.read()[:300]!r}")

    def sync(self, n=15):
        best = None
        for _ in range(n):
            t0 = time.time() * 1000
            st = self.call("GET", "/fapi/v1/time")["serverTime"]
            t1 = time.time() * 1000
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, st - (t0 + t1) / 2)
            time.sleep(0.05)
        self.rtt, self.offset = best
        return best


def step_round_up(x, step):
    k = math.ceil(x / step - 1e-9)
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(k * step, d)


def _filters(info, s):
    f = {x["filterType"]: x for x in info[s]["filters"]}
    step = float(f["MARKET_LOT_SIZE"]["stepSize"]) if "MARKET_LOT_SIZE" in f else float(f["LOT_SIZE"]["stepSize"])
    return step, float(f["LOT_SIZE"]["minQty"]), float(f.get("MIN_NOTIONAL", {}).get("notional", 5))


def plan(api):
    info = {s["symbol"]: s for s in api.call("GET", "/fapi/v1/exchangeInfo")["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    prem = {p["symbol"]: p for p in api.call("GET", "/fapi/v1/premiumIndex") if p["symbol"] in info}
    ivl = {x["symbol"]: x.get("fundingIntervalHours", 8) for x in api.call("GET", "/fapi/v1/fundingInfo")}
    qv = {}
    if MAINNET:
        qv = {t["symbol"]: float(t["quoteVolume"]) for t in api.call("GET", "/fapi/v1/ticker/24hr")}
    pool = list(prem) if MAINNET else [s for s in TESTNET_POOL if s in prem]
    cand = {}
    now = api.now()
    for s in pool:
        if s in EXCLUDE:
            continue
        p = prem[s]
        T = int(p["nextFundingTime"])
        rate = float(p["lastFundingRate"])
        if T - now < ENTRY_LEAD + 60_000 or abs(rate) < MIN_RATE or qv.get(s, 1e18) < MIN_QVOL:
            continue
        step, minq, mn = _filters(info, s)
        px = float(p["markPrice"])
        target = max(mn * 1.2, 6.0) if MAINNET else max(MAX_NOTIONAL, mn * 1.3)
        if target > MAX_NOTIONAL:
            continue
        qty = step_round_up(max(minq, target / px), step)
        if qty * px > MAX_NOTIONAL * 1.05:
            continue
        cand.setdefault(T, []).append(dict(symbol=s, qty=qty, mark=px, rate=rate, interval=ivl.get(s, 8),
                                           qvol=qv.get(s), side="SELL" if rate > 0 else "BUY"))
    if not cand:
        return None, []
    # 가장 이른 정산 중 후보가 3개 이상인 것(없으면 가장 많은 것)
    Ts = sorted(cand)
    T = next((t for t in Ts if len(cand[t]) >= 3), max(Ts, key=lambda t: len(cand[t])))
    picks = sorted(cand[T], key=lambda p: -abs(p["rate"]))[:len(DELAYS)]
    for p, d in zip(picks, DELAYS):
        p["delay"] = d
    return T, picks


def _order(api, dual, symbol, side, qty, close=False, pos_side=None):
    params = dict(symbol=symbol, side=side, type="MARKET", quantity=qty, newOrderRespType="RESULT")
    if dual:
        params["positionSide"] = pos_side
    elif close:
        params["reduceOnly"] = "true"
    return api.call("POST", "/fapi/v1/order", params, signed=True)


def _listen(api, lk, until, events, errs):
    import websocket
    try:
        ws = websocket.create_connection(WS + lk, timeout=5)
        while api.now() < until:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            events.append((api.now(), json.loads(raw)))
        ws.close()
    except Exception as e:      # noqa: BLE001
        errs.append(repr(e))


def _positions(api):
    return {x["symbol"]: x for x in api.call("GET", "/fapi/v2/positionRisk", signed=True)}


def smoke(api, dual):
    """지금 바로 최소 1건 열고 3초 뒤 닫기 — 서명·주문·청산·웹소켓·정리 확인."""
    s = "DOGEUSDT"
    info = {x["symbol"]: x for x in api.call("GET", "/fapi/v1/exchangeInfo")["symbols"]}
    step, minq, mn = _filters(info, s)
    px = float(api.call("GET", "/fapi/v1/premiumIndex", dict(symbol=s))["markPrice"])
    qty = step_round_up(max(minq, max(mn * 1.2, 6.0) / px), step)
    if abs(float(_positions(api).get(s, {}).get("positionAmt", 0))) > 0:
        sys.exit(f"{s} 에 이미 포지션이 있어요 — 스모크 중단")
    events, errs = [], []
    lk = api.call("POST", "/fapi/v1/listenKey")["listenKey"]
    th = threading.Thread(target=_listen, args=(api, lk, api.now() + 12_000, events, errs), daemon=True)
    th.start()
    time.sleep(1.5)
    print(f"스모크({NET}): {s} 롱 {qty} (≈${qty*px:.2f}) 열기 → 3초 → 닫기")
    a = _order(api, dual, s, "BUY", qty, pos_side="LONG")
    time.sleep(3)
    b = _order(api, dual, s, "SELL", qty, close=True, pos_side="LONG")
    th.join()
    left = abs(float(_positions(api).get(s, {}).get("positionAmt", 0)))
    kinds = sorted({(e.get("e"), (e.get("a") or {}).get("m") or (e.get("o") or {}).get("X")) for _, e in events})
    print(f"   진입 {a.get('status')} @ {a.get('avgPrice')} · 청산 {b.get('status')} @ {b.get('avgPrice')} · 남은 수량 {left}")
    print(f"   웹소켓 이벤트 {len(events)}개: {kinds}" + (f" · 오류 {errs[0][:100]}" if errs else ""))
    print("   ✅ 통과" if a.get("status") == "FILLED" and b.get("status") == "FILLED" and left == 0 and events and not errs
          else "   ⚠ 확인 필요")


def main():
    key, secret = _env()
    api = Api(key, secret)
    rtt, off = api.sync()
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{NET}] 서버 시계 오프셋 {off:+.1f}ms · 왕복 {rtt:.1f}ms")
    dual = None
    if key:
        try:
            dual = api.call("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition", False)
            bal = [b for b in api.call("GET", "/fapi/v2/balance", signed=True) if float(b.get("balance", 0)) > 0]
            print(f"키: ✅ 확인됨 · 헤지 모드={dual} · 선물 잔고 "
                  + (", ".join(f"{b['asset']} {float(b['balance']):.2f}" for b in bal) or "없음(입금 전)"))
        except RuntimeError as e:
            msg = f"키: ❌ {e}\n   -2015 = 키·IP 제한·선물 권한 확인 · -1021 = 컴퓨터 시계 동기화"
            if "--plan" not in sys.argv:
                sys.exit(msg)
            print(msg + "\n   (--plan 은 공개 API 만 쓰므로 계속 — 주문은 키가 허용된 서버에서)")
    else:
        print("키: 없음 — 공개 API 로 계획만 봐요(.env 의 키가 안 읽혔어요)")
    if "--smoke" in sys.argv:
        return smoke(api, dual)

    T, picks = plan(api)
    if not picks:
        sys.exit("조건에 맞는 정산이 없어요(가까운 정산에 |펀딩| 기준을 넘는 심볼 없음)")
    print(f"측정 정산 {f(T)} (지금부터 {(T - api.now())/60000:.1f}분) · 심볼 {len(picks)}개")
    for p in picks:
        print(f"   {p['symbol']:13} {p['interval']}h 주기 · 청산 T{p['delay']:+6d}ms · {p['side']:4} {p['qty']} "
              f"(≈${p['qty']*p['mark']:.1f}) · 예상 펀딩 {p['rate']*100:+.4f}%"
              + (f" · 24h ${p['qvol']/1e6:.0f}M" if p.get("qvol") else ""))
    if "--plan" in sys.argv:
        return
    if "--go" not in sys.argv:
        sys.exit("실행하려면 --go 를 붙이세요(--plan 으로 먼저 확인)")

    pos = _positions(api)
    busy = [p["symbol"] for p in picks if abs(float(pos.get(p["symbol"], {}).get("positionAmt", 0))) > 0]
    if busy:
        sys.exit(f"이미 포지션이 있는 심볼이 있어요: {busy} — 중단")
    wait = T - ENTRY_LEAD - api.now()
    print(f"T−30초까지 {wait/1000:.0f}초 대기… (헤지 모드={dual})", flush=True)

    events, ws_err = [], []
    time.sleep(max(0, (wait - 60_000) / 1000))           # listenKey 는 60분 유효 — 진입 1분 전에 연다
    lk = api.call("POST", "/fapi/v1/listenKey")["listenKey"]
    th = threading.Thread(target=_listen, args=(api, lk, T + 120_000, events, ws_err), daemon=True)
    th.start()
    time.sleep(max(0, (T - ENTRY_LEAD - api.now()) / 1000))
    api.sync(5)
    for p in picks:
        r = _order(api, dual, p["symbol"], p["side"], p["qty"], pos_side="SHORT" if p["side"] == "SELL" else "LONG")
        p["entry"] = dict(updateTime=(r.get("updateTime") or 0) - T, avgPrice=r.get("avgPrice"), status=r.get("status"))
    print(f"진입 완료 {len(picks)}개 · 정산까지 {(T - api.now())/1000:.1f}초", flush=True)
    api.sync(5)

    def close(p):
        target = T + p["delay"] - api.rtt / 2
        while api.now() < target - 5:
            time.sleep(0.001)
        while api.now() < target:
            pass
        sent = api.now()
        try:
            r = _order(api, dual, p["symbol"], "BUY" if p["side"] == "SELL" else "SELL", p["qty"], close=True,
                       pos_side="SHORT" if p["side"] == "SELL" else "LONG")
            p["exit"] = dict(sent=sent - T, updateTime=(r.get("updateTime") or 0) - T, avgPrice=r.get("avgPrice"),
                             status=r.get("status"), recv=api.now() - T)
        except Exception as e:      # noqa: BLE001
            p["exit"] = dict(sent=sent - T, error=repr(e))
    ths = [threading.Thread(target=close, args=(p,)) for p in picks]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    print("청산 완료 · 정산 처리·이벤트 대기 90초…", flush=True)
    time.sleep(90)
    inc = api.call("GET", "/fapi/v1/income", dict(incomeType="FUNDING_FEE", startTime=T - 300_000,
                                                  endTime=T + 300_000, limit=1000), signed=True)
    pos = _positions(api)
    left = []
    for p in picks:                                     # 남은 포지션 정리(안전)
        x = pos.get(p["symbol"], {})
        amt = float(x.get("positionAmt", 0))
        if abs(amt) > 0:
            left.append(p["symbol"])
            _order(api, dual, p["symbol"], "SELL" if amt > 0 else "BUY", abs(amt), close=True,
                   pos_side=x.get("positionSide", "BOTH"))

    fee_ev = []
    for rcv, e in events:
        if e.get("e") == "ACCOUNT_UPDATE" and e.get("a", {}).get("m") == "FUNDING_FEE":
            fee_ev.append(dict(recv=rcv - T, E=e.get("E", 0) - T, T=e.get("T", 0) - T,
                               symbol=e["a"].get("S"), balances=e["a"].get("B"), positions=e["a"].get("P")))
    got = {}
    for x in inc:
        got.setdefault(x["symbol"], []).append(dict(income=x["income"], time=int(x["time"]) - T))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{NET}_{datetime.fromtimestamp(T/1000, timezone.utc):%Y%m%d_%H%M}.json")
    json.dump(dict(net=NET, T=T, offset=api.offset, rtt=api.rtt, dual=dual, picks=picks, income=got,
                   funding_events=fee_ev, all_events=[(r - T, e) for r, e in events], ws_error=ws_err,
                   leftover_closed=left), open(path, "w"), ensure_ascii=False, indent=1)

    print(f"\n결과 {path}")
    print(f"   {'심볼':13} {'주기':>3} {'목표':>7} {'보냄':>7} {'처리':>7} {'펀딩 입금':>14} {'입금 시각':>9}")
    for p in picks:
        ex = p.get("exit", {})
        g = got.get(p["symbol"])
        print(f"   {p['symbol']:13} {p['interval']:>2}h {p['delay']:+7d} {ex.get('sent', float('nan')):+7.0f} "
              f"{ex.get('updateTime', float('nan')):+7.0f} {(g[0]['income'] if g else '없음'):>14} "
              f"{(str(g[0]['time']) + 'ms') if g else '':>9}" + (f"  ⚠ {ex['error'][:60]}" if ex.get("error") else ""))
    print(f"   FUNDING_FEE 이벤트 {len(fee_ev)}개: " +
          ", ".join(f"{x['symbol']} 처리 T{x['T']:+.0f}ms·도착 T{x['recv']:+.0f}ms" for x in fee_ev[:10]))
    if ws_err:
        print(f"   ⚠ 웹소켓 오류: {ws_err[0][:120]}")
    if left:
        print(f"   ⚠ 남은 포지션 정리함: {left}")


if __name__ == "__main__":
    main()

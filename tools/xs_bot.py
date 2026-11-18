"""XS MOM 실매매 봇 — 주 1회 알트 횡단면 모멘텀 롱숏 리밸런스(research/XS_MOM.md §5).

기존 트레이더(engine.live)와 별개다. 그쪽은 '심볼 하나 · 봉마다 조건 판정 · 포지션 하나'이고, 이건 '주 1회 · 30개 내외
포트폴리오 리밸런스'라 구조가 다르다. 겹치는 건 거래소 계정뿐 → 이 봇은 **자기가 연 심볼만** 만진다.

규칙(바꾸지 말 것 — exp_XS_cross_section 사전 고정 · xs_forward 와 같은 함수로 목록 산출):
  화 00:00 UTC 이후 1회. 월요일 일봉 종가 기준 28일 수익률 상위 10% 롱 · 하위 10% 숏 · 동일 가중.
  목록은 전진 기록 장부(xs_forward_book.jsonl)에 이번 주 기록이 있으면 그걸 그대로 쓰고, 없으면 xs_forward.signal() 로 계산.

사이징: 자본(= 선물 지갑 USDT 마진 잔고, --capital 로 상한) × 총 노출(--gross, 기본 1, 상한 2) ÷ 포지션 수.
  포지션당 $5 미만(또는 심볼 최소 주문 금액 미만이 과반)이면 경고하고 그 주는 쉰다.
순서: 빠진 코인 청산 → 남는 코인 비중 맞춤 → 새 코인 진입. 전부 시장가. 체결가·수수료·기대가(신호 종가) 기록.
손실 한도: 시작 자본 대비 −30%(--max-loss)면 전량 청산·정지(state 의 halted). --check 를 매시간 cron 으로.
안전: 실주문은 --mainnet --go 둘 다 있어야 · 헤지 모드면 중단 · 다른 포지션이 있는 심볼은 건드리지 않고 뺀다 ·
  BTCUSDC 제외 · 심볼별 교차 마진 + 레버리지 min(5, 심볼 최대) · 키 출력 안 함.
⚠ 손실 한도는 계정 전체 마진 잔고로 판단한다 → 같은 계정에서 다른 실험이 돌면 그 손익도 섞인다.

    python tools/xs_bot.py --mainnet --plan            # 이번 주 목표·주문 미리보기(주문 없음)
    python tools/xs_bot.py --mainnet --go              # 리밸런스 실행
    python tools/xs_bot.py --mainnet --check           # 손실 한도 점검(한도 넘으면 청산·정지) — 매시간
    python tools/xs_bot.py --mainnet --status          # 이 봇 포지션·손익
    python tools/xs_bot.py --mainnet --flatten         # 이 봇 포지션 전량 청산·정지(수동)
    python tools/xs_bot.py --mainnet --resume          # 정지 해제(다음 --go 부터 다시 매매)
  옵션: --gross=1(총 노출, ≤2) · --capital=200(자본 상한 USDT) · --max-loss=0.30 · --top=N(체결 확인용: 양쪽 상위 N개만 — 규칙 아님)
  cron(EC2, UTC):
    15 0 * * 2  cd ~/auto_trading && ~/fh_venv/bin/python tools/xs_bot.py --mainnet --go    >> ~/xs_bot.log 2>&1
    30 * * * *  cd ~/auto_trading && ~/fh_venv/bin/python tools/xs_bot.py --mainnet --check >> ~/xs_bot.log 2>&1
상태·기록: ~/xs_bot/state.json · ~/xs_bot/orders.jsonl (서버 data/ 는 root 소유라 홈).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REST = "https://fapi.binance.com"
HOME = os.environ.get("XS_BOT_DIR") or os.path.expanduser("~/xs_bot")
STATE = os.path.join(HOME, "state.json")
ORDERS = os.path.join(HOME, "orders.jsonl")
EXCLUDE = {"BTCUSDC"}
MIN_POS = 5.0
LEVERAGE = 5
D = 86_400_000


def _arg(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── 거래소 ──────────────────────────────────────────────────────────────────
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
    return k, s


class ApiError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code


class Api:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()
        self.offset = 0.0

    def sync(self):
        best = None
        for _ in range(3):
            t0 = time.time() * 1000
            st = self.call("GET", "/fapi/v1/time")["serverTime"]
            t1 = time.time() * 1000
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, st - (t0 + t1) / 2)
        self.offset = best[1]

    def call(self, method, path, params=None, signed=False, retries=3):
        for i in range(retries):
            p = dict(params or {})
            if signed:
                p["timestamp"] = int(time.time() * 1000 + self.offset)
                p["recvWindow"] = 10000
            q = urllib.parse.urlencode(p)
            if signed:
                q += "&signature=" + hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(f"{REST}{path}" + (f"?{q}" if q else ""), method=method,
                                         headers={"X-MBX-APIKEY": self.key, "User-Agent": "xs-bot/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read()[:300]
                try:
                    j = json.loads(body)
                    code, msg = j.get("code"), j.get("msg")
                except ValueError:
                    code, msg = e.code, body
                if code in (-1021, -1001, -1007) and i < retries - 1:      # 시계·서버 지연 → 다시
                    self.sync()
                    time.sleep(1)
                    continue
                if e.code in (418, 429) and i < retries - 1:
                    time.sleep(30)
                    continue
                raise ApiError(code, msg)
            except urllib.error.URLError:
                if i == retries - 1:
                    raise
                time.sleep(2)


def _fmt_qty(q, step):
    d = Decimal(str(step)).normalize()
    places = max(0, -d.as_tuple().exponent)
    return f"{q:.{places}f}"


def floor_step(x, step):
    return math.floor(x / step + 1e-9) * step


class Market:
    """exchangeInfo·가격 캐시."""

    def __init__(self, api):
        self.api = api
        info = api.call("GET", "/fapi/v1/exchangeInfo")
        self.sym = {}
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            self.sym[s["symbol"]] = dict(status=s.get("status"), step=float(lot["stepSize"]),
                                         minq=float(f["LOT_SIZE"]["minQty"]),
                                         maxq=float(lot.get("maxQty") or 1e18),
                                         min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)))
        self.mark = {x["symbol"]: float(x["markPrice"]) for x in api.call("GET", "/fapi/v1/premiumIndex")}


def account(api):
    acc = api.call("GET", "/fapi/v2/account", signed=True)
    pos = {p["symbol"]: float(p["positionAmt"]) for p in acc["positions"] if float(p["positionAmt"])}
    usdt = next((a for a in acc["assets"] if a["asset"] == "USDT"), {})
    return dict(wallet=float(usdt.get("walletBalance", 0)), margin=float(usdt.get("marginBalance", 0)),
                total_margin=float(acc.get("totalMarginBalance", 0)), positions=pos)


# ── 상태·기록 ──────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return dict(start_equity=None, halted=False, week=None, positions={})


def save_state(st):
    os.makedirs(HOME, exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def log_order(rec):
    os.makedirs(HOME, exist_ok=True)
    with open(ORDERS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 목록 ──────────────────────────────────────────────────────────────────
def this_week_signal():
    """전진 기록 장부의 이번 주 기록 → 없으면 같은 함수로 계산. (월요일 봉 시각, 롱{심볼:신호종가}, 숏{...}, 출처)"""
    from research import xs_forward as xf
    o = xf.last_monday_bar(int(time.time() * 1000))
    for rec in reversed(xf._read()):
        if rec.get("t") == o and rec.get("long") is not None:
            return o, rec["long"], rec["short"], "장부"
    sig = xf.signal(o)
    return o, sig["long"], sig["short"], "계산"


# ── 계획 ──────────────────────────────────────────────────────────────────
def plan(mk, acct, st, long, short, capital_cap, gross):
    """목표 수량과 주문 목록. 반환 dict(orders, targets, per, skipped, warn)."""
    own = dict(st.get("positions") or {})
    foreign = {s for s, q in acct["positions"].items() if s not in own}
    want = {s: 1 for s in long} | {s: -1 for s in short}
    skipped = {}
    for s in list(want):
        why = None
        if s in EXCLUDE:
            why = "제외 심볼"
        elif s in foreign:
            why = "다른 포지션이 있음"
        elif s not in mk.sym or mk.sym[s]["status"] != "TRADING" or s not in mk.mark:
            why = "거래 불가"
        if why:
            skipped[s] = why
            del want[s]
    capital = min(acct["margin"], capital_cap) if capital_cap else acct["margin"]
    n = len(want)
    per = capital * gross / n if n else 0.0
    warn = None
    if n == 0:
        warn = "목표 포지션이 없어요"
    elif per < MIN_POS:
        warn = f"포지션당 ${per:.2f} < ${MIN_POS:.0f} — 자본 ${capital:.2f} · {n}개 · 총 노출 {gross:g}배. 이번 주 쉼"
    else:
        small = [s for s in want if per < mk.sym[s]["min_notional"]]
        if len(small) * 2 > n:
            warn = f"최소 주문 금액에 못 미치는 심볼이 {len(small)}/{n}개 — 이번 주 쉼"
    targets = {}
    if not warn:
        for s, sgn in want.items():
            m = mk.sym[s]
            q = floor_step(per / mk.mark[s], m["step"])
            if q < m["minq"] or q * mk.mark[s] < m["min_notional"]:
                skipped[s] = f"수량이 최소 단위 미만(${per:.2f})"
                continue
            targets[s] = sgn * min(q, m["maxq"])
    # 주문: ① 빠진 것 청산 ② 남는 것 비중 맞춤 ③ 새 진입. 경고(쉼)면 빠진 것도 안 건드린다(그 주 통째로 쉼).
    orders = []
    if not warn:
        live_own = {s: acct["positions"].get(s, 0.0) for s in own}          # 실제 수량(수동 개입 반영)
        for s, cur in live_own.items():
            if cur and s not in targets:
                orders.append(dict(stage="청산", symbol=s, delta=-cur, target=0.0))
        for s, tgt in targets.items():
            cur = live_own.get(s, 0.0)
            delta = tgt - cur
            if not delta:
                continue
            m = mk.sym[s]
            reduce = cur != 0 and abs(tgt) < abs(cur) and tgt * cur >= 0
            # 늘리는 주문은 최소 주문 금액 이상이어야 · 작은 조정(목표의 10% 미만)은 회전 비용만 든다 → 건너뜀
            if not reduce and abs(delta) * mk.mark[s] < m["min_notional"]:
                continue
            if cur and abs(delta) < 0.10 * abs(tgt):
                continue
            orders.append(dict(stage="조정" if cur else "진입", symbol=s, delta=delta, target=tgt, reduce=reduce))
        rank = {"청산": 0, "조정": 1, "진입": 2}
        orders.sort(key=lambda o: (rank[o["stage"]], not o.get("reduce", True)))
    return dict(orders=orders, targets=targets, per=per, capital=capital, skipped=skipped, warn=warn)


# ── 실행 ──────────────────────────────────────────────────────────────────
def prepare_symbol(api, s):
    try:
        api.call("POST", "/fapi/v1/marginType", dict(symbol=s, marginType="CROSSED"), signed=True)
    except ApiError as e:
        if e.code != -4046:                                # 이미 교차
            raise
    br = api.call("GET", "/fapi/v1/leverageBracket", dict(symbol=s), signed=True)[0]["brackets"]
    api.call("POST", "/fapi/v1/leverage",
             dict(symbol=s, leverage=min(LEVERAGE, max(int(b["initialLeverage"]) for b in br))), signed=True)


def market_order(api, mk, s, delta, reduce, ctx):
    m = mk.sym[s]
    qty = abs(delta)
    params = dict(symbol=s, side="BUY" if delta > 0 else "SELL", type="MARKET",
                  quantity=_fmt_qty(qty, m["step"]), newOrderRespType="RESULT")
    if reduce:
        params["reduceOnly"] = "true"
    t0 = int(time.time() * 1000)
    r = api.call("POST", "/fapi/v1/order", params, signed=True)
    oid = r.get("orderId")
    avg, fee, fee_asset = float(r.get("avgPrice") or 0), 0.0, None
    for i in range(5):                                     # 체결가·수수료는 체결 내역에서
        try:
            tr = api.call("GET", "/fapi/v1/userTrades", dict(symbol=s, orderId=oid), signed=True)
        except ApiError:
            tr = []
        if tr:
            qn = sum(float(x["qty"]) for x in tr)
            avg = sum(float(x["price"]) * float(x["qty"]) for x in tr) / qn if qn else avg
            fee = sum(float(x["commission"]) for x in tr)
            fee_asset = tr[0].get("commissionAsset")
            break
        time.sleep(0.5 * (i + 1))
    rec = dict(t=t0, at=_now(), symbol=s, side=params["side"], qty=float(params["quantity"]), reduce=reduce,
               orderId=oid, status=r.get("status"), avgPrice=avg, fee=fee, feeAsset=fee_asset, **ctx)
    if ctx.get("expect") and avg:
        sgn = 1 if delta > 0 else -1
        rec["slip_bp"] = round(sgn * (avg / ctx["expect"] - 1) * 1e4, 1)     # +면 불리(비싸게 삼·싸게 팖)
    log_order(rec)
    return rec


def flatten(api, mk, st, why):
    acct = account(api)
    done = []
    for s in list(st.get("positions") or {}):
        cur = acct["positions"].get(s, 0.0)
        if cur:
            try:
                market_order(api, mk, s, -cur, True, dict(stage="전량청산", why=why))
                done.append(s)
            except ApiError as e:
                print(f"   ⚠ {s} 청산 실패: {e}", flush=True)
                continue
        st["positions"].pop(s, None)
    save_state(st)
    return done


def rebalance(api, go, capital_cap, gross):
    st = load_state()
    if st.get("halted"):
        print(f"[{_now()}] 정지 상태 — 매매 안 함(해제: --resume). 사유: {st.get('halt_reason')}")
        return
    if api.call("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"):
        sys.exit("헤지 모드 계정이에요 — 단방향 모드에서만 동작")
    o, long, short, src = this_week_signal()
    top = _arg("top")
    if top:                                                # 체결 확인용 축소 — 규칙과 다르다
        n = int(top)
        long = dict(list(long.items())[:n])
        short = dict(list(short.items())[-n:])
    mk = Market(api)
    acct = account(api)
    p = plan(mk, acct, st, long, short, capital_cap, gross)
    wk = datetime.fromtimestamp(o / 1000, timezone.utc).strftime("%Y-%m-%d")
    print(f"[{_now()}] XS MOM {'실행' if go else '미리보기'} · 월요일 봉 {wk}({src}) · 롱 {len(long)} · 숏 {len(short)}"
          + (f" · ⚠ --top={top}(규칙 아님)" if top else ""))
    print(f"   자본 ${p['capital']:.2f}(마진 잔고 ${acct['margin']:.2f}{f' · 상한 ${capital_cap:g}' if capital_cap else ''})"
          f" · 총 노출 {gross:g}배 · 포지션당 ${p['per']:.2f} · 목표 {len(p['targets'])}개")
    for s, why in p["skipped"].items():
        print(f"   뺌 {s}: {why}")
    if p["warn"]:
        print(f"   ⚠ {p['warn']}")
        return
    if st.get("week") == o and not p["orders"]:
        print("   이번 주 리밸런스 이미 끝남")
        return
    for od in p["orders"]:
        print(f"   {od['stage']:2} {od['symbol']:16} {od['delta']:+.6g} → 목표 {od['target']:+.6g}"
              f" (≈${abs(od['delta']) * mk.mark[od['symbol']]:.2f})")
    if not go:
        print("   (--plan: 주문 없음)")
        return
    if st.get("start_equity") is None:
        st["start_equity"] = acct["total_margin"]
        st["started"] = _now()
    expect = {**long, **short}
    fails = []
    for od in p["orders"]:
        s = od["symbol"]
        try:
            if od["stage"] == "진입":
                prepare_symbol(api, s)
            market_order(api, mk, s, od["delta"], od.get("reduce", od["stage"] == "청산"),
                         dict(stage=od["stage"], week=o, target=od["target"], expect=expect.get(s)))
            if od["target"]:
                st["positions"][s] = od["target"]
            else:
                st["positions"].pop(s, None)
        except ApiError as e:
            fails.append((s, str(e)))
            print(f"   ⚠ {s} {od['stage']} 실패: {e}", flush=True)
        save_state(st)                                     # 주문마다 저장 — 중간에 죽어도 자기 포지션을 안다
    # 실제 수량으로 동기화(부분 체결·실패 반영)
    acct = account(api)
    st["positions"] = {s: acct["positions"][s] for s in set(st["positions"]) | set(p["targets"]) if acct["positions"].get(s)}
    st["week"] = o
    save_state(st)
    print(f"   완료 · 이 봇 포지션 {len(st['positions'])}개 · 실패 {len(fails)}" + (f": {fails}" if fails else ""))


def check(api, max_loss):
    st = load_state()
    if st.get("halted") or not st.get("positions") or not st.get("start_equity"):
        return
    acct = account(api)
    eq, base = acct["total_margin"], st["start_equity"]
    dd = eq / base - 1
    if dd <= -max_loss:
        print(f"[{_now()}] ⛔ 손실 한도: 시작 ${base:.2f} → ${eq:.2f} ({dd:+.1%}) ≤ −{max_loss:.0%} → 전량 청산·정지", flush=True)
        mk = Market(api)
        done = flatten(api, mk, st, "손실 한도")
        st["halted"], st["halt_reason"] = True, f"손실 한도 {dd:+.1%} ({_now()})"
        save_state(st)
        print(f"   청산 {len(done)}개", flush=True)


def status(api):
    st = load_state()
    acct = account(api)
    mk = Market(api)
    base = st.get("start_equity")
    print(f"[{_now()}] 정지={st.get('halted')} · 마지막 리밸런스 주 "
          f"{datetime.fromtimestamp(st['week'] / 1000, timezone.utc):%Y-%m-%d}" if st.get("week") else
          f"[{_now()}] 정지={st.get('halted')} · 리밸런스 기록 없음")
    print(f"   계정 마진 잔고 ${acct['total_margin']:.2f}"
          + (f" · 시작 ${base:.2f} ({acct['total_margin'] / base - 1:+.1%})" if base else ""))
    gl = gs = 0.0
    for s, q in sorted((st.get("positions") or {}).items()):
        cur = acct["positions"].get(s, 0.0)
        v = cur * mk.mark.get(s, 0)
        gl += max(v, 0)
        gs += max(-v, 0)
        print(f"   {s:16} 기록 {q:+.6g} · 실제 {cur:+.6g} · ${v:+.2f}")
    print(f"   롱 ${gl:.2f} · 숏 ${gs:.2f}")


def main():
    if "--mainnet" not in sys.argv:
        sys.exit("실서버 전용이에요: --mainnet 을 붙여 주세요")
    gross = float(_arg("gross", "1"))
    if not 0 < gross <= 2:
        sys.exit("--gross 는 0~2 사이(문서: 1배 적정, 2배 상한)")
    cap = float(_arg("capital")) if _arg("capital") else None
    max_loss = float(_arg("max-loss", "0.30"))
    api = Api(*_env())
    api.sync()
    if "--check" in sys.argv:
        return check(api, max_loss)
    if "--status" in sys.argv:
        return status(api)
    if "--flatten" in sys.argv:
        st = load_state()
        done = flatten(api, Market(api), st, "수동")
        st["halted"], st["halt_reason"] = True, f"수동 청산 ({_now()})"
        save_state(st)
        print(f"청산 {len(done)}개 · 정지(해제: --resume)")
        return
    if "--resume" in sys.argv:
        st = load_state()
        st["halted"], st["halt_reason"] = False, None
        st["start_equity"] = None if not st.get("positions") else st.get("start_equity")
        save_state(st)
        print("정지 해제")
        return
    go = "--go" in sys.argv
    if not go and "--plan" not in sys.argv:
        sys.exit("--plan(미리보기) 또는 --go(실행)를 붙여 주세요")
    rebalance(api, go, cap, gross)


if __name__ == "__main__":
    main()

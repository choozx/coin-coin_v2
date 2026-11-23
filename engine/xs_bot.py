"""XS MOM 실매매 서비스 — 알트 횡단면 모멘텀 롱숏 리밸런스(research/XS_MOM.md §5).

멀티서비스 구조의 한 컨테이너(docker-compose `xsbot`). 트레이더(engine.live)와 별개다: 그쪽은 '심볼 하나 · 봉마다 판정 ·
포지션 하나'이고 이건 '30개 내외 포트폴리오'. 겹치는 건 거래소 계정뿐 → **자기가 연 심볼만** 만진다.

규칙(바꾸지 말 것): engine.xs_mom.signal() — research/xs_forward.py(종이 전진 기록)와 같은 함수.
몫(XSD): 자금을 7몫으로 기록한다. 몫마다 요일·마지막 교체 봉·코인별 수량. 거래소엔 코인별 합계 한 포지션.
  월요일 하나(기본) = 7몫 모두 월요일 → 화 00:15 UTC 에 7몫이 한꺼번에 교체 = 예전 '주 1회 리밸런스'와 같은 포지션.
  요일 분산(--split-on --yes) = 몫 k 가 요일 k(0=월) 종가 + 1일 00:15 UTC 에 자기 몫만 교체 → 매일 1/7.
    켜는 순간엔 주문 없음(지금 포지션을 7몫이 그대로 든 채로 시작) · 7일에 걸쳐 저절로 분산된다.
    켜기는 한 번뿐 — 요청 파일($XS_DIR/split_on)을 서비스가 다음 바퀴에 읽고 state.json 에 split_since 를 남긴다. 끄는 기능은 없다.
    (.env 설정이 아닌 이유: 컨테이너 환경변수는 docker-compose.yml 로만 들어오는데, 그 파일을 고치면 배포 때 전 서비스가 재시작된다.)
사이징: 자본(USDT 마진 잔고, XS_CAPITAL 로 상한) × 총 노출(XS_GROSS, 기본 1 · 상한 2) ÷ 포지션 수 × (교체하는 몫 수 ÷ 7).
  그 금액이 $5 미만(또는 최소 주문 금액 미만이 과반)이면 경고하고 그 몫(들)은 쉰다(예전 수량 유지).
순서: 빠진 코인 청산 → 남는 코인 비중 맞춤 → 새 코인 진입. 시장가. 체결가·수수료·신호 종가 대비 슬리피지 기록.
손실 한도: 시작 자본 대비 −XS_MAX_LOSS(기본 30%)면 이 봇 포지션 전량 청산·정지. 매시간 점검.
안전: XS_MODE=live 일 때만 실주문(기본 plan = 목록·주문 계획만 대시보드에) · 헤지 모드면 중단 ·
  다른 포지션이 있는 심볼·BTCUSDC 는 뺀다 · 심볼별 교차 마진 + 레버리지 min(5, 심볼 최대) · 컨테이너가 재시작돼도 청산하지 않는다.
⚠ 손실 한도는 계정 전체 마진 잔고 기준 → 같은 메인넷 계정에서 다른 실험이 돌면 그 손익도 섞인다.

서비스:  python -m engine.xs_bot --loop                (XS_MODE=off|plan|live)
분산:    python -m engine.xs_bot --split-on --yes     요일 분산으로 바꾸기(한 번 · 되돌리지 않음). --yes 없으면 설명만
수동:    python -m engine.xs_bot --plan | --go | --check | --status | --flatten | --resume   [--top=N 체결 확인용 · 규칙 아님]
대조:    python -m engine.xs_bot --compare   주별 실매매 ↔ 규칙(월요일 종가 체결) 차이 = 체결 지연·슬리피지·수수료·수량 차이.
         live 면 화요일 리밸런스 뒤 지난주 확정분을 #매매 에 자동으로 올린다.
상태:    $XS_DIR(기본 data/xs)/state.json · orders.jsonl(호가 bid/ask 포함) · weeks.jsonl(주별 목표) · status.json(대시보드가 읽음)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from . import xs_mom
from .notifier import notify

REST = "https://fapi.binance.com"
EXCLUDE = {"BTCUSDC"}
MIN_POS = 5.0
LEVERAGE = 5
D = xs_mom.D
H = 3_600_000
REBALANCE_DELAY = 15 * 60_000          # 봉 마감(다음 날 00:00 UTC) + 15분
K = 7                                  # 몫 수 — 요일 분산이면 몫 k 가 요일 k(0=월)에 교체
WD = xs_mom.WEEKDAYS


def _dir():
    return os.environ.get("XS_DIR", "data/xs")


def _path(name):
    return os.path.join(_dir(), name)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _arg(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def config():
    gross = float(_arg("gross") or os.environ.get("XS_GROSS") or 1)
    if not 0 < gross <= 2:
        raise SystemExit("XS_GROSS(--gross) 는 0~2 사이(문서: 1배 적정, 2배 상한)")
    cap = _arg("capital") or os.environ.get("XS_CAPITAL")
    top = _arg("top") or os.environ.get("XS_TOP")
    return dict(mode=(os.environ.get("XS_MODE") or "plan").strip().lower(), gross=gross,
                capital=float(cap) if cap else None, max_loss=float(_arg("max-loss") or os.environ.get("XS_MAX_LOSS") or 0.30),
                top=int(top) if top else None, split=split_requested())


# ── 거래소 ──────────────────────────────────────────────────────────────────
class ApiError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code


class Api:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret.encode()
        self.offset = 0.0

    @classmethod
    def from_env(cls):
        k, s = os.environ.get("BINANCE_MAINNET_API_KEY"), os.environ.get("BINANCE_MAINNET_API_SECRET")
        if not k or not s:
            raise SystemExit("BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET 가 없어요(.env)")
        return cls(k, s)

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
    return f"{q:.{max(0, -d.as_tuple().exponent)}f}"


def floor_step(x, step):
    return math.floor(x / step + 1e-9) * step


class Market:
    """exchangeInfo·마크 가격."""

    def __init__(self, api):
        info = api.call("GET", "/fapi/v1/exchangeInfo")
        self.sym = {}
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            self.sym[s["symbol"]] = dict(status=s.get("status"), step=float(lot["stepSize"]),
                                         minq=float(f["LOT_SIZE"]["minQty"]), maxq=float(lot.get("maxQty") or 1e18),
                                         min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)))
        self.mark = {x["symbol"]: float(x["markPrice"]) for x in api.call("GET", "/fapi/v1/premiumIndex")}


def account(api):
    acc = api.call("GET", "/fapi/v2/account", signed=True)
    pos = {p["symbol"]: float(p["positionAmt"]) for p in acc["positions"] if float(p["positionAmt"])}
    upnl = {p["symbol"]: float(p.get("unrealizedProfit", 0)) for p in acc["positions"] if float(p["positionAmt"])}
    entry = {p["symbol"]: float(p.get("entryPrice", 0) or 0) for p in acc["positions"] if float(p["positionAmt"])}
    usdt = next((a for a in acc["assets"] if a["asset"] == "USDT"), {})
    return dict(wallet=float(usdt.get("walletBalance", 0)), margin=float(usdt.get("marginBalance", 0)),
                total_margin=float(acc.get("totalMarginBalance", 0)), positions=pos, upnl=upnl, entry=entry)


# ── 상태·기록 ──────────────────────────────────────────────────────────────
def _slices(bar=None, qty=None):
    return [dict(day=0, bar=bar, qty=dict(qty or {})) for _ in range(K)]


def _norm(st):
    """몫이 없는 옛 형식(월요일 하나) → 7몫이 같은 월요일 목록을 1/7 씩 든 것으로. 합계 = 지금 포지션이라 주문이 생기지 않는다."""
    if not st.get("slices"):
        pos = st.get("positions") or {}
        st["slices"] = _slices(st.get("week"), {s: q / K for s, q in pos.items()})
    if not st.get("plan_bars"):
        st["plan_bars"] = [st.get("plan_week")] * K       # plan 모드가 미리보기를 마친 봉(몫별)
    st.setdefault("positions", {})
    return st


def load_state():
    p = _path("state.json")
    st = dict(start_equity=None, halted=False, week=None, positions={})
    if os.path.exists(p):
        with open(p) as f:
            st = json.load(f)
    return _norm(st)


def split_on(st):
    return bool(st.get("split_since"))


def split_requested():
    """--split-on --yes 가 남긴 요청 파일. state.json 을 직접 쓰지 않는 건 돌고 있는 서비스와 덮어쓰기 경쟁을 피하려고."""
    return os.path.exists(_path("split_on"))


def apply_split(st, cfg, now_ms, plan=False):
    """요일 분산 요청이 있으면 몫 k 를 요일 k 로 바꾼다 — 한 번만, 되돌리지 않는다. st 를 고치고 알릴 말을 돌려준다(저장은 부른 쪽).
    교체할 차례인 몫이 있으면(예: 화 00:15 월요일 리밸런스 전) 그걸 먼저 끝내고 다음 번에 바꾼다 → 바꾸는 순간 7몫이 모두 최신 목록."""
    if split_on(st) or not cfg.get("split") or due_slices(st, now_ms, plan):
        return None
    for k, sl in enumerate(st["slices"]):
        sl["day"] = k
        if k and sl["bar"] is not None:                    # 이미 돌던 몫은 자기 요일이 '다음에' 올 때 교체(한꺼번에 갈아엎지 않는다)
            sl["bar"] = xs_mom.last_bar(now_ms, k)
        if k and st["plan_bars"][k] is not None:
            st["plan_bars"][k] = xs_mom.last_bar(now_ms, k)
    st["split_since"] = _now()
    return "🧭 XS MOM 요일 분산 켬 — 지금 포지션 그대로, 앞으로 7일 동안 하루 1/7 씩 자기 요일 목록으로 교체(되돌리지 않음)"


def due_slices(st, now_ms, plan=False):
    """지금 교체할 몫 [(몫 번호, 봉)] — 몫 요일의 마지막 봉이 끝나고 15분 지났는데 아직 그 봉으로 안 한 것."""
    st = _norm(st)
    out = []
    for k, sl in enumerate(st["slices"]):
        o = xs_mom.last_bar(now_ms, sl["day"])
        done = st["plan_bars"][k] if plan else sl["bar"]
        if now_ms >= o + D + REBALANCE_DELAY and done != o:
            out.append((k, o))
    return out


def _picks(st, now_ms, plan):
    """이번에 교체할 몫. 차례인 몫이 없으면(수동 --plan/--go) 가장 최근 봉의 몫들을 다시."""
    p = due_slices(st, now_ms, plan)
    if p:
        return p
    latest = [(k, xs_mom.last_bar(now_ms, sl["day"])) for k, sl in enumerate(st["slices"])]
    m = max(o for _, o in latest)
    return [(k, o) for k, o in latest if o == m]


def _hold(st, o):
    """봉 o 다음 교체까지 일 수(월요일 하나 = 7 · 요일 분산 = 1)."""
    wd = xs_mom.weekday(o)
    return min(((sl["day"] - wd) % 7) or 7 for sl in st["slices"])


def _tag(o, ks):
    return f"{WD[xs_mom.weekday(o)]}요일 봉 {xs_mom.fmt_day(o)}" + ("" if len(ks) == K else f" · 몫 {len(ks)}/{K}")


def _write_json(name, obj):
    os.makedirs(_dir(), exist_ok=True)
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def save_state(st):
    _write_json("state.json", st)


def log_order(rec):
    os.makedirs(_dir(), exist_ok=True)
    with open(_path("orders.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_orders(n=40):
    p = _path("orders.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    return rows[-n:]


# ── 목록 ──────────────────────────────────────────────────────────────────
def bar_signal(o):
    """봉 o 기준 목록 (롱{심볼:신호 종가}, 숏{...})."""
    sig = xs_mom.signal(o, progress=False)
    return sig["long"], sig["short"]


# ── 계획 ──────────────────────────────────────────────────────────────────
def _why(s, mk, foreign):
    if s in EXCLUDE:
        return "제외 심볼"
    if s in foreign:
        return "다른 포지션이 있음"
    if s not in mk.sym or mk.sym[s]["status"] != "TRADING" or s not in mk.mark:
        return "거래 불가"
    return None


def plan(mk, acct, st, groups, capital_cap, gross):
    """groups = [(봉, [몫 번호], 롱, 숏)] — 교체할 몫들(같은 봉끼리 묶음).
    몫별 새 수량 → 코인별 합계를 목표로 → 실제 포지션과의 차이만 주문.
    반환 dict(orders, targets, slices(새 몫 수량), per, capital, skipped, warn(전부 쉼), notes(일부 쉼), rested(쉰 몫))."""
    own = dict(st.get("positions") or {})
    foreign = {s for s in acct["positions"] if s not in own}
    capital = min(acct["margin"], capital_cap) if capital_cap else acct["margin"]
    slices = [dict(sl, qty=dict(sl["qty"])) for sl in st["slices"]]
    skipped, notes, rested, per, applied = {}, [], [], 0.0, 0
    for o, ks, long, short in groups:
        want = {s: 1 for s in long} | {s: -1 for s in short}
        for s in list(want):
            why = _why(s, mk, foreign)
            if why:
                skipped[s] = why
                del want[s]
        n = len(want)
        per = capital * gross / n * len(ks) / K if n else 0.0     # 이 몫들이 코인 하나에 넣는 금액(7몫 모두면 자본 × 배수 ÷ n)
        rest = "이번 주 쉼" if len(ks) == K else f"이 몫({_tag(o, ks)}) 쉼"
        warn = None
        if n == 0:
            warn = "목표 포지션이 없어요"
        elif per < MIN_POS:
            warn = (f"포지션당 ${per:.2f} < ${MIN_POS:.0f} — 자본 ${capital:.2f} · {n}개 · 총 노출 {gross:g}배"
                    + ("" if len(ks) == K else f" · 몫 {len(ks)}/{K}") + f". {rest}")
        else:
            small = [s for s in want if per < mk.sym[s]["min_notional"]]
            if len(small) * 2 > n:
                warn = f"최소 주문 금액에 못 미치는 심볼이 {len(small)}/{n}개 — {rest}"
        if warn:
            notes.append(warn)
            rested += ks
            continue
        applied += 1
        for k in ks:
            slices[k]["qty"] = {s: sgn * per / len(ks) / mk.mark[s] for s, sgn in want.items()}
    targets, orders = {}, []
    warn = "; ".join(notes) if notes and not applied else None
    if not warn:
        agg = {}
        for sl in slices:
            for s, q in sl["qty"].items():
                agg.setdefault(s, []).append(q)
        for s, qs in agg.items():
            why = _why(s, mk, foreign)
            if why:
                skipped[s] = why
                if why == "거래 불가":                     # 폐지·중단 → 몫에서도 지운다
                    for sl in slices:
                        sl["qty"].pop(s, None)
                continue
            m, q = mk.sym[s], math.fsum(qs)
            a = floor_step(abs(q), m["step"])
            if a < m["minq"] or a * mk.mark[s] < m["min_notional"]:
                skipped[s] = (f"몫끼리 롱·숏 상쇄(${abs(q) * mk.mark[s]:.2f})" if min(qs) < 0 < max(qs)
                              else f"수량이 최소 단위 미만(${abs(q) * mk.mark[s]:.2f})")
                continue
            targets[s] = math.copysign(min(a, m["maxq"]), q)
        live_own = {s: acct["positions"].get(s, 0.0) for s in own}          # 실제 수량(수동 개입 반영)
        for s, cur in live_own.items():
            if cur and s not in targets:
                orders.append(dict(stage="청산", symbol=s, delta=-cur, target=0.0, reduce=True))
        for s, tgt in targets.items():
            cur = live_own.get(s, 0.0)
            delta = tgt - cur
            if not delta:
                continue
            m = mk.sym[s]
            reduce = cur != 0 and abs(tgt) < abs(cur) and tgt * cur >= 0
            if not reduce and abs(delta) * mk.mark[s] < m["min_notional"]:     # 거래소가 거절할 크기
                continue
            if cur and abs(delta) < 0.10 * abs(tgt):                           # 작은 조정은 회전 비용만
                continue
            orders.append(dict(stage="조정" if cur else "진입", symbol=s, delta=delta, target=tgt, reduce=reduce))
        rank = {"청산": 0, "조정": 1, "진입": 2}
        orders.sort(key=lambda o: (rank[o["stage"]], not o["reduce"]))
    return dict(orders=orders, targets=targets, slices=slices, per=per, capital=capital, skipped=skipped, warn=warn,
                notes=notes if applied else [], rested=rested)


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
    params = dict(symbol=s, side="BUY" if delta > 0 else "SELL", type="MARKET",
                  quantity=_fmt_qty(abs(delta), m["step"]), newOrderRespType="RESULT")
    if reduce:
        params["reduceOnly"] = "true"
    bid = ask = None
    try:                                                   # 주문 직전 호가 — 슬리피지(호가 중간 → 체결) 측정용
        bt = api.call("GET", "/fapi/v1/ticker/bookTicker", dict(symbol=s))
        bid, ask = float(bt["bidPrice"]), float(bt["askPrice"])
    except Exception:      # noqa: BLE001 — 측정용이라 실패해도 주문은 낸다
        pass
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
    if bid and ask:
        rec.update(bid=bid, ask=ask, mid=(bid + ask) / 2)
    if ctx.get("expect") and avg:
        rec["slip_bp"] = round((1 if delta > 0 else -1) * (avg / ctx["expect"] - 1) * 1e4, 1)   # +면 불리
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
    for sl in st["slices"]:                                # 청산 못 한 것만 몫에 남긴다
        sl["qty"] = {s: q for s, q in sl["qty"].items() if s in st["positions"]}
    save_state(st)
    return done


def rebalance(api, go, cfg, signal_fn=bar_signal, now_ms=None):
    """차례인 몫을 교체한다(없으면 가장 최근 봉의 몫들 — 수동 실행).
    반환: 요약 dict(status=done|skip|halted|preview, picks=[(몫, 봉)], ...). 계획은 st['last_plan'] 에도 남긴다."""
    now_ms = now_ms or int(time.time() * 1000)
    st = load_state()
    if st.get("halted"):
        print(f"[{_now()}] 정지 상태 — 매매 안 함(해제: --resume). 사유: {st.get('halt_reason')}")
        return dict(status="halted")
    if api.call("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"):
        raise SystemExit("헤지 모드 계정이에요 — 단방향 모드에서만 동작")
    picks = _picks(st, now_ms, plan=not go)
    by = {}
    for k, o in picks:
        by.setdefault(o, []).append(k)
    groups = []
    for o in sorted(by):                                   # 며칠 밀렸으면 오래된 봉부터
        long, short = signal_fn(o)
        if cfg.get("top"):                                 # 체결 확인용 축소 — 규칙과 다르다
            n = cfg["top"]
            long, short = dict(list(long.items())[:n]), dict(list(short.items())[-n:])
        groups.append((o, by[o], long, short))
    mk = Market(api)
    acct = account(api)
    p = plan(mk, acct, st, groups, cfg.get("capital"), cfg["gross"])
    o, ks, long, short = groups[-1]                        # 머리말·대시보드 목록은 가장 최근 봉
    head = (f"XS MOM {'실행' if go else '미리보기'} · {_tag(o, ks)} · 롱 {len(long)} · 숏 {len(short)}"
            + (f" · ⚠ top={cfg['top']}(규칙 아님)" if cfg.get("top") else ""))
    print(f"[{_now()}] {head}")
    print(f"   자본 ${p['capital']:.2f}(마진 잔고 ${acct['margin']:.2f}"
          f"{' · 상한 $%g' % cfg['capital'] if cfg.get('capital') else ''}) · 총 노출 {cfg['gross']:g}배 · "
          f"포지션당 ${p['per']:.2f} · 목표 {len(p['targets'])}개")
    for s, why in p["skipped"].items():
        print(f"   뺌 {s}: {why}")
    for x in p["notes"]:
        print(f"   ⚠ {x}")
    st["last_plan"] = dict(at=_now(), week=o, picks=picks, split=split_on(st), long=list(long), short=list(short),
                           per=p["per"], capital=p["capital"], targets=p["targets"], skipped=p["skipped"],
                           warn=p["warn"] or ("; ".join(p["notes"]) or None),
                           orders=[dict(stage=x["stage"], symbol=x["symbol"], delta=x["delta"], target=x["target"],
                                        usd=round(abs(x["delta"]) * mk.mark[x["symbol"]], 2)) for x in p["orders"]])
    if p["warn"]:
        print(f"   ⚠ {p['warn']}")
        if go:
            for k, ob in picks:                            # 그 몫들은 쉼으로 확정(다시 시도 안 함) — 예전 수량 유지
                st["slices"][k]["bar"] = ob
            st["week"] = o
        save_state(st)
        return dict(status="skip", week=o, picks=picks, warn=p["warn"], head=head)
    for od in p["orders"]:
        print(f"   {od['stage']:2} {od['symbol']:16} {od['delta']:+.6g} → 목표 {od['target']:+.6g}"
              f" (≈${abs(od['delta']) * mk.mark[od['symbol']]:.2f})")
    if not go:
        print("   (미리보기: 주문 없음)")
        save_state(st)
        return dict(status="preview", week=o, picks=picks, n=len(p["orders"]), head=head)
    if st.get("start_equity") is None:
        st["start_equity"] = acct["total_margin"]
        st["started"] = _now()
    expect = {}
    for _, _, lg, sh in groups:
        expect.update(lg)
        expect.update(sh)
    log_week(dict(week=o, at=_now(), hold=_hold(st, o), picks=picks, capital=p["capital"], gross=cfg["gross"], per=p["per"],
                  top=cfg.get("top"), targets=p["targets"], expect={s: expect[s] for s in p["targets"] if s in expect},
                  skipped=p["skipped"]))
    fails = []
    for od in p["orders"]:
        s = od["symbol"]
        try:
            if od["stage"] == "진입":
                prepare_symbol(api, s)
            market_order(api, mk, s, od["delta"], od["reduce"],
                         dict(stage=od["stage"], week=o, target=od["target"], expect=expect.get(s)))
            if od["target"]:
                st["positions"][s] = od["target"]
            else:
                st["positions"].pop(s, None)
        except ApiError as e:
            fails.append((s, str(e)))
            print(f"   ⚠ {s} {od['stage']} 실패: {e}", flush=True)
        save_state(st)                                     # 주문마다 저장 — 중간에 죽어도 자기 포지션을 안다
    acct = account(api)                                    # 실제 수량으로 동기화(부분 체결·실패 반영)
    st["positions"] = {s: acct["positions"][s] for s in set(st["positions"]) | set(p["targets"]) if acct["positions"].get(s)}
    st["slices"] = p["slices"]                             # 몫은 주문을 다 낸 뒤에 확정 — 중간에 죽으면 다시 돌아 남은 차이만 주문
    for k, ob in picks:
        st["slices"][k]["bar"] = ob
    st["week"] = o
    save_state(st)
    print(f"   완료 · 이 봇 포지션 {len(st['positions'])}개 · 실패 {len(fails)}" + (f": {fails}" if fails else ""))
    return dict(status="done", week=o, picks=picks, n=len(p["orders"]), fails=fails, npos=len(st["positions"]), per=p["per"],
                head=head, notes=p["notes"])


def check(api, max_loss):
    """손실 한도 점검. 반환: 발동했으면 dict, 아니면 None."""
    st = load_state()
    if st.get("halted") or not st.get("positions") or not st.get("start_equity"):
        return None
    eq, base = account(api)["total_margin"], st["start_equity"]
    dd = eq / base - 1
    if dd > -max_loss:
        return None
    print(f"[{_now()}] ⛔ 손실 한도: 시작 ${base:.2f} → ${eq:.2f} ({dd:+.1%}) ≤ −{max_loss:.0%} → 전량 청산·정지", flush=True)
    done = flatten(api, Market(api), st, "손실 한도")
    st["halted"], st["halt_reason"] = True, f"손실 한도 {dd:+.1%} ({_now()})"
    save_state(st)
    return dict(dd=dd, closed=len(done), base=base, eq=eq)


# ── 실매매 ↔ 규칙 대조 ─────────────────────────────────────────────────────
# 규칙(종이) = 같은 목표 수량을 신호 봉 종가에 체결했다고 보고, 다음 교체 봉 종가(진행 중이면 현재 마크)까지의 가격 손익.
# 구간 = 리밸런스 기록 하나(hold 일: 월요일 하나 = 7 · 요일 분산 = 1 → 보여 줄 땐 주 단위로 합친다).
# 실매매 = 실제 체결가·수수료. 항등식으로 쪼갠다(심볼 s, 구간 [봉+1일 00:00, 봉+1+hold 일 00:00)):
#   실매매 가격 손익 = 목표·(P1−C0) + (끝 수량−목표)·(P1−C0) + Σ체결수량·(C0−호가중간) + Σ체결수량·(호가중간−체결가)
#                    = 규칙            + 수량 차이               + 체결 지연                  + 슬리피지
#   + 수수료(실제). 펀딩은 양쪽에 거의 같이 붙으므로 차이에서 빼고 따로 보여 준다.
def _read_jsonl(name):
    p = _path(name)
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(x) for x in f if x.strip()]


def log_week(rec):
    os.makedirs(_dir(), exist_ok=True)
    with open(_path("weeks.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_CLOSE = {}


def _day_close(s, t):
    """일봉(시작 t) 종가 — 없으면 None."""
    if (s, t) not in _CLOSE:
        k = xs_mom.daily(s, t, t)
        _CLOSE[(s, t)] = k[t][0] if t in k else None
    return _CLOSE[(s, t)]


def _ref_1m(s, t):
    """호가 기록이 없던 주문의 기준가 — 주문 시각 1분봉 시가."""
    rows = xs_mom._get("klines", {"symbol": s, "interval": "1m", "startTime": t // 60_000 * 60_000, "limit": 1})
    return float(rows[0][1]) if rows else None


def _funding(api, a, b):
    """[a, b) 펀딩 수입, 심볼별 USDT(받으면 +)."""
    out, seen, t = {}, set(), a
    while t < b:
        e, start = min(b, t + 3 * D) - 1, t
        while True:
            rows = api.call("GET", "/fapi/v1/income",
                            dict(incomeType="FUNDING_FEE", startTime=start, endTime=e, limit=1000), signed=True) or []
            new = [r for r in rows if r.get("tranId") not in seen]
            for r in new:
                seen.add(r.get("tranId"))
                out[r["symbol"]] = out.get(r["symbol"], 0.0) + float(r["income"])
            if len(rows) < 1000 or not new:
                break
            start = int(rows[-1]["time"])                 # 같은 시각 여러 건 → +1 하지 않고 tranId 로 중복 제거
        t += 3 * D
    return out


def _weeks(orders):
    """주별 목표 {월요일 봉: 기록}. weeks.jsonl(리밸런스 때 기록, 같은 주 두 번이면 나중 것) → 없던 주는 주문으로 복원."""
    recs = {w["week"]: w for w in _read_jsonl("weeks.jsonl")}
    lp = load_state().get("last_plan") or {}
    by = {}
    for o in orders:
        if o.get("week") is not None and "target" in o:
            by.setdefault(o["week"], []).append(o)
    for w, rows in by.items():
        if w in recs:
            continue
        tg, ex = {}, {}
        for o in rows:
            tg[o["symbol"]] = o["target"]
            if o.get("expect"):
                ex[o["symbol"]] = o["expect"]
        rec = dict(week=w, targets={s: q for s, q in tg.items() if q}, expect=ex, rebuilt=True)
        if lp.get("week") == w:
            rec.update(capital=lp.get("capital"), skipped=lp.get("skipped"))
        recs[w] = rec
    return dict(sorted(recs.items()))


def compare_week(api, rec, orders, now_ms, marks, funding=True):
    w, hold = rec["week"], rec.get("hold") or 7
    a, b = w + D, w + (hold + 1) * D
    done = now_ms >= b
    fills = sorted((o for o in orders if o.get("avgPrice") and o.get("qty")), key=lambda o: o["t"])

    def sq(o):
        return (1 if o["side"] == "BUY" else -1) * o["qty"]
    q0 = {}
    for o in fills:
        if o["t"] < a:
            q0[o["symbol"]] = q0.get(o["symbol"], 0.0) + sq(o)
    win = [o for o in fills if a <= o["t"] < b]
    tg = rec.get("targets") or {}
    syms = set(tg) | {o["symbol"] for o in win} | {s for s, q in q0.items() if abs(q) > 1e-12}
    r = dict(week=w, hold=hold, n=1, done=done, capital=rec.get("capital"), gross=rec.get("gross"), rebuilt=rec.get("rebuilt", False),
             skipped=rec.get("skipped") or {}, paper=0.0, delay=0.0, slip=0.0, qty=0.0, fee=0.0, fund=0.0,
             traded=0.0, notional=0.0, fills=len(win), miss=[], other_fee={}, worst=[])
    for s in sorted(syms):
        c0 = (rec.get("expect") or {}).get(s) or _day_close(s, w)
        p1 = _day_close(s, w + hold * D) if done else marks.get(s)
        if c0 is None or p1 is None:
            r["miss"].append(s)
            continue
        t = tg.get(s, 0.0)
        mine = [o for o in win if o["symbol"] == s]
        qend = q0.get(s, 0.0) + sum(sq(o) for o in mine)
        r["paper"] += t * (p1 - c0)
        r["qty"] += (qend - t) * (p1 - c0)
        r["notional"] += abs(t) * c0
        for o in mine:
            ref = o.get("mid") or _ref_1m(s, o["t"]) or o["avgPrice"]
            d, sl = sq(o) * (c0 - ref), sq(o) * (ref - o["avgPrice"])
            r["delay"] += d
            r["slip"] += sl
            r["traded"] += o["qty"] * o["avgPrice"]
            r["worst"].append((sl / (o["qty"] * o["avgPrice"]) * 1e4, s))
    for o in win:
        fa = o.get("feeAsset") or "USDT"
        if fa == "USDT":
            r["fee"] -= float(o.get("fee") or 0)
        else:
            r["other_fee"][fa] = r["other_fee"].get(fa, 0.0) + float(o.get("fee") or 0)
    if funding:
        r["fund"] = sum(v for s, v in _funding(api, a, min(b, now_ms)).items() if s in syms)
    r["live"] = r["paper"] + r["qty"] + r["delay"] + r["slip"] + r["fee"]
    r["worst"] = sorted(r["worst"])[:3]
    return r


def compare_all(api, now_ms=None, funding=True, since=None):
    """since(ms) 를 주면 그 뒤 구간만 — 요일 분산은 하루 한 구간이라 자동 게시에선 최근 것만 본다."""
    now_ms = now_ms or int(time.time() * 1000)
    orders = _read_jsonl("orders.jsonl")
    weeks = {w: r for w, r in _weeks(orders).items() if since is None or w >= since}
    marks = {}
    if any(now_ms < w + ((r.get("hold") or 7) + 1) * D for w, r in weeks.items()):
        marks = {x["symbol"]: float(x["markPrice"]) for x in api.call("GET", "/fapi/v1/premiumIndex")}
    return [compare_week(api, rec, orders, now_ms, marks, funding) for rec in weeks.values() if rec["week"] + D <= now_ms]


def merge_compare(rs):
    """여러 구간을 한 묶음으로(요일 분산 하루치 7개 → 한 주). 금액은 더하고 기준 자본은 마지막 구간 것."""
    if len(rs) == 1:
        return rs[0]
    m = dict(rs[-1], week=rs[0]["week"], n=len(rs), end=rs[-1]["week"] + rs[-1]["hold"] * D,
             done=all(r["done"] for r in rs), rebuilt=any(r["rebuilt"] for r in rs), miss=sorted({s for r in rs for s in r["miss"]}),
             skipped={k: v for r in rs for k, v in r["skipped"].items()}, worst=sorted(x for r in rs for x in r["worst"])[:3])
    for f in ("paper", "delay", "slip", "qty", "fee", "fund", "traded", "live", "fills"):
        m[f] = sum(r[f] for r in rs)
    m["other_fee"] = {}
    for r in rs:
        for a, v in r["other_fee"].items():
            m["other_fee"][a] = m["other_fee"].get(a, 0.0) + v
    return m


def weekly(rs):
    """대조 구간을 주 단위로 묶는다 — 7일 구간은 그대로, 1일 구간은 시작부터 7일씩."""
    out = []
    for r in rs:
        g = out[-1] if out else None
        if g and r["hold"] < 7 and g[0]["hold"] < 7 and r["week"] - g[0]["week"] < 7 * D:
            g.append(r)
        else:
            out.append([r])
    return [merge_compare(g) for g in out]


def fmt_compare(r, short=False):
    base = r["capital"] or r["notional"]
    pct = (lambda x: f"{x / base * 100:+.2f}%") if base else (lambda x: "—")
    bp = (lambda x: f"{x / r['traded'] * 1e4:+.1f}bp") if r["traded"] else (lambda x: "—")
    cost = r["slip"] + r["fee"]
    span = (f"{xs_mom.fmt_day(r['week'])}~{xs_mom.fmt_day(r['end'])} 봉 {r['n']}구간" if r.get("n", 1) > 1
            else f"{WD[xs_mom.weekday(r['week'])]}요일 봉 {xs_mom.fmt_day(r['week'])}" + (f" · {r['hold']}일" if r.get("hold", 7) != 7 else ""))
    head = (f"XS 대조 · {span} · {'확정' if r['done'] else '진행 중(현재 마크 기준)'}"
            f" · 기준 {'자본' if r['capital'] else '목표 노출'} ${base:.2f}" + (" · 주문으로 복원" if r["rebuilt"] else ""))
    lines = [head,
             f"  규칙(신호 종가 체결) {pct(r['paper'])} (${r['paper']:+.2f}) · 실매매 {pct(r['live'])} (${r['live']:+.2f})"
             f" · 차이 {pct(r['live'] - r['paper'])} (${r['live'] - r['paper']:+.2f})",
             f"  회전 비용(슬리피지+수수료) {bp(cost)} of 체결액 ${r['traded']:.2f} — 백테스트 가정 −{xs_mom.COST * 1e4:.0f}bp"]
    if short:
        return "\n".join(lines)
    lines += [f"    체결 지연(신호 종가 → 주문 직전 호가)   ${r['delay']:+.2f} {bp(r['delay'])}  ← 운(평균 0 기대), 비용 아님",
              f"    슬리피지(호가 중간 → 체결가)         ${r['slip']:+.2f} {bp(r['slip'])}",
              f"    수수료                               ${r['fee']:+.2f} {bp(r['fee'])}"
              + (f" (+ USDT 외 {r['other_fee']})" if r["other_fee"] else ""),
              f"    수량 차이(내림·빠짐·작은 조정 생략)   ${r['qty']:+.2f}",
              f"  펀딩(실제, 차이에서 뺌) ${r['fund']:+.2f} · 체결 {r['fills']}건"]
    if r["worst"]:
        lines.append("  슬리피지 큰 순: " + ", ".join(f"{s} {x:+.0f}bp" for x, s in r["worst"]))
    if r["skipped"]:
        lines.append("  뺀 심볼: " + ", ".join(f"{s}({why})" for s, why in r["skipped"].items()))
    if r["miss"]:
        lines.append(f"  가격 없음(계산 제외): {r['miss']}")
    return "\n".join(lines)


def snapshot(api, cfg, err=None):
    """대시보드용 상태(status.json)."""
    st = load_state()
    now = int(time.time() * 1000)
    nxt = None
    for k, sl in enumerate(st["slices"]):                  # 몫마다: 안 했으면 이번 차례(지났으면 곧) · 했으면 다음 주 같은 요일
        o = xs_mom.last_bar(now, sl["day"])
        done = (sl["bar"] if cfg["mode"] == "live" else st["plan_bars"][k]) == o
        t = o + 8 * D + REBALANCE_DELAY if done else o + D + REBALANCE_DELAY
        nxt = t if nxt is None else min(nxt, t)
    days = sorted({sl["day"] for sl in st["slices"]})
    out = dict(updated=_now(), mode=cfg["mode"], gross=cfg["gross"], capital_cap=cfg.get("capital"), max_loss=cfg["max_loss"],
               split=split_on(st), split_since=st.get("split_since"),
               schedule="요일 분산 7몫(매일 1/7)" if split_on(st) else "월요일 하나(주 1회)" + (" · 요일 분산 대기 중" if cfg.get("split") else ""),
               slices=[dict(day=WD[sl["day"]], bar=xs_mom.fmt_day(sl["bar"]) if sl["bar"] else None, n=len(sl["qty"]))
                       for sl in st["slices"]] if len(days) > 1 else None,
               top=cfg.get("top"), halted=bool(st.get("halted")), halt_reason=st.get("halt_reason"),
               start_equity=st.get("start_equity"), started=st.get("started"),
               last_week=xs_mom.fmt_day(st["week"]) if st.get("week") else None,
               next_rebalance=datetime.fromtimestamp(nxt / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
               + (" (지남 — 곧 실행)" if nxt <= now else ""),
               last_plan=st.get("last_plan"), orders=recent_orders(), positions=[], error=err)
    if api is not None:
        try:
            acct = account(api)
            mk_mark = {x["symbol"]: float(x["markPrice"]) for x in api.call("GET", "/fapi/v1/premiumIndex")}
            out.update(equity=acct["total_margin"], wallet=acct["wallet"])
            if st.get("start_equity"):
                out["pnl_pct"] = acct["total_margin"] / st["start_equity"] - 1
            gl = gs = up = 0.0
            for s in sorted(st.get("positions") or {}):
                q = acct["positions"].get(s, 0.0)
                v = q * mk_mark.get(s, 0.0)
                gl, gs, up = gl + max(v, 0), gs + max(-v, 0), up + acct["upnl"].get(s, 0.0)
                out["positions"].append(dict(symbol=s, qty=q, side="롱" if q > 0 else "숏" if q < 0 else "-",
                                             entry=acct["entry"].get(s), mark=mk_mark.get(s), value=round(v, 2),
                                             upnl=round(acct["upnl"].get(s, 0.0), 4)))
            out.update(long_usd=round(gl, 2), short_usd=round(gs, 2), upnl=round(up, 4))
        except Exception as e:      # noqa: BLE001 — 스냅샷 실패가 서비스를 죽이면 안 된다
            out["error"] = (err + " · " if err else "") + f"계정 조회 실패: {e}"
    _write_json("status.json", out)
    return out


# ── 서비스 루프 ─────────────────────────────────────────────────────────────
def due(st, now_ms, plan=False):
    """교체할 차례인 몫이 있나(몫 요일 봉 마감 + 15분 이후 · 그 봉으로 아직 안 함). 월요일 하나면 = 화 00:15 이후 주 1회."""
    return bool(due_slices(st, now_ms, plan))


def loop(cfg, api=None, sleep=time.sleep, now=lambda: int(time.time() * 1000), max_iter=None):
    mode = cfg["mode"]
    if mode not in ("off", "plan", "live"):
        raise SystemExit(f"XS_MODE={mode} — off | plan | live 중 하나")
    print(f"[{_now()}] XS MOM 서비스 시작 · 모드 {mode} · 총 노출 {cfg['gross']:g}배 · 자본 상한 {cfg.get('capital')} · "
          f"손실 한도 −{cfg['max_loss']:.0%}" + (f" · top={cfg['top']}" if cfg.get("top") else ""), flush=True)
    if mode == "off":
        snapshot(None, cfg, "XS_MODE=off — 꺼짐")
    elif api is None:
        api = Api.from_env()
        api.sync()
    st0 = load_state()
    notify(f"🧭 XS MOM 서비스 시작 · 모드 **{mode}** · {cfg['gross']:g}배 · 손실 한도 −{cfg['max_loss']:.0%} · "
           + ("요일 분산 7몫" if split_on(st0) else "월요일 하나" + (" (요일 분산 켤 차례 기다림)" if cfg.get("split") else "")), "system")
    last_check = last_snap = last_try = 0
    it = 0
    while max_iter is None or it < max_iter:
        it += 1
        t = now()
        if mode != "off":
            err = None
            try:
                st = load_state()
                plan_mode = mode != "live"
                cfg["split"] = split_requested()                # --split-on 은 서비스가 도는 중에 남긴다 → 바퀴마다 확인
                msg = apply_split(st, cfg, t, plan_mode)
                if msg:
                    save_state(st)
                    print(f"[{_now()}] {msg}", flush=True)
                    notify(msg, "trade")
                if due(st, t, plan_mode) and t - last_try >= H:
                    last_try = t
                    res = rebalance(api, mode == "live", cfg, bar_signal, now_ms=t)
                    if mode == "plan" and res.get("picks"):
                        st = load_state()
                        for k, ob in res["picks"]:
                            st["plan_bars"][k] = ob
                        save_state(st)
                    _announce(res, mode)
                    if mode == "live" and res.get("status") == "done":
                        _announce_compare(api, t, res.get("week"))
                if mode == "live" and t - last_check >= H:
                    last_check = t
                    hit = check(api, cfg["max_loss"])
                    if hit:
                        notify(f"⛔ XS MOM 손실 한도 — 시작 ${hit['base']:.2f} → ${hit['eq']:.2f} ({hit['dd']:+.1%}) · "
                               f"{hit['closed']}개 청산 · 정지(해제: --resume)", "trade")
            except SystemExit as e:
                err = str(e)
                notify(f"⚠️ XS MOM 중단 사유: {err}", "system")
            except Exception as e:      # noqa: BLE001 — 한 번 실패로 서비스가 죽지 않게(다음 시간에 재시도)
                err = f"{type(e).__name__}: {e}"
                print(traceback.format_exc(), flush=True)
                notify(f"⚠️ XS MOM 오류(1시간 뒤 재시도): {err[:300]}", "system")
            if err or t - last_snap >= 5 * 60_000:
                last_snap = t
                snapshot(api, cfg, err)
        sleep(60)


def _announce_compare(api, now_ms, week=None):
    """리밸런스 뒤 지난주(확정) 실매매 ↔ 규칙 대조를 #매매 에. 요일 분산이면 월요일 몫 교체 날에 지난 7일을 묶어 주 1회.
    실패해도 매매엔 영향 없음."""
    try:
        if week is not None and xs_mom.weekday(week) != 0:
            return
        done = [r for r in compare_all(api, now_ms, since=now_ms - 16 * D) if r["done"]]
        if done and done[-1]["hold"] < 7:
            done = [merge_compare([r for r in done if r["week"] > done[-1]["week"] - 7 * D])]
        if done:
            notify("🔍 " + fmt_compare(done[-1], short=True), "trade")
    except Exception as e:      # noqa: BLE001
        print(f"[{_now()}] 대조 실패(무시): {type(e).__name__}: {e}", flush=True)


def _announce(res, mode):
    s = res.get("status")
    if s == "done":
        msg = (f"📊 {res['head']}\n주문 {res['n']}건 · 포지션 {res['npos']}개 · 포지션당 ${res['per']:.2f}"
               + (f"\n⚠ 실패 {len(res['fails'])}건: " + ", ".join(f"{a} {b[:60]}" for a, b in res["fails"]) if res["fails"] else "")
               + "".join(f"\n⏸ {x}" for x in res.get("notes") or []))
    elif s == "skip":
        msg = f"⏸ {res['head']}\n{res['warn']}"
    elif s == "preview":
        msg = f"👀 {res['head']} (모드 plan — 주문 없음) · 계획 주문 {res['n']}건 · 대시보드에서 확인"
    elif s == "halted":
        msg = "⛔ XS MOM 정지 상태라 이번 주 리밸런스 안 함(해제: --resume)"
    else:
        return
    notify(msg, "trade")


def main():
    from .env import load_dotenv
    load_dotenv()
    cfg = config()
    if "--loop" in sys.argv:
        return loop(cfg)
    api = Api.from_env()
    api.sync()
    if "--check" in sys.argv:
        check(api, cfg["max_loss"])
    elif "--status" in sys.argv:
        out = snapshot(api, cfg)
        print(json.dumps({k: out.get(k) for k in ("mode", "halted", "equity", "start_equity", "pnl_pct", "last_week",
                                                   "next_rebalance", "long_usd", "short_usd", "upnl")}, ensure_ascii=False))
        for p in out["positions"]:
            print(f"   {p['symbol']:16} {p['side']} {p['qty']:+.6g} · ${p['value']:+.2f} · 미실현 {p['upnl']:+.4f}")
    elif "--flatten" in sys.argv:
        st = load_state()
        done = flatten(api, Market(api), st, "수동")
        st["halted"], st["halt_reason"] = True, f"수동 청산 ({_now()})"
        save_state(st)
        print(f"청산 {len(done)}개 · 정지(해제: --resume)")
    elif "--resume" in sys.argv:
        st = load_state()
        st["halted"], st["halt_reason"] = False, None
        if not st.get("positions"):
            st["start_equity"] = None
            for sl in st["slices"]:
                sl["qty"] = {}
        save_state(st)
        print("정지 해제")
    elif "--split-on" in sys.argv:
        st = load_state()
        if split_on(st):
            print(f"이미 요일 분산이에요({st['split_since']} 부터)")
        elif "--yes" not in sys.argv:
            print("요일 분산(7몫 · 매일 1/7 교체)으로 바꿉니다 — 되돌리는 기능은 없어요.\n"
                  "  · 바꾸는 순간 주문 없음. 그다음 7일 동안 매일 00:15 UTC 에 그 요일 몫 1/7 만 교체\n"
                  "  · 한 몫분 주문이 $5 미만이면 그 몫은 쉼(2배 · 30개 기준 자본 ≥ ~$530, 대상 많은 주 ~$1,100)\n"
                  f"  · 지금 마진 잔고 ${account(api)['margin']:.2f}\n"
                  "진행하려면: python -m engine.xs_bot --split-on --yes")
        else:
            os.makedirs(_dir(), exist_ok=True)
            with open(_path("split_on"), "w") as f:
                f.write(_now() + "\n")
            print("요청 기록 — 서비스가 다음 바퀴(1분 안, 교체 차례면 그걸 끝낸 뒤)에 바꾸고 #매매 에 알립니다")
    elif "--compare" in sys.argv:
        rs = weekly(compare_all(api))
        if not rs:
            print("대조할 주가 없어요(실주문 기록 없음)")
        for r in rs:
            print(fmt_compare(r))
        if len(rs) > 1:
            pp, lv = sum(r["paper"] for r in rs), sum(r["live"] for r in rs)
            print(f"합계 {len(rs)}주 · 규칙 ${pp:+.2f} · 실매매 ${lv:+.2f} · 차이 ${lv - pp:+.2f}"
                  f" · 펀딩 ${sum(r['fund'] for r in rs):+.2f}")
    elif "--go" in sys.argv or "--plan" in sys.argv:
        rebalance(api, "--go" in sys.argv, cfg)
    else:
        raise SystemExit("--loop | --plan | --go | --check | --status | --flatten | --resume | --compare | --split-on [--yes]")


if __name__ == "__main__":
    main()

"""XS MOM 규칙 — 알트 횡단면 28일 모멘텀 롱숏의 '이번 주 목록' 산출. 규칙은 여기 한 곳에만 둔다.

쓰는 곳: engine.xs_bot(실매매 서비스) · research/xs_forward.py(종이 전진 기록) · research/exp_XS_cross_section.py(상수).
사전 고정(research/XS_MOM.md · exp_XS_cross_section 머리말) — 바꾸지 말 것:
  리밸런스  월요일 일봉 종가(= 화 00:00 UTC). 그 종가가 신호·기대 체결가.
  대상      USDT 무기한 COIN 거래 중 · 상장 ≥ 30일 · 최근 30개 일봉 거래대금 중앙 ≥ $5M · 스테이블 제외.
  신호      월요일 종가 / 28일 전 종가 − 1. 상위 10% 롱 · 하위 10% 숏 · 동일 가중.
공개 API 만 쓴다(키 불필요).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

D = 86_400_000
LOOK, FRAC = 28, 0.10
COST = 0.0010
MINQ, AGE = 5e6, 30
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "USD1", "RLUSD", "XUSD", "PYUSD", "BFUSD"}
FAPI = "https://fapi.binance.com/fapi/v1"


def _get(path, params, retries=5):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{FAPI}/{path}?{q}", headers={"User-Agent": "xs-mom/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                print(f"   한도 {e.code} — 60초 쉼", flush=True)
                time.sleep(60)
                continue
            if e.code == 400:
                return []
            if i == retries - 1:
                raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise
        time.sleep(2 * (i + 1))
    return []


def fmt_day(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")


def last_monday_bar(now_ms):
    """마지막으로 '끝난' 월요일 일봉의 시작 시각(ms)."""
    o = now_ms // D * D - D
    while datetime.fromtimestamp(o / 1000, timezone.utc).weekday() != 0:
        o -= D
    return o


def universe():
    info = _get("exchangeInfo", {})
    return [(s["symbol"], int(s.get("onboardDate", 0))) for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("underlyingType") == "COIN"
            and s.get("status") == "TRADING" and s.get("baseAsset") not in STABLE]


def daily(sym, start, end):
    rows = _get("klines", {"symbol": sym, "interval": "1d", "startTime": start, "endTime": end, "limit": 1000})
    time.sleep(0.12)
    return {int(k[0]): (float(k[4]), float(k[7])) for k in rows}


def rank(cand):
    """[(28일 수익률, 심볼, 종가)] → 상위·하위 FRAC. 순서: 롱은 강한 순, 숏은 약한 것이 끝."""
    cand = sorted(cand, reverse=True)
    k = max(1, int(len(cand) * FRAC))
    return k, cand[:k], cand[-k:]


def signal(o, progress=True):
    """월요일 봉 o 기준 이번 주 목록. long/short = {심볼: 월요일 종가}."""
    syms = universe()
    cand = []
    for n, (s, ob) in enumerate(syms, 1):
        if (o - ob) / D < AGE:
            continue
        k = daily(s, o - 29 * D, o)
        if o not in k or (o - LOOK * D) not in k:
            continue
        q = [k[t][1] for t in range(o - 29 * D, o + D, D) if t in k]
        if np.median(q) < MINQ:
            continue
        cand.append((k[o][0] / k[o - LOOK * D][0] - 1, s, k[o][0]))
        if progress and n % 200 == 0:
            print(f"  {n}/{len(syms)}", flush=True)
    k, top, bot = rank(cand)
    return dict(t=o, n=len(cand), k=k,
                long={s: p for _, s, p in top}, short={s: p for _, s, p in bot},
                sig_long=[round(x, 4) for x, _, _ in top], sig_short=[round(x, 4) for x, _, _ in bot])

"""바이낸스 USDⓈ-M 선물 공개 klines 수집 (API 키 불필요).

candle-collector와 동일한 엔드포인트(fapi.binance.com/fapi/v1/klines).
1분봉을 받아 Candles 로 변환. 페이지네이션으로 여러 날 수집.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error

import numpy as np

from .candles import Candles, MINUTE_MS

BASE = "https://fapi.binance.com/fapi/v1/klines"
EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
MAX_LIMIT = 1500          # 요청당 최대 캔들 수 (weight 10)
PAGE_SLEEP = 0.3          # 페이지 간 대기 (레이트리밋 여유: ~3.3req/s < 2400 weight/min)

SYMBOLS_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "symbols.json")
SYMBOLS_TTL = 24 * 3600   # 상장/폐지는 드물다 → 하루 캐시로 충분


def _get(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int, retries: int = 4):
    q = urllib.parse.urlencode({
        "symbol": symbol, "interval": interval,
        "startTime": start_ms, "endTime": end_ms, "limit": limit,
    })
    url = BASE + "?" + q
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "auto-trading/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 429=레이트리밋, 418=밴. Retry-After 존중하고 백오프.
            if e.code in (429, 418) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 0)) or (2 ** attempt)
                print(f"  [레이트리밋 {e.code}] {wait}s 대기 후 재시도...", flush=True)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return []


def fetch_range_rows(symbol: str, interval: str, start_ms: int, end_ms: int,
                     verbose: bool = False):
    """[start_ms, end_ms] 구간 klines를 페이지네이션으로 수집.

    반환: [(open_time, o, h, l, c, v, taker_buy), ...] (raw 튜플). 캐시 저장용.
    taker_buy = klines[9] 테이커 매수 체결량(base) — 오더플로우 델타/CVD 지표용.

    ⚠️ '아직 닫히지 않은' 마지막 봉(형성 중)은 제외한다 — closeTime이 현재보다 미래면
    거래량/종가가 확정 전이라, 저장하면 부분값으로 굳는다(캐시 PK라 이후 갱신 안 됨).
    과거 구간(end_ms가 과거)에는 영향 없음(모든 봉이 이미 닫혀 있음).
    """
    now = int(time.time() * 1000)
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get(symbol, interval, cursor, end_ms, MAX_LIMIT)
        if not batch:
            break
        for k in batch:
            # klines: [openTime,o,h,l,c,v,closeTime,quoteVol,trades,takerBuyBase,takerBuyQuote,...]
            if int(k[6]) >= now:                 # closeTime 미래 = 형성 중 봉 → 확정될 때까지 저장 보류
                continue
            rows.append((int(k[0]), float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5]), float(k[9])))
        last_open = int(batch[-1][0])
        cursor = last_open + MINUTE_MS
        if verbose:
            print(f"  수집 {len(rows)}개...", flush=True)
        if len(batch) < MAX_LIMIT:
            break
        time.sleep(PAGE_SLEEP)
    return rows


def fetch(symbol: str = "BTCUSDT", interval: str = "1m", days: float = 5,
          end_ms: int = None, verbose: bool = True) -> Candles:
    """최근 `days`일치 1분봉 수집.

    end_ms: 종료 시각(ms). 미지정 시 지금. 재현성 원하면 고정값 전달.
    """
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    total_min = int(days * 24 * 60)
    start_ms = end_ms - total_min * MINUTE_MS

    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get(symbol, interval, cursor, end_ms, MAX_LIMIT)
        if not batch:
            break
        for k in batch:
            # klines: [openTime,o,h,l,c,v,closeTime,quoteVol,trades,takerBuyBase,takerBuyQuote,...]
            rows.append((int(k[0]), float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5]), float(k[9])))
        last_open = int(batch[-1][0])
        cursor = last_open + MINUTE_MS
        if verbose:
            print(f"  수집 {len(rows)}개... (~{last_open})", flush=True)
        if len(batch) < MAX_LIMIT:
            break
        time.sleep(PAGE_SLEEP)  # 레이트리밋 예의

    tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60}[interval]
    return Candles.from_rows(rows, timeframe_min=tf_min)


def list_symbols(refresh: bool = False, cache_path: str = None) -> dict:
    """거래 가능한 USDⓈ-M **무기한** 선물 심볼 목록 (exchangeInfo).

    수집 심볼을 손으로 타이핑하면 오타(BTCUSCD 등)를 내도 조용히 빈 캔들만 쌓인다.
    대시보드가 이 목록으로 자동완성·검증하게 하려고 뽑는다.

    반환: {"symbols": [{"symbol","base","quote"}...], "fetchedAt": ms, "stale": bool}
      stale=True 는 "네트워크 실패로 캐시(오래됐을 수 있음)를 대신 돌려줬다"는 뜻.

    응답이 수 MB라 하루(SYMBOLS_TTL) 캐시한다. 상장/폐지는 드물어 문제되지 않고,
    새 상장을 바로 보고 싶으면 refresh=True.
    """
    path = cache_path or SYMBOLS_CACHE
    now_ms = int(time.time() * 1000)

    cached = None
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        pass
    if not refresh and cached and (now_ms - cached.get("fetchedAt", 0)) < SYMBOLS_TTL * 1000:
        return {**cached, "stale": False}

    try:
        req = urllib.request.Request(EXCHANGE_INFO, headers={"User-Agent": "auto-trading/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        if cached:                        # 네트워크가 죽어도 편집기는 계속 쓸 수 있어야 한다
            return {**cached, "stale": True}
        raise

    out = []
    for s in data.get("symbols", []):
        # PERPETUAL 만 — 분기물(CURRENT_QUARTER 등)은 만기가 있어 워치리스트에 부적합.
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL":
            out.append({"symbol": s["symbol"], "base": s.get("baseAsset", ""),
                        "quote": s.get("quoteAsset", "")})
    out.sort(key=lambda d: d["symbol"])

    result = {"symbols": out, "fetchedAt": now_ms}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except OSError:
        pass                              # 캐시 못 써도 조회 자체는 성공 — 다음에 다시 받으면 됨
    return {**result, "stale": False}


def fetch_funding(symbol: str = "BTCUSDT", days: float = 5, end_ms: int = None):
    """과거 펀딩비율 히스토리. 반환: [(fundingTime_ms, rate), ...] (8시간 간격)."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 60) * MINUTE_MS
    return fetch_funding_range(symbol, start_ms, end_ms)


def fetch_funding_range(symbol: str, start_ms: int, end_ms: int):
    """[start, end] 펀딩 히스토리 전체 — 1000개 제한을 넘겨 페이지네이션. 반환 [(time_ms, rate), ...]."""
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    out = []
    cursor = start_ms
    while cursor < end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "startTime": cursor,
                                    "endTime": end_ms, "limit": 1000})
        req = urllib.request.Request(url + "?" + q, headers={"User-Agent": "auto-trading/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if not data:
            break
        out.extend((int(d["fundingTime"]), float(d["fundingRate"])) for d in data)
        cursor = int(data[-1]["fundingTime"]) + 1
        if len(data) < 1000:
            break
        time.sleep(PAGE_SLEEP)
    return out

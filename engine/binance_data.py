"""바이낸스 USDⓈ-M 선물 공개 klines 수집 (API 키 불필요).

candle-collector와 동일한 엔드포인트(fapi.binance.com/fapi/v1/klines).
1분봉을 받아 Candles 로 변환. 페이지네이션으로 여러 날 수집.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
import urllib.error

import numpy as np

from .candles import Candles, MINUTE_MS

BASE = "https://fapi.binance.com/fapi/v1/klines"
MAX_LIMIT = 1500          # 요청당 최대 캔들 수 (weight 10)
PAGE_SLEEP = 0.3          # 페이지 간 대기 (레이트리밋 여유: ~3.3req/s < 2400 weight/min)


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
    """
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


def fetch_funding(symbol: str = "BTCUSDT", days: float = 5, end_ms: int = None):
    """과거 펀딩비율 히스토리. 반환: [(fundingTime_ms, rate), ...] (8시간 간격)."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 60) * MINUTE_MS
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    q = urllib.parse.urlencode({"symbol": symbol, "startTime": start_ms,
                                "endTime": end_ms, "limit": 1000})
    req = urllib.request.Request(url + "?" + q, headers={"User-Agent": "auto-trading/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return [(int(d["fundingTime"]), float(d["fundingRate"])) for d in data]

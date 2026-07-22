"""독립 실행 캔들 수집기.

candle-collector(Go)가 크론으로 캔들을 쌓던 것처럼, 이 스크립트는 로컬
SQLite 캐시(`data/candles.db`)를 최신으로 유지한다. 백테스트와 별개로 돌릴 수 있음.

사용법:
    # 1회 수집 (기본 7일치 시드, 이미 있으면 최신분만 증분)
    python3 -m engine.collector BTCUSDT ETHUSDT

    # 60초마다 반복 (Ctrl+C 종료) — 실시간 유지
    python3 -m engine.collector BTCUSDT --loop 60

    # 과거 대량 백필 (첫 수집 범위 지정)
    python3 -m engine.collector BTCUSDT --seed-days 365

    # 워치리스트 파일 사용 (한 줄에 심볼 하나, # 주석)
    python3 -m engine.collector --watchlist data/watchlist.txt --loop 60

백그라운드로 돌리려면 세션 프롬프트에서:
    ! python3 -m engine.collector BTCUSDT ETHUSDT --loop 60 &
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from . import candle_store


def _fmt(ms):
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def _now():
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S")


def _next_boundary(period_s, offset_s=2.0):
    """현재보다 미래인 다음 period 경계(+offset) 시각(epoch초). period=60이면 다음 분 :00+offset."""
    now = time.time()
    return (int((now - offset_s) // period_s) + 1) * period_s + offset_s


def _sleep_aligned(period_s, offset_s=2.0):
    """다음 period 경계(+offset)까지 잔다. period=60이면 매분 :00 직후(+2초)에 깬다.
    epoch초는 :00에 정렬돼 있어 60의 배수가 정확히 분 경계와 일치 → 캔들이 확정되는 순간 바로 수집."""
    time.sleep(max(0.5, _next_boundary(period_s, offset_s) - time.time()))


# 백필 분리: 한 요청(PAGE_MINUTES봉)보다 많이 뒤처진 심볼은 '백필 그룹'으로 빼서
# 최신 수집(폴링)이 끝난 뒤 남는 시간에만 페이지 단위로 채운다 → 새 심볼 대량 수집이
# 기존 심볼의 최신 캔들 지연을 일으키지 않게. 다 채워 최신 근접하면 폴링 그룹에 합류.
PAGE_MINUTES = candle_store.PAGE_MINUTES
BACKFILL_MARGIN_S = 5          # 다음 경계 이 초 전에는 백필 중단(최신 수집 시간 확보)


def classify(symbols):
    """심볼을 (fast, backlog)로 나눈다. backlog = 신규이거나 최신에서 한 페이지(PAGE_MINUTES) 넘게 뒤처진 심볼."""
    fast, backlog = [], []
    for sym in symbols:
        gap = candle_store.tail_gap_minutes(sym)         # None = 캐시 없음(신규)
        (backlog if (gap is None or gap > PAGE_MINUTES) else fast).append(sym)
    return fast, backlog


def run_backfill(symbols, seed_days, with_funding, deadline, verbose=True):
    """백로그 심볼을 deadline까지 페이지 단위로 채운다. 최신에 근접하면 다음 사이클부터 폴링 합류."""
    for sym in symbols:
        if time.time() >= deadline:
            if verbose:
                print(f"[{_now()}] {sym:12s} 백필 대기 — 시간 부족, 다음 사이클에 계속", flush=True)
            break
        pages = 0
        while time.time() < deadline:
            try:
                fetched, caught = candle_store.backfill_step(sym, seed_days)
            except Exception as e:
                print(f"[{_now()}] {sym} 백필 실패: {e}", flush=True)
                break
            pages += 1
            if caught:
                if with_funding:
                    try:
                        candle_store.ensure_funding(sym, days=seed_days)
                    except Exception:
                        pass
                st = candle_store.stats(sym)
                if verbose:
                    print(f"[{_now()}] {sym:12s} 백필 완료 → 폴링 합류 (총 {st['count']:,}개)", flush=True)
                break
            if fetched == 0:                              # 소스에도 없음 → 더 진행 불가
                break
        else:                                             # while가 deadline으로 끝남(=아직 안 끝남)
            st = candle_store.stats(sym)
            if verbose:
                print(f"[{_now()}] {sym:12s} 백필 진행중 (+{pages}페이지, 총 {st['count']:,}개, "
                      f"최신 {_fmt(st['max'])}) — 다음 사이클에 계속", flush=True)


def collect_once(symbols, seed_days, with_funding=True, verbose=True):
    """각 심볼을 현재 시각까지 증분 수집. 반환: 총 신규 캔들 수."""
    total_new = 0
    for sym in symbols:
        before = candle_store.stats(sym)["count"]
        try:
            candle_store.ensure_days(sym, seed_days, verbose=False)
            if with_funding:
                candle_store.ensure_funding(sym, days=seed_days)
        except Exception as e:
            print(f"[{_now()}] {sym} 수집 실패: {e}")
            continue
        st = candle_store.stats(sym)
        new = st["count"] - before
        total_new += new
        if verbose:
            print(f"[{_now()}] {sym:12s} +{new:>4}개  (총 {st['count']:,}개, 최신 {_fmt(st['max'])} UTC)")
    return total_new


def load_watchlist(path):
    syms = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip().upper()
            if line:
                syms.append(line)
    return syms


def main():
    from .env import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser(description="독립 캔들 수집기 (SQLite 캐시 유지)")
    ap.add_argument("symbols", nargs="*", help="수집할 심볼 (예: BTCUSDT ETHUSDT)")
    ap.add_argument("--watchlist", help="심볼 목록 파일 (한 줄에 하나, # 주석)")
    ap.add_argument("--seed-days", type=float, default=7,
                    help="첫 수집(백필) 범위. 이미 있으면 최신분만 증분 (기본 7)")
    ap.add_argument("--loop", type=float, metavar="SEC",
                    help="N초마다 반복 수집. 생략하면 1회만")
    ap.add_argument("--heal-every", type=int, default=60, metavar="N",
                    help="N 사이클마다 내부 구멍 스캔·복구(자가치유). 기본 60(=루프60s면 1시간), 0=끔")
    ap.add_argument("--no-funding", action="store_true", help="펀딩비 수집 생략")
    args = ap.parse_args()

    symbols = list(args.symbols)
    if args.watchlist:
        symbols += load_watchlist(args.watchlist)
    symbols = list(dict.fromkeys(symbols))  # 중복 제거, 순서 유지
    if not symbols:
        ap.error("심볼을 지정하거나 --watchlist 를 줘")

    with_funding = not args.no_funding
    print(f"수집 대상: {', '.join(symbols)}  |  시드 {args.seed_days}일"
          f"{'  |  반복 %gs' % args.loop if args.loop else '  |  1회'}")

    if not args.loop:
        n = collect_once(symbols, args.seed_days, with_funding)
        print(f"완료: 신규 {n:,}개")
        return

    from . import control
    if control.get_symbols() is None:            # 최초 1회 시드 → 대시보드가 현재 목록을 보게
        control.set_symbols(symbols)
    cycle = 0
    try:
        while True:
            if control.service_state("collector") == "paused":
                print("  [멈춤] 수집 건너뜀", flush=True)
            else:
                active = control.get_symbols() or symbols   # 대시보드가 바꾸면 재시작 없이 반영
                fast, backlog = classify(active)
                # 1) 폴링 그룹(최신) — 깨어난 즉시 꼬리 봉만 빠르게 수집 (지연 최소)
                if fast:
                    collect_once(fast, args.seed_days, with_funding)
                # 2) 백필 그룹(신규/대량) — 남는 시간에만 페이지 단위로 따로 채움
                if backlog:
                    deadline = _next_boundary(args.loop) - BACKFILL_MARGIN_S
                    run_backfill(backlog, args.seed_days, with_funding, deadline)
                # 자가치유: N사이클마다 내부 구멍 스캔·복구 (최신 수집된 폴링 그룹만)
                if args.heal_every and cycle % args.heal_every == 0:
                    for sym in fast:
                        try:
                            candle_store.heal_gaps(sym, verbose=True)
                        except Exception as e:
                            print(f"[{_now()}] {sym} heal 실패: {e}")
            cycle += 1
            _sleep_aligned(args.loop)      # 다음 분 경계(:00+2초)에 깨어 방금 확정된 캔들을 수집
    except KeyboardInterrupt:
        print("\n수집기 종료")


if __name__ == "__main__":
    main()

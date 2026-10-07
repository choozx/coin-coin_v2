"""Q 1단계 — markout 측정용 수집기. 바이낸스 선물 **공개** 웹소켓만(키·주문 없음).

모으는 것(심볼마다 CSV 두 개, data/mm/<UTC 시작시각>/):
  <SYM>_book.csv   bookTicker — 최우선 호가가 바뀔 때마다  T,bid,bidq,ask,askq
  <SYM>_trade.csv  aggTrade   — 체결마다                   T,price,qty,m   (m=1: 매수자가 maker = 매도 공격)

분석은 research/mm_markout.py. 이 파일은 받아 적기만 한다.

웹소켓 라이브러리 없이 표준 라이브러리로 쓴다(수신 전용 — 텍스트·ping·close 프레임만 다루면 된다).
끊기면 다시 붙고, 끊긴 구간은 로그로 남긴다(분석 때 그 구간을 빼야 markout 이 안 오염된다).

    python3 -u -m research.mm_collect --secs 7000 ADAUSDC NEARUSDC ...
    python3 -u -m research.mm_collect --secs 3600 --depth BTCUSDT ETHUSDT ...   # + 호가 깊이(시장가 체결 비용)
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time

HOST = "fstream.binance.com"
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mm")


class WS:
    """최소 웹소켓 클라이언트(수신 전용). RFC 6455 중 필요한 것만."""

    def __init__(self, host: str, path: str, timeout: float = 30.0):
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("핸드셰이크 중 끊김")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        line = status.split(b"\r\n")[0]
        if b" 101 " not in line:
            raise ConnectionError(f"업그레이드 거부: {line!r}")
        self.buf = bytearray(rest)

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("소켓 닫힘")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send(self, op: int, payload: bytes = b""):
        mask = os.urandom(4)
        hdr = bytes([0x80 | op])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str:
        """다음 텍스트 메시지. ping 은 pong 으로 답하고 넘긴다. close 면 예외."""
        msg = bytearray()
        while True:
            b0, b1 = self._need(2)
            op, fin = b0 & 0x0F, b0 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            mask = self._need(4) if b1 & 0x80 else None
            data = self._need(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:                      # ping → pong (바이낸스는 3분마다, 10분 안에 답 없으면 끊음)
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise ConnectionError(f"서버 close {data[:2].hex()}")
            msg += data
            if fin:
                return msg.decode()

    def close(self):
        try:
            self._send(0x8)
            self.s.close()
        except OSError:
            pass


DEPTH_SIZES = (10_000, 50_000)             # 시장가 체결 비용을 잴 주문 크기($)


def walk(levels, usd: float, mid: float, side: int) -> float:
    """호가를 따라 usd 만큼 시장가로 먹었을 때 평균가의 mid 대비 비용(bp). 10단계로 모자라면 -1."""
    left, cost_qty, got_usd = usd, 0.0, 0.0
    for p, q in levels:
        p, q = float(p), float(q)
        take = min(left, p * q)
        cost_qty += take / p
        got_usd += take
        left -= take
        if left <= 1e-9:
            avg = got_usd / cost_qty
            return side * (avg - mid) / mid * 1e4
    return -1.0


def depth_row(x: dict) -> str:
    """depth10 한 장 → T, $1만·$5만 매수/매도 비용(bp), 10단계 누적 호가($) 매수·매도."""
    b, a = x["b"], x["a"]
    mid = (float(b[0][0]) + float(a[0][0])) / 2
    cells = [str(x["T"])]
    for usd in DEPTH_SIZES:
        cells += [f"{walk(a, usd, mid, 1):.3f}", f"{walk(b, usd, mid, -1):.3f}"]
    cells += [f"{sum(float(p) * float(q) for p, q in b):.0f}", f"{sum(float(p) * float(q) for p, q in a):.0f}"]
    return ",".join(cells)


def pump(kind: str, path: str, files: dict, t_end: float, gaps, stats: dict):
    """연결 하나를 t_end 까지 붙들고 받아 적는다. 끊기면 재연결, 끊긴 구간은 gaps 에."""
    down = reason = None
    while time.time() < t_end:
        try:
            ws = WS(HOST, path)
        except (OSError, ConnectionError) as e:
            print(f"  [{kind}] 연결 실패: {e} — 5초 뒤 재시도", flush=True)
            down = down or int(time.time() * 1000)
            reason = reason or "connect-fail"
            time.sleep(5)
            continue
        if down is not None:
            gaps.write(f"{kind},{down},{int(time.time() * 1000)},{reason}\n")
            gaps.flush()
            down = reason = None
        try:
            while time.time() < t_end:
                x = json.loads(ws.recv()).get("data", {})
                f = files.get(x.get("s"))
                if f is None:
                    continue
                if kind == "book":
                    f.write(f"{x['T']},{x['b']},{x['B']},{x['a']},{x['A']}\n")
                elif kind == "depth":
                    f.write(depth_row(x) + "\n")
                else:
                    f.write(f"{x['T']},{x['p']},{x['q']},{1 if x['m'] else 0}\n")
                stats[kind] += 1
        except (OSError, ConnectionError, ValueError) as e:
            down, reason = int(time.time() * 1000), str(e)[:60].replace(",", ";")
            print(f"  [{kind}] 끊김: {e} — 재연결", flush=True)
            time.sleep(1)
        finally:
            ws.close()


def main() -> int:
    args = sys.argv[1:]
    secs = 7000.0
    with_depth = "--depth" in args
    if with_depth:
        args.remove("--depth")
    if "--secs" in args:
        i = args.index("--secs")
        secs = float(args[i + 1])
        del args[i:i + 2]
    syms = [s.upper() for s in args]
    if not syms:
        print("심볼을 주세요")
        return 2
    out = os.path.join(ROOT, dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(out, exist_ok=True)
    book, trade = {}, {}
    for s in syms:
        book[s] = open(os.path.join(out, f"{s}_book.csv"), "w")
        trade[s] = open(os.path.join(out, f"{s}_trade.csv"), "w")
        book[s].write("T,bid,bidq,ask,askq\n")
        trade[s].write("T,price,qty,m\n")
    depth = {}
    if with_depth:
        for s in syms:
            depth[s] = open(os.path.join(out, f"{s}_depth.csv"), "w")
            depth[s].write("T," + ",".join(f"buy{u//1000}k,sell{u//1000}k" for u in DEPTH_SIZES) + ",bid10usd,ask10usd\n")
    gaps = open(os.path.join(out, "gaps.csv"), "w")
    gaps.write("kind,down_ms,up_ms,reason\n")
    # 2026 현재 바이낸스 선물 웹소켓은 경로가 갈려 있다: bookTicker 는 /public, aggTrade 는 /market
    # (옛 /stream 에선 aggTrade 가 조용히 안 온다 — 연결은 되는데 0건. 첫 시험에서 그렇게 걸렸다.)
    paths = {"book": "/public/stream?streams=" + "/".join(f"{s.lower()}@bookTicker" for s in syms),
             "trade": "/market/stream?streams=" + "/".join(f"{s.lower()}@aggTrade" for s in syms)}
    if with_depth:                                   # depth 도 /public (500ms 마다 10단계 스냅샷)
        paths["depth"] = "/public/stream?streams=" + "/".join(f"{s.lower()}@depth10@500ms" for s in syms)
    files_of = {"book": book, "trade": trade, "depth": depth}
    print(f"수집 → {out}  ({len(syms)}심볼, {secs/3600:.1f}시간)", flush=True)
    t_end = time.time() + secs
    stats = {k: 0 for k in paths}
    th = [threading.Thread(target=pump, args=(k, paths[k], files_of[k], t_end, gaps, stats), daemon=True)
          for k in paths]
    for t in th:
        t.start()
    last = {k: -1 for k in paths}
    while any(t.is_alive() for t in th):
        time.sleep(min(300, max(1, t_end - time.time())))
        for f in list(book.values()) + list(trade.values()) + list(depth.values()):
            f.flush()
        stalled = [k for k in stats if stats[k] == last[k]]
        last = dict(stats)
        print(f"  {dt.datetime.utcnow():%H:%M:%S} 호가 {stats['book']:,} · 체결 {stats['trade']:,} · "
              + (f"깊이 {stats['depth']:,} · " if with_depth else "")
              + f"남은 {max(0, t_end - time.time()) / 60:.0f}분" + (f"  ⚠ 정체: {stalled}" if stalled else ""),
              flush=True)
    for f in list(book.values()) + list(trade.values()) + list(depth.values()):
        f.close()
    gaps.close()
    print(f"끝 — 호가 {stats['book']:,} · 체결 {stats['trade']:,}  → {out}", flush=True)
    return 0 if stats["trade"] and stats["book"] else 1


if __name__ == "__main__":
    sys.exit(main())

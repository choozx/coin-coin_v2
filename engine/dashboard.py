"""매매 대시보드 — 봇이 기록한 상태(data/state.json)를 읽어 웹으로 보여준다.

봇(engine.live)과 '별개 프로세스'. read-only 모니터라 대시보드를 몇 번 재배포해도
실매매 봇·캔들 수집은 안 멈춘다(멀티서비스 구조). 상태는 공유 볼륨의 state.json으로 주고받음.

    python3 -m engine.dashboard --port 8080     # http://localhost:8080
    STATE_PATH=/app/data/state.json 로 상태 파일 경로 지정(도커).
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import control
from . import ledger
from .preset import list_strategies, select_strategy

_HTML = os.path.join(os.path.dirname(__file__), "dashboard.html")
STATE_PATH = os.environ.get("STATE_PATH", "data/state.json")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            with open(_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(200, json.dumps({"error": "상태 없음 — 봇이 아직 안 돌았거나 state.json 미생성"}))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/control":
            self._send(200, json.dumps(control.read_control()))
        elif self.path == "/api/strategies":
            self._send(200, json.dumps({"strategies": list_strategies()}))
        elif self.path.split("?")[0] == "/api/trades":
            from urllib.parse import parse_qs, urlparse
            mode = parse_qs(urlparse(self.path).query).get("mode", [None])[0]
            self._send(200, json.dumps({"trades": ledger.load(mode=mode, limit=1000)}))
        elif self.path.split("?")[0] == "/api/stats":
            from urllib.parse import parse_qs, urlparse
            mode = parse_qs(urlparse(self.path).query).get("mode", [None])[0]
            self._send(200, json.dumps(ledger.stats(mode=mode)))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/control":       # {"service":"trader"|"collector","state":...}
                ctrl = control.set_service(body["service"], body["state"])
            elif self.path == "/api/strategy":     # {"path": "presets/..."} 봇 전략 선택
                ctrl = select_strategy(body["path"])
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, json.dumps({"ok": True, "control": ctrl}))
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="매매 대시보드 (봇 상태 모니터)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"대시보드: http://localhost:{args.port}   (상태 파일: {STATE_PATH})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

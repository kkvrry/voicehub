"""限速验证：并发信号量管不住 RPM，闸口能。

本地起一个假服务端，按滑动窗口计成交请求数，超限返回 ``base_resp.status_code=1002``。
两种跑法都走真实的 ``minimax.client.synthesize``：一种不过闸口（旧行为），一种过
``app/engines/ratelimit.permit``（网关现在的路径）。

    ./.venv/Scripts/python.exe scripts/diag_rpm_limit.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import ratelimit
from app.engines.builtin.minimax import client as mm
from app.engines.builtin.minimax.settings import MinimaxSettings

AUDIO_HEX = (b"\x00" * 400).hex()
WINDOW = 6.0


class Quota:
    """滑动窗口内的成交配额。"""

    def __init__(self, limit: int, window: float, latency: float) -> None:
        self.limit = limit
        self.window = window
        self.latency = latency
        self.stamps: deque[float] = deque()
        self.lock = threading.Lock()
        self.arrivals: list[float] = []
        self.inflight = 0
        self.peak = 0

    def enter(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.arrivals.append(now)
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            while self.stamps and now - self.stamps[0] > self.window:
                self.stamps.popleft()
            if len(self.stamps) >= self.limit:
                self.inflight -= 1
                return False
            self.stamps.append(now)
        return True

    def leave(self) -> None:
        with self.lock:
            self.inflight -= 1

    def max_per_window(self) -> int:
        arr = sorted(self.arrivals)
        best, left = 0, 0
        for right, t in enumerate(arr):
            while t - arr[left] > self.window:
                left += 1
            best = max(best, right - left + 1)
        return best


def serve(quota: Quota) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            admitted = quota.enter()
            try:
                if admitted:
                    time.sleep(quota.latency)
                    body = {
                        "base_resp": {"status_code": 0, "status_msg": "success"},
                        "data": {"audio": AUDIO_HEX},
                        "extra_info": {"usage_characters": 10},
                    }
                else:
                    body = {
                        "base_resp": {
                            "status_code": 1002,
                            "status_msg": "rate limit exceeded(RPM)",
                        },
                        "data": {},
                    }
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            finally:
                if admitted:
                    quota.leave()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def run(rows: int, concurrency: int, rpm: int, quota_limit: int) -> dict:
    quota = Quota(quota_limit, WINDOW, 0.6)
    server = serve(quota)
    cfg = MinimaxSettings(
        api_key="test-key",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        concurrency=concurrency,
        rpm=rpm,
        timeout=30.0,
    )
    body = {
        "model": "speech-2.5-hd-preview",
        "text": "测试",
        "stream": False,
        "voice_setting": {"voice_id": "Chinese (Mandarin)_Crisp_Girl"},
        "audio_setting": {"format": "mp3"},
    }

    ok = 0
    bad = 0
    sem = asyncio.Semaphore(concurrency)
    started = time.perf_counter()

    async def one() -> None:
        nonlocal ok, bad
        async with sem:
            try:
                if rpm > 0:
                    async with ratelimit.permit("minimax", cfg, window=WINDOW):
                        await mm.synthesize(cfg, dict(body))
                else:
                    await mm.synthesize(cfg, dict(body))
                ok += 1
            except Exception:  # noqa: BLE001
                bad += 1

    await asyncio.gather(*(one() for _ in range(rows)))
    elapsed = time.perf_counter() - started
    server.shutdown()
    return {
        "gated": "否" if rpm == 0 else "是",
        "concurrency": concurrency,
        "rows": rows,
        "peak_inflight": quota.peak,
        "requests": len(quota.arrivals),
        "max_per_window": quota.max_per_window(),
        "quota": quota_limit,
        "ok": ok,
        "bad": bad,
        "seconds": round(elapsed, 1),
    }


async def main() -> None:
    rows = [
        await run(rows=16, concurrency=3, rpm=0, quota_limit=5),
        await run(rows=16, concurrency=3, rpm=5, quota_limit=5),
    ]
    print(f"窗口 {WINDOW:g}s，平台配额 {rows[0]['quota']} 次/窗口，单次耗时 0.6s")
    print(
        f"{'过闸':>4}{'并发':>6}{'行数':>6}{'在途峰值':>9}"
        f"{'请求数':>7}{'窗口内峰值':>11}{'成功':>6}{'被拒':>6}{'耗时s':>8}"
    )
    for r in rows:
        print(
            f"{r['gated']:>4}{r['concurrency']:>6}{r['rows']:>6}{r['peak_inflight']:>9}"
            f"{r['requests']:>7}{r['max_per_window']:>11}{r['ok']:>6}{r['bad']:>6}"
            f"{r['seconds']:>8}"
        )


if __name__ == "__main__":
    asyncio.run(main())

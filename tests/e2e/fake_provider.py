"""Deterministic OpenAI-compatible SSE server for browser tests."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages") or []
        prompt = str(messages[-1].get("content") or "") if messages else ""
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        chunks = ["slow "] * 40 if "slow" in prompt.lower() else ["hello ", "world"]
        try:
            for index, content in enumerate(chunks):
                payload = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": "stop" if index == len(chunks) - 1 else None,
                        }
                    ]
                }
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                if len(chunks) > 2:
                    time.sleep(0.1)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 19001), Handler).serve_forever()

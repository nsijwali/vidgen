"""Test doubles shared by the test files: a tone generator and a fake MCP text-to-speech server."""
from __future__ import annotations

import base64
import http.server
import io
import json
import threading
import wave

import numpy as np


def _tone_wav_bytes(seconds: float, rate: int = 22050, dc: float | None = None) -> bytes:
    """A mono 16-bit WAV: a 220 Hz tone, or with `dc` a constant level (so sample 0 is non-zero)."""
    t = np.arange(int(seconds * rate)) / rate
    if dc is None:
        pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype("<i2")
    else:
        pcm = np.full(len(t), int(dc * 32767), dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _fake_voice_server(seconds: float = 3.0, sse: bool = False, fail: bool = False, dc: float | None = None):
    """A stand-in for a local MCP text-to-speech server: initialize, notifications, tools/call generate_speech."""
    calls: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "id" not in msg:
                self.send_response(202); self.end_headers(); return
            if msg["method"] == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
            else:
                calls.append(msg["params"]["arguments"])
                if fail:
                    result = {"isError": True, "content": [{"type": "text", "text": "model not installed"}]}
                else:
                    payload = {"wav_base64": base64.b64encode(_tone_wav_bytes(seconds, dc=dc)).decode()}
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            if sse:
                body, ctype = f"event: message\ndata: {body}\n\n", "text/event-stream"
            else:
                ctype = "application/json"
            data = body.encode()
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Mcp-Session-Id", "s1"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def log_message(self, *a): pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls, f"http://127.0.0.1:{srv.server_address[1]}/mcp/"

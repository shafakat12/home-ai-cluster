#!/usr/bin/env python3
"""Home AI Cluster - GPU use report for the AI server (runs on each Linux GPU PC with an NVIDIA GPU).
Answers on port 50053, only to the AI server, with how busy this PC's GPU is from its OWN programs (games, video...).
The AI worker (rpc-server) is left out, so the server can switch this GPU to "Off" while you use the PC.
Usage: gpu-status.py <ai-server-ip> [port]"""
import json, subprocess, sys, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

SERVER = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 50053


def usage():
    ai = set(subprocess.run(["pgrep", "-f", "rpc-server"], capture_output=True, text=True).stdout.split())
    out = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"], capture_output=True, text=True, timeout=15).stdout
    head, own, ai_use, top = [], 0, 0, ("", 0)
    for line in out.splitlines():
        if line.startswith("# gpu"): head = line[1:].split(); continue
        if line.startswith("#") or not line.strip() or not head: continue
        f = line.split()
        try: pid, sm = f[head.index("pid")], f[head.index("sm")]
        except (ValueError, IndexError): continue
        if pid == "-" or sm == "-": continue
        sm = int(sm)
        if pid in ai: ai_use += sm
        else:
            own += sm
            if sm > top[1]: top = (f[-1], sm)
    return {"pc_pct": min(100, own), "ai_pct": min(100, ai_use), "top": top[0] if top[1] >= 5 else "",
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.client_address[0] not in (SERVER, "127.0.0.1", "::1"):
            code, body = 403, {"error": "only the AI server may ask"}
        else:
            try: code, body = 200, usage()
            except Exception: code, body = 503, {"error": "could not read GPU use (nvidia-smi missing?)"}
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

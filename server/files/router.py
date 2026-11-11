#!/usr/bin/env python3
"""
Home AI router: adds two "virtual" models on top of llama-swap.
  team : asks several models, then a judge model merges the best answer (mixture of agents)
  auto : picks the right model for each message (vision / code / deep / quick)
Started by llama-swap (see llama-swap.yaml). Python standard library only.
Which models are used is set in /opt/ai/router.json (written by the installer).
"""
import json, sys, re, os, threading, urllib.request, urllib.error, argparse, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ---------------- SETTINGS ----------------
UPSTREAM = os.environ.get("ROUTER_UPSTREAM", "http://127.0.0.1:8080/v1/chat/completions")   # llama-swap
TEAM_MEMBERS = ["qwen3.5-4b", "gemma-4-12b"]               # models that answer in parallel
TEAM_JUDGE = "gemma-4-12b"                                 # model that writes the final answer
AUTO = {
    "vision": "gemma-4-12b",   # messages with pictures
    "code":   "gpt-oss-20b",   # programming
    "deep":   "gemma-4-12b",   # long / detailed questions
    "quick":  "qwen3.5-4b",    # everyday chat
}
CLASSIFIER = "qwen3.5-4b"
# The installer / control panel keep the real choices in /opt/ai/router.json (overrides the values above).
try:
    _c = json.load(open("/opt/ai/router.json"))
    TEAM_MEMBERS = _c.get("team", TEAM_MEMBERS); TEAM_JUDGE = _c.get("judge", TEAM_JUDGE)
    CLASSIFIER = _c.get("classifier", CLASSIFIER); AUTO.update(_c.get("auto", {}))
except Exception:
    pass
# ------------------------------------------

CODE_HINT = re.compile(r"```|\b(code|coding|function|bug|error|exception|stack ?trace|python|javascript|typescript|"
                       r"php|laravel|sql|html|css|java|c\+\+|c#|golang|rust|bash|powershell|regex|api|compile|"
                       r"script|debug|refactor|git)\b", re.I)


def post(body, stream=False, timeout=1800):
    req = urllib.request.Request(UPSTREAM, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def complete(model, messages, **extra):
    body = {"model": model, "messages": messages, "stream": False}
    body.update(extra)
    with post(body) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"].get("content") or ""


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def has_image(messages):
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in ("image_url", "input_image") for p in c):
            return True
    return False


def choose(messages):
    if has_image(messages):
        return "vision", AUTO["vision"]
    last = text_of(next((m["content"] for m in reversed(messages) if m.get("role") == "user"), ""))
    try:
        ans = complete(CLASSIFIER, [
            {"role": "system", "content": "Classify the user's request. Reply with ONE word only: "
                                          "code (programming, scripts, errors, databases), "
                                          "deep (long explanation, analysis, planning, essays, research), "
                                          "quick (greeting, short question, simple chat)."},
            {"role": "user", "content": last[:4000]}],
            max_tokens=4, temperature=0, chat_template_kwargs={"enable_thinking": False}).strip().lower()
        for k in ("code", "deep", "quick"):
            if k in ans:
                return k, AUTO[k]
    except Exception as e:
        print("classifier failed:", e, file=sys.stderr)
    if CODE_HINT.search(last):
        return "code", AUTO["code"]
    return ("deep", AUTO["deep"]) if len(last) > 600 else ("quick", AUTO["quick"])


def team_messages(messages):
    answers = {}

    def ask(m):
        try:
            answers[m] = complete(m, messages)
        except Exception as e:
            answers[m] = f"(no answer: {e})"
    threads = [threading.Thread(target=ask, args=(m,)) for m in TEAM_MEMBERS]
    for t in threads: t.start()
    for t in threads: t.join()
    question = text_of(next((m["content"] for m in reversed(messages) if m.get("role") == "user"), ""))
    drafts = "\n\n".join(f"### Draft {i+1}\n{answers[m]}" for i, m in enumerate(TEAM_MEMBERS))
    judge = [m for m in messages if m.get("role") != "system"][:-1] + [{
        "role": "user",
        "content": f"Question:\n{question}\n\nSeveral assistants wrote these drafts:\n\n{drafts}\n\n"
                   "Write ONE final answer to the question: keep what is correct and useful from the drafts, "
                   "fix any mistakes, remove repetition. Answer in the same language as the question. "
                   "Do not mention drafts or assistants."}]
    sysmsgs = [m for m in messages if m.get("role") == "system"]
    return sysmsgs + judge


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        print("[router]", fmt % a, file=sys.stderr)

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok"})
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [{"id": "team", "object": "model"}, {"id": "auto", "object": "model"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": "only /v1/chat/completions is supported"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = body.get("model", "auto")
        messages = body.get("messages", [])
        stream = bool(body.get("stream"))
        t0 = time.time()
        try:
            if name == "team":
                target, messages = TEAM_JUDGE, team_messages(messages)
                note = f"team {TEAM_MEMBERS} -> judge {TEAM_JUDGE}"
            else:
                kind, target = choose(messages)
                note = f"auto -> {kind} -> {target}"
            print(f"[router] {note} (prep {time.time()-t0:.1f}s)", file=sys.stderr)
            fwd = {k: v for k, v in body.items() if k not in ("model", "messages")}
            fwd.update({"model": target, "messages": messages})
            r = post(fwd, stream=stream)
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": e.read().decode(errors="replace")})
        except Exception as e:
            return self._json(500, {"error": str(e)})
        # relay the chosen model's answer (streaming or not)
        self.send_response(r.status)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Router-Choice", target)
        self.end_headers()
        try:
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        finally:
            r.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    print(f"[router] listening on 127.0.0.1:{a.port}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()

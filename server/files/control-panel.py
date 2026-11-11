#!/usr/bin/python3 -I
"""Home AI Cluster - control panel (port 8091): models, GPUs, server + drive health, adding GPU PCs, settings.
Runs on the AI server as the admin user. Usable from any home device after logging in with the admin password.
The admin password is created once with the setup code printed by the installer (or from ADMIN_PC_IP)."""
import fcntl, json, os, re, glob, time, subprocess, ipaddress, urllib.request, urllib.parse, argparse, hashlib, hmac, secrets, threading, signal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

ADD = "/opt/ai/bin/add-model.sh"
CFG = "/opt/ai/llama-swap.yaml"
LOGS = "/opt/ai/logs"
POL = "/opt/ai/gpu-policy.json"
SUG = "/opt/ai/suggestions.json"
HELPERS = "/opt/ai/sbin"            # root-owned helpers the panel may run with sudo (see /etc/sudoers.d/home-ai)
SEC = "/opt/ai/secrets"
ALLOW = "/opt/ai/panel-allow.txt"
LLAMA = "/opt/ai/llama.cpp/current/llama-server"
SWAP = "http://127.0.0.1:8080"
VIRTUAL = {"team", "auto"}
HIDDEN = VIRTUAL | {"placeholder"}       # not shown as installed models
CONF_FILE = "/opt/ai/ai.conf"
RE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$", re.I)
RE_WORKER = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$")


def conf():
    d = {}
    try:
        for l in open(CONF_FILE):
            if "=" in l and not l.startswith("#"): k, v = l.strip().split("=", 1); d[k] = v
    except FileNotFoundError: pass
    return d


def notify(text):
    """Telegram message to the admin (if set up in Settings)."""
    c = read_env("telegram")
    if not (c.get("TOKEN") and c.get("CHAT_ID")): return False
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{c['TOKEN']}/sendMessage",
            data=json.dumps({"chat_id": c["CHAT_ID"], "text": text}).encode(), headers={"Content-Type": "application/json"}), timeout=15)
        return True
    except Exception: return False


def jload(p, d):
    try: return json.load(open(p))
    except Exception: return d


def write_file(path, text, mode=0o600):
    try:                                   # safe write: temp file + rename
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    except PermissionError:                # folder not writable (root-owned): write the file itself
        with open(path, "w") as f: f.write(text)
        return
    with os.fdopen(fd, "w") as f: f.write(text)
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)


def read_env(name):
    d = {}
    try:
        for l in open(f"{SEC}/{name}.env"):
            if "=" in l: k, v = l.strip().split("=", 1); d[k] = v
    except FileNotFoundError: pass
    return d


def write_env(name, d):
    for v in d.values():
        if any(c in str(v) for c in "\r\n\0"): raise ValueError("invalid characters")
    write_file(f"{SEC}/{name}.env", "".join(f"{k}={v}\n" for k, v in d.items()))


PASSF = f"{SEC}/panel-pass"
SETUP_CODE = f"{SEC}/setup-code"
SESSF = f"{SEC}/panel-sessions.json"
API_ALLOW = "/opt/ai/api-allow.txt"
SESSION_DAYS = 30
_lock = threading.Lock()
_fails = {}                       # ip -> [timestamps of failed logins]


def _hash_pw(pw, salt=None, it=310000):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2_sha256${it}${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), it).hex()}"


def _check_pw(pw):
    try:
        _, it, salt, h = open(PASSF).read().strip().split("$")
        return hmac.compare_digest(_hash_pw(pw, salt, int(it)).split("$")[3], h)
    except Exception:
        return False


def _sessions():
    s = jload(SESSF, {}); now = time.time()
    return {k: v for k, v in s.items() if v.get("exp", 0) > now}


def new_session(ip, ua):
    tok = secrets.token_urlsafe(32)
    with _lock:
        s = _sessions(); s[hashlib.sha256(tok.encode()).hexdigest()] = {"exp": time.time() + SESSION_DAYS * 86400, "ip": ip, "ua": ua[:120],
                                                                         "created": time.strftime("%Y-%m-%d %H:%M")}
        write_file(SESSF, json.dumps(s, indent=1))
    return tok


def session_ok(tok):
    return bool(tok) and hashlib.sha256(tok.encode()).hexdigest() in _sessions()


def end_sessions(tok=None):
    with _lock:
        s = _sessions()
        if tok: s.pop(hashlib.sha256(tok.encode()).hexdigest(), None)
        else: s = {}
        write_file(SESSF, json.dumps(s, indent=1))


def too_many_fails(ip):
    now = time.time(); f = [t for t in _fails.get(ip, []) if now - t < 600]; _fails[ip] = f
    return len(f) >= 5


def api_devices():
    try: return [l.strip() for l in open(API_ALLOW) if l.strip() and not l.startswith("#")]
    except FileNotFoundError: return []


def set_api_devices(ips):
    out = []
    for ip in ips:
        ip = str(ip).strip()
        if not ip: continue
        a = ipaddress.ip_address(ip)
        if not a.is_private or a.version != 4: raise ValueError(f"{ip} is not a home-network address")
        out.append(str(a))
    write_file(API_ALLOW, "".join(f"{i}\n" for i in sorted(set(out))), 0o644)
    r = subprocess.run(["sudo", "-n", f"{HELPERS}/api-allow-sync"], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip() or "Saved."


def allowed_ips():
    try: return {l.strip() for l in open(ALLOW) if l.strip() and not l.startswith("#")}
    except FileNotFoundError: return set()


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r: return r.read()


# ------------------------------------------------------------------ models
def hf(path):
    req = urllib.request.Request("https://huggingface.co" + path, headers={"User-Agent": "home-ai-control-panel"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())


def installed():
    names, cur, inmodels = [], None, False
    for line in open(CFG):
        if re.match(r"^\S", line):
            inmodels = line.startswith("models:"); cur = None; continue
        if not inmodels: continue
        m = re.match(r'^  "([^"]+)":', line)
        if m:
            cur = m.group(1)
            if cur not in HIDDEN: names.append({"name": cur, "mode": "", "file": ""})
        elif cur and names and names[-1]["name"] == cur:
            mm = re.search(r"llama-run\.sh (\w+)", line)
            if mm: names[-1]["mode"] = mm.group(1)
            mf = re.search(r"-m (\S+\.gguf)", line)
            if mf:
                names[-1]["file"] = os.path.basename(mf.group(1))
                try: names[-1]["gb"] = round(os.path.getsize(mf.group(1)) / 2**30, 1)
                except OSError: names[-1]["gb"] = None
    return names


def progress():
    out = []
    for f in sorted(glob.glob(f"{LOGS}/add-*.log"), key=os.path.getmtime, reverse=True)[:8]:
        txt = open(f, errors="replace").read().replace("\r", "\n")
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        last = lines[-1] if lines else ""
        pct = None
        for l in reversed(lines):
            m = re.match(r"^\s*(\d{1,3})\s", l)
            if m: pct = int(m.group(1)); break
        out.append({"name": os.path.basename(f)[4:-4], "done": "READY" in txt,
                    "failed": "curl:" in last or "FAILED" in txt, "pct": pct, "last": last[-160:]})
    return out


# ------------------------------------------------------------------ GPUs
_gpu_cache = {"t": 0, "workers": {}}


def server_gpu():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        n, t, u, g = [x.strip() for x in o.split(",")]
        return {"name": n, "total": int(t), "used": int(u), "util": int(g), "online": True}
    except Exception:
        return {"name": "server GPU", "online": False}


def probe_worker(w):
    try:
        o = subprocess.run([LLAMA, "--rpc", w, "--list-devices"], capture_output=True, text=True, timeout=6).stdout
        m = re.search(r"RPC\d+: \S+ \((\d+) MiB, (\d+) MiB free\)", o)
        if m: return {"online": True, "total": int(m.group(1)), "used": int(m.group(1)) - int(m.group(2))}
        return {"online": True, "slow": True}
    except subprocess.TimeoutExpired:
        return {"online": True, "slow": True}
    except Exception:
        return {"online": False}


def workers_in_use():
    """GPU PCs that a running llama-server is connected to right now (from its --rpc list).
    A GPU PC serves one model at a time and doesn't answer other connections meanwhile,
    so without this check it would look offline exactly while it is working."""
    used = set()
    try: out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=5).stdout
    except Exception: return used
    for line in out.splitlines():
        if os.path.basename(line.split(" ", 1)[0]) != "llama-server": continue
        for m in re.finditer(r"--rpc[ =](\S+)", line):
            used.update(w.strip() for w in m.group(1).split(",") if RE_WORKER.match(w.strip()))
    return used


# ---------------------------------------------------------------- GPU auto-off
# Each GPU PC reports how much its OWN programs (a game, video...) use its GPU (gpu-status on port 50053).
# Above the limit for two checks in a row, the GPU is switched to Off and the models using it are stopped
# (they restart without it on the next message). After N minutes below the limit it goes back to its old mode.
GUARD = "/opt/ai/gpu-guard.json"
STATUS_PORT = 50053
_guard = {"hits": {}}
_guard_lock = threading.Lock()


def _guard_note(text):
    try:
        os.makedirs("/var/lib/home-ai", exist_ok=True)
    except OSError: pass
    notify(text)


def guard_settings(pol=None):
    pol = pol if pol is not None else jload(POL, {})
    try: pct = max(0, min(100, int(pol.get("auto_off_pct", 40))))
    except (TypeError, ValueError): pct = 40
    try: back = max(1, min(120, int(pol.get("auto_back_min", 5))))
    except (TypeError, ValueError): back = 5
    return pct, back


def set_guard_settings(pct, back):
    try: pct, back = int(pct), int(back)
    except (TypeError, ValueError): raise ValueError("Use whole numbers.")
    if not (0 <= pct <= 100 and 1 <= back <= 120): raise ValueError("Use 0-100 % (0 = never) and 1-120 minutes.")
    pol = jload(POL, {}); pol["auto_off_pct"] = pct; pol["auto_back_min"] = back
    write_file(POL, json.dumps(pol, indent=1), 0o644)
    if pct == 0: return "Saved. GPU PCs are never switched off automatically."
    return f"Saved. A GPU PC switches to Off while its own programs use its GPU more than {pct}%, and goes back after {back} min below that."


def stop_models_on(w):
    """Stop the llama-server processes that use GPU PC w; llama-swap starts them again without it on the next message."""
    try: out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception: return 0
    n = 0
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        exe = args.split(" ", 1)[0]
        if os.path.basename(exe) == "llama-server" and any(w in m.group(1).split(",") for m in re.finditer(r"--rpc[ =](\S+)", args)):
            try: os.kill(int(pid), signal.SIGTERM); n += 1
            except (OSError, ValueError): pass
    return n


def _guard_set_mode(w, mode):
    pol = jload(POL, {}); pol.setdefault("workers", {})[w] = mode
    write_file(POL, json.dumps(pol, indent=1), 0o644)


def guard_tick():
    with _guard_lock: _guard_tick()


def forget_gpu(key):
    """Remove an offline GPU PC (name, mode, auto-off state), e.g. after it got a new IP address."""
    if not RE_WORKER.match(key): raise ValueError("bad gpu")
    if key in workers_in_use() or _gpu_cache["workers"].get(key, {}).get("online"):
        raise ValueError("This GPU PC is online. Only offline GPU PCs can be forgotten.")
    with _guard_lock:
        pol = jload(POL, {}); pol.get("workers", {}).pop(key, None); pol.get("names", {}).pop(key, None)
        write_file(POL, json.dumps(pol, indent=1), 0o644)
        st = jload(GUARD, {}); st.get("workers", {}).pop(key, None)
        write_file(GUARD, json.dumps(st, indent=1), 0o644)
        _gpu_cache["workers"].pop(key, None); _guard["hits"].pop(key, None)
    return "Forgotten. If a PC answers at that address again, it shows up as a new GPU."


def _guard_tick():
    pol = jload(POL, {}); thr, back = guard_settings(pol)
    st = jload(GUARD, {}); ws = st.setdefault("workers", {})
    now = time.time(); names = pol.get("names", {})
    for w in sorted(set(pol.get("workers", {})) | set(_gpu_cache["workers"]) | set(ws) | workers_in_use()):
        if not RE_WORKER.match(w): continue
        e = ws.setdefault(w, {})
        try:
            r = json.loads(get(f"http://{w.split(':')[0]}:{STATUS_PORT}/", 3))
            pct = max(0, min(100, int(r.get("pc_pct", 0))))
            e.update(reporter=True, pct=pct, top=str(r.get("top") or "")[:60], seen=now)
        except Exception:
            e["reporter"] = False; pct = None
        mode = pol.get("workers", {}).get(w, "use"); name = names.get(w) or w.split(":")[0]
        if e.get("auto_off") and mode != "off":               # changed by hand in the panel: respect it until the PC is quiet
            e.update(auto_off=False, hold=True, low_since=None)
        if pct is None: continue                               # PC off or no reporter: change nothing
        if e.get("hold") and pct <= thr: e["hold"] = False
        if e.get("auto_off"):
            if thr == 0 or pct <= thr:
                e["low_since"] = e.get("low_since") or now
                if thr == 0 or now - e["low_since"] >= back * 60:
                    prev = e.get("prev", "use")
                    _guard_set_mode(w, prev); e.update(auto_off=False, low_since=None)
                    _guard_note(f"✅ {name}: auto-off was turned off, so the AI may use this GPU again ({prev})." if thr == 0 else
                                f"✅ {name}: its PC is free again ({pct}%), so the AI may use this GPU again ({prev}).")
            else:
                e["low_since"] = None
        elif thr > 0 and pct > thr and mode in ("use", "standby") and not e.get("hold"):
            _guard["hits"][w] = _guard["hits"].get(w, 0) + 1
            if _guard["hits"][w] >= 2:                             # two checks in a row (about 10 s), not a short spike
                _guard["hits"][w] = 0
                _guard_set_mode(w, "off"); e.update(auto_off=True, prev=mode, since=now, low_since=None)
                n = stop_models_on(w)
                top = f", {e['top']}" if e.get("top") else ""
                stopped = f" ({n} model(s) stopped; they restart without it)" if n else ""
                _guard_note(f"🎮 {name} is busy on its PC ({pct}%{top}). The AI stopped using it{stopped} "
                            f"and switches it back after {back} min below {thr}%.")
        else:
            _guard["hits"][w] = 0
    write_file(GUARD, json.dumps(st, indent=1), 0o644)


def guard_loop():
    while True:
        try: guard_tick()
        except Exception as ex: print("gpu auto-off:", ex, flush=True)
        time.sleep(5)


def gpus():
    pol = jload(POL, {})
    inuse = workers_in_use()
    if time.time() - _gpu_cache["t"] > 20:
        reach = subprocess.run(["/opt/ai/bin/find-workers.sh"], capture_output=True, text=True, timeout=30).stdout.strip()
        reach = [w for w in reach.split(",") if w]
        ws = {}
        for w in sorted(set(reach) | set(pol.get("workers", {})) | set(_gpu_cache["workers"]) | inuse):
            if w in inuse: ws[w] = {"online": True, "busy": True}      # don't probe a GPU PC that is working
            elif w in reach: ws[w] = probe_worker(w)
            else: ws[w] = {"online": False}
        _gpu_cache.update(t=time.time(), workers=ws)
    for w in inuse:                                                     # a model may have started since the last scan
        _gpu_cache["workers"][w] = {"online": True, "busy": True}
    try: running = json.loads(get(f"{SWAP}/running")).get("running", [])
    except Exception: running = []
    names = pol.get("names", {}); gst = jload(GUARD, {}).get("workers", {})
    sg = server_gpu()
    rows = [{"key": "server", "where": f"AI server ({conf().get('SERVER_IP', '')})", **sg, "model": sg.get("name", ""),
             "name": names.get("server") or sg.get("name", "server GPU"), "mode": pol.get("server", "use")}]
    for w, info in _gpu_cache["workers"].items():
        g = gst.get(w, {})
        rows.append({"key": w, "where": f"GPU worker PC {w.split(':')[0]}", **info, "model": "",
                     "name": names.get(w) or "worker GPU", "mode": pol.get("workers", {}).get(w, "use"),
                     "pc_pct": g.get("pct") if g.get("reporter") else None, "pc_top": g.get("top", ""),
                     "reporter": g.get("reporter"), "auto_off": bool(g.get("auto_off"))})
    pct, back = guard_settings(pol)
    return {"gpus": rows, "running": running, "guard": {"pct": pct, "back": back}}


def set_gpu_name(key, name):
    if key != "server" and not RE_WORKER.match(key): raise ValueError("bad gpu")
    name = re.sub(r"[^\w .()+-]", "", str(name)).strip()[:40]
    pol = jload(POL, {})
    if name: pol.setdefault("names", {})[key] = name
    else: pol.get("names", {}).pop(key, None)
    write_file(POL, json.dumps(pol, indent=1), 0o644)
    return "Name saved."


RESERVED = VIRTUAL | {"duo", "big", "router", "placeholder"}


def rename_model(old, new):
    new = str(new).strip().lower()
    names = [x["name"] for x in installed()]
    if old not in names: raise ValueError("model not found")
    if not RE_NAME.match(new) or new in RESERVED: raise ValueError("Use letters, numbers, dot, dash or underscore (not team/auto)")
    if new in names: raise ValueError("A model with that name already exists")
    rp = "/opt/ai/router.json"
    try: router = open(rp).read()
    except OSError: router = ""
    txt = open(CFG).read()
    head, sep, body = txt.partition("\nmodels:\n")
    if not sep: raise ValueError("could not read the model list")
    body = re.sub(rf'(?m)^  "{re.escape(old)}":', f'  "{new}":', body, count=1)
    head = head.replace(f'"{old}"', f'"{new}"')          # group member lists
    open(CFG + ".bak-rename", "w").write(txt)
    write_file(CFG, head + sep + body, 0o644)
    if f'"{old}"' in router:                              # keep the team/auto models working
        open(rp + ".bak-rename", "w").write(router)
        write_file(rp, router.replace(f'"{old}"', f'"{new}"'), 0o644)
        try: get(f"{SWAP}/unload", 30)
        except Exception: pass
    return f"Renamed {old} -> {new}. It shows with the new name in Open WebUI after a page refresh (old chats keep the old name)."


def set_policy(key, value):
    if value not in ("use", "standby", "off"): raise ValueError("bad mode")
    pol = jload(POL, {})
    if key == "server": pol["server"] = value
    elif RE_WORKER.match(key): pol.setdefault("workers", {})[key] = value
    else: raise ValueError("bad gpu")
    write_file(POL, json.dumps(pol, indent=1), 0o644)
    try: get(f"{SWAP}/unload", 30)
    except Exception: pass
    return "Saved. Running models were stopped; the next chat starts them with the new GPU choice."


_prev = {}
SERVICES = [("llama-swap", "AI models (llama-swap)"), ("open-webui", "Chat website (Open WebUI)"), ("ai-control-panel", "Control panel"),
            ("ai-worker-kit", "Worker kit (port 8090)"), ("docker", "Docker"), ("ai-health-check.timer", "Health check (10 min)"),
            ("ai-backup.timer", "Nightly backup"), ("ai-model-scout.timer", "Weekly model finder")]
ENDPOINTS = [("http://127.0.0.1:8080/health", "AI API"), ("http://127.0.0.1:3000/health", "Chat website"),
             ("http://127.0.0.1:8888/healthz", "Web search")]


def _cpu_times():
    v = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(v), v[3] + v[4]


def _iface():
    try:
        for l in open("/proc/net/route").read().splitlines()[1:]:
            f = l.split()
            if f[1] == "00000000": return f[0]
    except Exception: pass
    return "eth0"


def _temps():
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try: name = open(f"{h}/name").read().strip()
        except Exception: continue
        if name not in ("coretemp", "k10temp", "zenpower", "nvme", "acpitz", "drivetemp", "it87", "nct6775"): continue
        for t in sorted(glob.glob(f"{h}/temp*_input")):
            try:
                c = int(open(t).read()) / 1000
                lab = open(t.replace("_input", "_label")).read().strip() if os.path.exists(t.replace("_input", "_label")) else name
            except Exception: continue
            if lab.startswith("Core "): continue          # keep the summary (Package / Tctl) only
            if 0 < c < 130: out.append({"name": f"{name}: {lab}", "c": round(c)})
    return out[:8]


def raids(text=None):
    """RAID arrays from /proc/mdstat: level, state, member drives, failed drives, rebuild progress."""
    if text is None:
        try: text = open("/proc/mdstat").read()
        except Exception: return []
    labels = {}
    try:
        for n in os.listdir("/dev/md"): labels[os.path.basename(os.path.realpath("/dev/md/" + n))] = n
    except Exception: pass
    out, cur = [], None
    for l in text.splitlines():
        m = re.match(r"^(md\w+) : (\w+)\s*(.*)$", l)
        if m:
            parts = m.group(3).split()
            mem = [(re.match(r"(\w+)\[\d+\]", p), p) for p in parts]
            cur = {"name": m.group(1), "label": labels.get(m.group(1), ""), "state": m.group(2),
                   "level": next((p for p in parts if p.startswith("raid") or p == "linear"), ""),
                   "members": [x.group(1) for x, p in mem if x], "failed": [x.group(1) for x, p in mem if x and p.endswith("(F)")],
                   "disks": None, "disks_ok": None, "sync": None}
            out.append(cur); continue
        if not cur: continue
        s = re.search(r"\[(\d+)/(\d+)\] \[[U_]+\]", l)
        if s: cur["disks"], cur["disks_ok"] = int(s.group(1)), int(s.group(2))
        s = re.search(r"(resync|recovery|reshape|check)\s*=\s*([\d.]+)%.*?finish=([\d.]+)min", l)
        if s: cur["sync"] = {"what": s.group(1), "pct": float(s.group(2)), "eta_min": round(float(s.group(3)))}
    for r in out:
        r["degraded"] = r["state"] != "active" or bool(r["failed"]) or (r["disks"] is not None and r["disks_ok"] < r["disks"])
        r["problem"] = "" if not r["degraded"] else (
            f"RAID {r['label'] or r['name']} is not running ({r['state']})" if r["state"] != "active" else
            f"RAID {r['label'] or r['name']} is degraded - a drive failed or is missing" + (f" ({', '.join(r['failed'])})" if r["failed"] else "")
            + ". It still works, but replace the drive soon (docs/storage.md)")
    return out


def server_status():
    now = time.time()
    tot, idle = _cpu_times(); nic = _iface()
    try:
        rx = int(open(f"/sys/class/net/{nic}/statistics/rx_bytes").read()); tx = int(open(f"/sys/class/net/{nic}/statistics/tx_bytes").read())
    except Exception: rx = tx = 0
    pv = _prev.get("s"); _prev["s"] = (now, tot, idle, rx, tx)
    cpu = rxr = txr = None
    if pv and now - pv[0] > 0.5:
        dt = tot - pv[1]; cpu = round(100 * (1 - (idle - pv[2]) / dt), 1) if dt else 0
        rxr = round((rx - pv[3]) / (now - pv[0]) / 1e6, 2); txr = round((tx - pv[4]) / (now - pv[0]) / 1e6, 2)
    mem = {}
    for l in open("/proc/meminfo"):
        k, v = l.split(":", 1); mem[k] = int(v.split()[0]) * 1024
    la = open("/proc/loadavg").read().split()[:3]
    up = float(open("/proc/uptime").read().split()[0])
    model = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "")
    try: speed = int(open(f"/sys/class/net/{nic}/speed").read())
    except Exception: speed = None
    gpu = []
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8).stdout
        for l in o.strip().splitlines():
            n, t, u, mu, mt, pw, fan = [x.strip() for x in l.split(",")]
            num = lambda x: None if x in ("[N/A]", "N/A", "") else float(x)
            gpu.append({"name": n, "temp": num(t), "util": num(u), "mem_used": num(mu), "mem_total": num(mt), "power": num(pw), "fan": num(fan)})
    except Exception: pass
    disks = []
    c = conf(); mounts = []
    for path in ("/", c.get("MODELS_DIR", "/opt/ai/models"), c.get("BACKUP_DIR", "/opt/ai/backups")):
        p = os.path.realpath(path)
        while p != "/" and not os.path.ismount(p): p = os.path.dirname(p)
        if p not in mounts: mounts.append(p)
    for m in mounts:
        try:
            st = os.statvfs(m); total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            disks.append({"mount": m, "total_gb": round(total / 1e9), "used_gb": round((total - free) / 1e9), "pct": round(100 * (total - free) / total)})
        except Exception: pass
    svc = [(u, lab) for u, lab in SERVICES if u == "docker" or os.path.exists(f"/etc/systemd/system/{u}")
           or os.path.exists(f"/etc/systemd/system/{u}.service")]
    names = [u for u, _ in svc]
    try: act = subprocess.run(["systemctl", "is-active", *names], capture_output=True, text=True, timeout=10).stdout.split()
    except Exception: act = []
    services = [{"unit": u, "label": lab, "state": act[i] if i < len(act) else "unknown"} for i, (u, lab) in enumerate(svc)]
    def probe(item):
        url, lab = item; t0 = time.time()
        try: get(url, 2); return {"label": lab, "ok": True, "ms": round((time.time() - t0) * 1000)}
        except Exception: return {"label": lab, "ok": False}
    epl = [e for e in ENDPOINTS if not (e[1] == "Chat website" and "open-webui" not in names)
           and not (e[1] == "Web search" and not os.path.exists("/opt/ai/searxng/settings.yml"))]
    with ThreadPoolExecutor(len(epl)) as ex: eps = list(ex.map(probe, epl))
    try: bk = subprocess.run(["systemctl", "show", "ai-backup.service", "-p", "ExecMainExitTimestamp", "-p", "Result"], capture_output=True, text=True, timeout=10).stdout
    except Exception: bk = ""
    bkd = dict(l.split("=", 1) for l in bk.splitlines() if "=" in l)
    try: running = json.loads(get(f"{SWAP}/running")).get("running", [])
    except Exception: running = []
    temps = _temps()
    problems = [f"{x['label']} is {x['state']}" for x in services if x["state"] not in ("active",)]
    problems += [f"{e['label']} not answering" for e in eps if not e["ok"]]
    memp = round(100 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]))
    if memp >= 92: problems.append(f"Memory almost full ({memp}%)")
    problems += [f"Disk {d['mount']} {d['pct']}% full" for d in disks if d["pct"] >= 90]
    rd = raids()
    problems += [r["problem"] for r in rd if r["problem"]]
    problems += [f"Hot: {t['name']} {t['c']} °C" for t in temps if t["c"] >= 88]
    problems += [f"GPU hot: {g['temp']} °C" for g in gpu if g["temp"] and g["temp"] >= 85]
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "host": os.uname().nodename, "kernel": os.uname().release,
            "uptime_s": int(up), "cpu": {"model": model, "cores": os.cpu_count(), "pct": cpu, "load": la},
            "mem": {"total": mem["MemTotal"], "avail": mem.get("MemAvailable", 0), "pct": memp,
                    "swap_total": mem.get("SwapTotal", 0), "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)},
            "temps": temps, "gpu": gpu, "net": {"iface": nic, "speed": speed, "rx_mbs": rxr, "tx_mbs": txr}, "disks": disks,
            "raids": rd, "models_dir": c.get("MODELS_DIR", "/opt/ai/models"), "backup_dir": c.get("BACKUP_DIR", "/opt/ai/backups"),
            "services": services, "endpoints": eps, "running": running,
            "backup": {"last": bkd.get("ExecMainExitTimestamp", ""), "result": bkd.get("Result", "")}, "problems": problems}


_drive_cache = {"t": 0, "data": None}


def drives(force=False):
    if force or time.time() - _drive_cache["t"] > 5 or not _drive_cache["data"]:
        r = subprocess.run(["sudo", "-n", f"{HELPERS}/disk-health", "json"], capture_output=True, text=True, timeout=90)
        try: _drive_cache.update(t=time.time(), data=json.loads(r.stdout))
        except Exception: return {"error": (r.stderr or r.stdout or "could not read disks").strip()[-300:]}
    return _drive_cache["data"]


# ---------------------------------------------------------------- storage (models / backups on a folder, drive or RAID)
STORAGE_TOOL = f"{HELPERS}/ai-storage"
STORAGE_LOG = f"{LOGS}/storage-job.log"
STORAGE_LOCK = "/run/ai-storage.lock"
RE_PATH = re.compile(r"^/[A-Za-z0-9._/+-]{1,200}$")
_st_cache = {"t": 0, "data": None}
_PROGRESS = re.compile(r"^\s*[\d,.]+[KMG]?\s+\d+%")


def storage_running():
    """True while ai-storage changes something (it holds an exclusive lock on STORAGE_LOCK)."""
    try: fd = os.open(STORAGE_LOCK, os.O_RDONLY)
    except OSError: return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); return False
    except OSError: return True
    finally: os.close(fd)


def storage_job():
    running = storage_running()
    try: raw = open(STORAGE_LOG, errors="replace").read()[-100000:]; mt = os.path.getmtime(STORAGE_LOG)
    except OSError: return {"running": running, "log": "", "result": "", "time": ""}
    m = re.search(r"AI-STORAGE-EXIT=(\d+)", raw)
    if m: result = "ok" if m.group(1) == "0" else "failed"
    else: result = "running" if running or time.time() - mt < 15 else "stopped"
    out = []
    for l in raw.replace("\r", "\n").split("\n"):
        l = l.rstrip()
        if not l.strip() or l.startswith("AI-STORAGE-EXIT="): continue
        if _PROGRESS.match(l) and out and _PROGRESS.match(out[-1]): out[-1] = l      # one live line for copy progress
        else: out.append(l)
    return {"running": result == "running", "log": "\n".join(out[-60:]), "result": result,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))}


def storage_info(force=False):
    if force or time.time() - _st_cache["t"] > 3 or not _st_cache["data"]:
        r = subprocess.run(["sudo", "-n", STORAGE_TOOL, "json"], capture_output=True, text=True, timeout=60)
        try: _st_cache.update(t=time.time(), data=json.loads(r.stdout))
        except Exception: return {"error": (r.stderr or r.stdout or "could not read the drives").strip()[-400:], "job": storage_job()}
    d = dict(_st_cache["data"]); d["raids"] = raids(); d["job"] = storage_job()
    return d


def storage_args(b):
    """Checked ai-storage arguments for a request from the page. Returns (args, erases_drives)."""
    kind = b.get("kind")
    if kind in ("folder", "backups"):
        path = str(b.get("path", "")).strip().rstrip("/") or "/"
        if not RE_PATH.match(path) or ".." in path:
            raise ValueError("Use a full folder path like /mnt/data/ai-models (letters, numbers and . _ - + / only).")
        return [kind, path] + (["--accept-warnings"] if b.get("accept") else []), False
    if kind in ("drive", "raid"):
        info = storage_info(force=True)
        if info.get("error"): raise ValueError(info["error"])
        usable = {d["dev"] for d in info["disks"] if d["ok"]}
        disks = [str(x) for x in b.get("disks", [])]
        if not disks or len(set(disks)) != len(disks) or any(d not in usable for d in disks):
            raise ValueError("Tick drives from the list - only drives marked 'can be used'.")
        bk = ["--backups"] if b.get("backups") else ["--no-backups"]
        if kind == "drive":
            if len(disks) != 1: raise ValueError("'One drive' needs exactly one ticked drive. For several drives choose a RAID.")
            return ["drive", disks[0]] + bk, True
        level = str(b.get("level", ""))
        if level not in ("0", "1", "5", "10"): raise ValueError("Choose RAID 0, 1, 5 or 10.")
        return ["raid", level] + disks + bk, True
    raise ValueError("unknown storage action")


WORKERS_TXT = "/opt/ai/workers.txt"


def extra_workers():
    try: return [l.strip() for l in open(WORKERS_TXT) if l.strip() and not l.startswith("#")]
    except FileNotFoundError: return []


def set_extra_workers(items):
    out = []
    for w in items:
        w = str(w).strip()
        if not w: continue
        host, _, port = w.partition(":")
        a = ipaddress.ip_address(host)
        if not a.is_private or a.version != 4: raise ValueError(f"{w}: must be a home-network IPv4 address")
        if port and not (port.isdigit() and 1 <= int(port) <= 65535): raise ValueError(f"{w}: bad port")
        out.append(f"{a}:{port or 50052}")
    write_file(WORKERS_TXT, "# GPU worker PCs on other networks (ip:port), added in the control panel\n" + "".join(f"{w}\n" for w in out), 0o644)
    _gpu_cache["t"] = 0
    return "Saved. The server will try these PCs the next time a model starts."


# ------------------------------------------------------------------ settings
def settings():
    tg = read_env("telegram")
    return {"telegram": {"set": bool(tg.get("TOKEN")), "chat_id": tg.get("CHAT_ID", "")}, "allow": sorted(allowed_ips()),
            "api_devices": api_devices(),
            "sessions": [{"ip": v.get("ip"), "ua": v.get("ua"), "created": v.get("created")} for v in _sessions().values()]}


RE_TOKEN = re.compile(r"^\d{5,15}:[\w-]{30,60}$")


def save_settings(b):
    if b.get("section") == "telegram":
        tok, cid = b.get("token", "").strip(), b.get("chat_id", "").strip()
        if not re.match(r"^\d{5,15}$", cid): raise ValueError("Your Telegram user ID is a number (get it from @userinfobot)")
        old = read_env("telegram")
        if tok and not RE_TOKEN.match(tok): raise ValueError("That doesn't look like a bot token")
        if not (tok or old.get("TOKEN")): raise ValueError("Paste the bot token")
        write_env("telegram", {"TOKEN": tok or old.get("TOKEN", ""), "CHAT_ID": cid})
        ok = notify("✅ Home AI alerts are connected. Disk problems, stopped services and new model suggestions will arrive here.")
        return "Saved - a test message was sent." if ok else "Saved, but the test message failed (press Start in your bot chat first, check the token)."
    raise ValueError("unknown section")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Home AI Control Panel</title><style>
:root{color-scheme:light dark;--b:#8884;--a:#2b7cff}body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:1080px;margin:20px auto;padding:0 16px}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:24px 0 8px}small,.m{opacity:.72}
nav{display:flex;gap:4px;margin:14px 0 6px;border-bottom:1px solid var(--b);flex-wrap:wrap}nav button{background:none;color:inherit;border:0;border-bottom:3px solid transparent;border-radius:0;padding:8px 14px}
nav button.on{border-color:var(--a);font-weight:600}.tab{display:none}.tab.on{display:block}
input,select,button{font:inherit;padding:6px 10px;border:1px solid var(--b);border-radius:8px;background:transparent;color:inherit}
button{background:var(--a);color:#fff;border:0;cursor:pointer}button.s{background:transparent;color:inherit;border:1px solid var(--b)}button.r{background:#c33}
table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid var(--b);text-align:left;font-size:14px;vertical-align:top}
.tag{font-size:12px;padding:2px 8px;border-radius:9px;background:#8882;white-space:nowrap}.ok{background:#2a73}.mid{background:#d903}.bad{background:#e333}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}.out{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px}
.bar{height:6px;background:#8883;border-radius:3px;overflow:hidden;width:140px;display:inline-block;vertical-align:middle}.bar i{display:block;height:100%;background:var(--a)}
.seg{white-space:nowrap}.seg button{border-radius:0;background:transparent;color:inherit;border:1px solid var(--b);margin-left:-1px}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0}
.seg button.on{background:var(--a);color:#fff}.card{border:1px solid var(--b);border-radius:12px;padding:4px 14px 12px;margin:10px 0}
#toast{position:fixed;bottom:16px;right:16px;max-width:440px;background:#222;color:#fff;padding:10px 14px;border-radius:10px;display:none;white-space:pre-wrap}
.tab{overflow-x:auto}#app{display:none}#gate{max-width:380px;margin:60px auto;display:none}#gate input{width:100%;box-sizing:border-box;margin:6px 0}
#top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}@media(max-width:640px){body{margin:10px auto}nav button{padding:8px 9px}}
.badge{background:#e33;color:#fff;border-radius:9px;padding:0 7px;font-size:12px;margin-left:4px}code{font-size:12px;word-break:break-all}
</style></head><body>
<div id="gate"><h1>Home AI Control Panel</h1><p id="gmsg" class="m"></p>
<input id="gcode" placeholder="Setup code (shown at the end of the installer)" style="display:none" autocomplete="off">
<input id="gpw" type="password" placeholder="Admin password" autocomplete="current-password"><input id="gpw2" type="password" placeholder="Type it again" style="display:none" autocomplete="new-password">
<button id="gbtn" style="width:100%">Log in</button><p id="gerr" style="color:#e44"></p></div>
<div id="app">
<div id="top"><div><h1>Home AI Control Panel</h1><div class="m">Models, GPUs, health and settings of your home AI server.</div></div><button class="s" id="lo">Log out</button></div>
<nav><button data-t="models" class="on">Models</button><button data-t="gpus">GPUs</button><button data-t="drives">Health</button><button data-t="storage">Storage</button><button data-t="addpc">Add a PC</button><button data-t="set">Settings</button></nav>

<section id="models" class="tab on">
<h2>Suggested new models <small id="sugt"></small></h2><div class="m">Checked every week on Hugging Face. Only models that fit your GPUs. Nothing downloads until you press Add.</div>
<div id="sug"></div><div class="row"><button class="s" id="scoutb">Check now</button></div>
<h2>Search Hugging Face</h2><div class="row"><input id="q" placeholder="e.g. Qwen3.5 9B, gemma 4, coder" size="40"><button id="qb">Search</button></div>
<div id="res"></div><div id="files"></div>
<h2>Downloads</h2><div id="prog" class="m">none</div>
<h2>Installed models</h2><div id="inst"></div>
</section>

<section id="gpus" class="tab">
<h2>Your GPUs</h2>
<div class="m"><b>Use</b> = every model may use it. <b>Standby</b> = kept ready, only used by big models that need all GPUs (or when nothing else is free).
<b>Off</b> = never used (e.g. while you game on that PC). Changing a setting stops the running models; they restart with the new choice on the next message.
Image generation (ComfyUI) is separate and isn't affected.</div>
<div class="row m">Auto-off: a GPU PC switches to <b>Off</b> while its own programs (a game, a video...) use its GPU more than
<input id="gap" type="number" min="0" max="100" style="width:4.5em"> % and goes back after <input id="gab" type="number" min="1" max="120" style="width:4.5em"> min
below that (0 % = never). <button class="s" id="gas">Save</button></div>
<div id="gl">Loading...</div>
<h2>Running now</h2><div id="run"></div><div class="row"><button class="s" id="unl">Stop all running models (free all VRAM)</button></div>
</section>

<section id="drives" class="tab">
<style>.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}.grid .card{margin:0}.big{font-size:26px;font-weight:600}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.up{background:#2a7}.down{background:#e33}</style>
<h2>Server status <small id="stime"></small> <span id="sv"></span></h2>
<div id="sp" style="margin-bottom:8px"></div>
<div class="grid" id="sg"></div>
<h2>Services</h2><div id="ss"></div>
<h2>Drive health (SMART) <small id="dtime"></small></h2>
<div class="m">Live health data straight from each disk. <b>Good</b> = no warning signs. <b>Warning</b> = early signs of wear (keep backups current, plan a replacement).
<b>Failing</b> = copy important data off now. The server also checks disks and services every 10 minutes and sends a Telegram alert if something new goes wrong.</div>
<div class="row"><button class="s" id="drf">Refresh drives now</button><label class="m"><input type="checkbox" id="dlive" checked> live (every 0.5 s; SMART is re-read from the disks every 5 s)</label></div>
<div id="dl">Loading...</div>
</section>

<section id="storage" class="tab">
<h2>Where models and backups are stored</h2>
<div id="stcur" class="m">Loading...</div>
<h2>Drives</h2>
<div class="m">Tick the drive(s) to use. The system disk, drives that are in use and drives already in a RAID cannot be ticked.</div>
<div id="stdrv">Loading...</div><div id="straid"></div>
<h2>Use the ticked drive(s) <small style="color:#e44">erases them</small></h2>
<div class="row"><label>How: <select id="stmode"><option value="drive">One drive (no RAID)</option><option value="1">RAID 1 - mirror, safe (2+ drives)</option>
<option value="5">RAID 5 - safe, more space (3+ drives)</option><option value="10">RAID 10 - fast and safe (4+ drives)</option><option value="0">RAID 0 - fastest, NOT safe (2+ drives)</option></select></label>
<label class="m"><input type="checkbox" id="stbk" checked> also keep the nightly backups there</label></div>
<div id="stplan" class="m"></div>
<div class="row"><button class="s" id="stprev">Show what will happen (changes nothing)</button></div>
<div class="card"><b style="color:#e44">Everything on the ticked drives will be erased.</b> Models already downloaded are moved there automatically.
<div class="row"><input id="sterase" placeholder="type ERASE" size="12" autocomplete="off"><input id="stpw" type="password" placeholder="your admin password" size="22" autocomplete="current-password">
<button id="strun" style="background:#c33;border-color:#c33;color:#fff">Erase and set up</button></div></div>
<h2>Or use a folder <small>erases nothing</small></h2>
<div class="m">A folder on a drive that is already set up, e.g. <b>/mnt/data/ai-models</b>. To go back to the system disk use <b>/opt/ai/models</b> (backups: <b>/opt/ai/backups</b>).</div>
<div class="row"><input id="stfolder" placeholder="/mnt/data/ai-models" size="34"><button id="stfgo">Move the models there</button><button class="s" id="stbgo">Move the backups there</button><button class="s" id="stfprev">Preview</button></div>
<label class="m"><input type="checkbox" id="stacc"> use it even if it is a Windows-format, network or temporary drive (slower or may be missing after a restart)</label>
<pre id="stpo" class="out m" style="display:none"></pre>
<h2>Progress <small id="stjt"></small></h2>
<pre id="stlog" class="out m">No storage job yet.</pre>
<div class="m"><b>RAID 1/5/10</b> keep working when one drive dies. <b>RAID 0</b> loses everything if any drive dies (fine for models - they can be downloaded again - not for backups).
RAID is not a backup. Same tool on the server: <b>sudo ai-storage</b>. Details: docs/storage.md</div>
</section>

<section id="addpc" class="tab">
<h2>Add another PC's graphics card</h2>
<div class="m">Any PC at home with a graphics card can lend its GPU to the AI server. Its GPU memory is added to the pool, so bigger models fit and run faster.
Its files stay on that PC; only the GPU is used, and only the AI server may connect to it.</div>
<div class="card"><h2>Before you start</h2><div class="m">
&bull; The PC must be on the home network (a cable is much faster than Wi-Fi) and switched on when you want to use it.<br>
&bull; Graphics card: NVIDIA works best (install the latest NVIDIA driver first). AMD and Intel work too (slower). 6 GB+ of GPU memory is recommended.<br>
&bull; About 1 GB of free disk space.</div></div>
<div class="card"><h2>Windows PC</h2><div class="m">On the PC you want to add:</div>
<ol><li>Open this link in its browser and save the file:<br><a id="winlink" href="#" download>add-this-pc.bat</a> <button class="s" onclick="cp('winurl')">Copy link</button> <input id="winurl" readonly size="40"></li>
<li>Double-click <b>add-this-pc.bat</b> and click <b>Yes</b> when Windows asks for permission.</li>
<li>Wait until it shows <b>WORKER-OK</b> (the first time it downloads ~300 MB).</li>
<li>Come back to this tab - the new GPU appears in the list below within a minute.</li></ol>
<div class="m">To remove it later: on that PC open Task Scheduler and delete the tasks <b>AI GPU Worker</b> and <b>AI GPU Status</b> (or just set it to <b>Off</b> in the GPUs tab).</div></div>
<div class="card"><h2>Ubuntu / Linux PC</h2><div class="m">On the PC you want to add, open a Terminal and paste this command (it asks for that PC's password):</div>
<div class="row"><input id="linuxcmd" readonly size="62" style="font-family:ui-monospace,monospace"><button class="s" onclick="cp('linuxcmd')">Copy</button></div>
<div class="m">If it says an NVIDIA driver was installed, restart that PC and run the same command again.
To remove it later: <code>sudo systemctl disable --now ai-gpu-worker ai-gpu-status</code></div></div>
<div class="card"><h2>GPU PCs the server can see</h2><div id="wl" class="m">Loading...</div>
<div class="row"><button class="s" id="rsc">Look for new PCs now</button></div>
<div class="m">Found it? Go to the <b>GPUs</b> tab to give it a name and choose <b>Use</b>, <b>Standby</b> or <b>Off</b>. Models pick it up the next time they start
(or press "Stop all running models" there).</div></div>
<div class="card"><h2>PC on a different network?</h2><div class="m">PCs on this home network are found automatically. Only if a PC is on another subnet (e.g. 192.168.1.x), type its address here, one per line (ip or ip:port):</div>
<textarea id="wx" rows="3" cols="30" style="font:inherit;padding:6px;border:1px solid var(--b);border-radius:8px;background:transparent;color:inherit"></textarea>
<div class="row"><button id="wxs">Save</button></div></div>
</section>

<section id="set" class="tab">
<div class="card"><h2>Telegram alerts (optional)</h2>
<div class="m">Get a message on your phone when a disk shows problems, a service stops, or better models are found.
In Telegram: open <b>@BotFather</b> &rarr; /newbot &rarr; copy the token. Open <b>@userinfobot</b> to get your user ID (a number). Open your new bot and press <b>Start</b>.</div>
<div class="row"><input id="tgt" type="password" placeholder="bot token" size="46"><input id="tgc" placeholder="your user ID" size="14"><button id="tgs">Save + send test</button><span id="tgst" class="m"></span></div></div>
<div class="card"><h2>Use the AI from other devices</h2><div class="m">
Everyone at home can chat in <b>Open WebUI</b>: <code class="wurl"></code> (phones/tablets too - add it to the home screen).
Family members get their own accounts there (Open WebUI &rarr; Admin Panel &rarr; Users). This control panel works from any home device after logging in.<br><br>
<b>Direct AI access</b> (VS Code, Chrome AI extensions, scripts) is only allowed for the devices listed here - one IP per line (plus ADMIN_PC_IP from the installer, if set).</div>
<textarea id="apid" rows="4" cols="30" style="font:inherit;padding:6px;border:1px solid var(--b);border-radius:8px;background:transparent;color:inherit"></textarea>
<div class="row"><button id="apis">Save allowed devices</button></div></div>
<div class="card"><h2>Admin password and logins</h2>
<div class="row"><input id="opw" type="password" placeholder="current password" size="18"><input id="npw" type="password" placeholder="new password (10+ characters)" size="24"><button id="cpw">Change password</button></div>
<div id="sl" class="m"></div><div class="row"><button class="s" id="loa">Log out all devices</button></div></div>
<h2>Output</h2><div id="out" class="out m"></div>
</section>
</div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* fast live refresh: redraw only when something changed, never overlap requests, pause while clicking */
(()=>{const ih=Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML'),tc=Object.getOwnPropertyDescriptor(Node.prototype,'textContent');
 Object.defineProperty(Element.prototype,'innerHTML',{get(){return ih.get.call(this)},set(v){if(this._lastH===v)return;this._lastH=v;ih.set.call(this,v)},configurable:true});
 Object.defineProperty(Node.prototype,'textContent',{get(){return tc.get.call(this)},set(v){this._lastH=undefined;tc.set.call(this,v)},configurable:true});})();
let PDOWN=0;document.addEventListener('pointerdown',()=>PDOWN=Date.now(),true);document.addEventListener('pointerup',()=>PDOWN=Date.now(),true);
const BUSY={};function tick(key,fn){if(BUSY[key]||Date.now()-PDOWN<700)return;BUSY[key]=1;Promise.resolve().then(fn).catch(()=>{}).finally(()=>BUSY[key]=0)}
const REFRESH_MS=500;
const api=(p,b)=>fetch('api/'+p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Panel':'1'},body:JSON.stringify(b)}).then(r=>{if(r.status==401&&!['login','setup'].includes(p))gate();return r.json()});
function toast(t){const e=$('#toast');e.textContent=t;e.style.display='block';clearTimeout(e._t);e._t=setTimeout(()=>e.style.display='none',7000)}
async function act(p,b,after){const r=await api(p,b);const t=r.error?('Error: '+r.error):(r.output||'Done');toast(t);$('#out').textContent=t;if(after)after();return r}
let TAB='models';document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{TAB=b.dataset.t;document.querySelectorAll('nav button,.tab').forEach(x=>x.classList.remove('on'));b.classList.add('on');$('#'+TAB).classList.add('on');load()});
/* ---------- models ---------- */
let INFO={solo_gb:4.5,pool_gb:13};
function fit(gb){if(gb<=INFO.solo_gb)return['<span class="tag ok">fits: server GPU</span>','solo'];if(gb<=INFO.pool_gb)return['<span class="tag ok">fits: all GPUs</span>','pool'];
 if(gb<=INFO.pool_gb*1.6)return['<span class="tag mid">all GPUs + RAM (slow)</span>','pool'];return['<span class="tag bad">too big</span>','pool']}
let SR=[],FL=[],REPO='',SG=[],INST=[];
function nameOf(repo){return repo.split('/')[1].replace(/-?gguf$/i,'').replace(/^(google|qwen|openai|meta|mistralai)_/i,'').toLowerCase().replace(/[^a-z0-9._-]/g,'-').slice(0,40)}
$('#qb').onclick=search;$('#q').onkeydown=e=>{if(e.key=='Enter')search()};
async function search(){const q=$('#q').value.trim();if(!q)return;$('#res').textContent='Searching...';$('#files').innerHTML='';
 const r=await api('search?q='+encodeURIComponent(q));if(r.error){$('#res').textContent=r.error;return}SR=r;
 $('#res').innerHTML='<table><tr><th>Repository</th><th>Downloads</th><th></th></tr>'+r.map((x,i)=>`<tr><td>${esc(x.id)}</td><td>${esc(x.downloads)}</td><td><button class="s" onclick="files(${i})">Choose size</button></td></tr>`).join('')+'</table>'}
async function files(i){REPO=SR[i].id;$('#files').textContent='Loading files...';const r=await api('files?repo='+encodeURIComponent(REPO));if(r.error){$('#files').textContent=r.error;return}FL=r.files;
 $('#files').innerHTML=`<h2>Pick a size - ${esc(REPO)}</h2><div class="m">Vision: ${r.vision?'yes':'no'}. Smaller = faster, a bit less smart. Q4_K_M is a good default.</div>
 <div class="row">Name in the menu: <input id="nm" value="${esc(nameOf(REPO))}" size="24"></div><table><tr><th>File</th><th>Size</th><th>Fits</th><th></th></tr>`+
 FL.map((f,j)=>{const[t,mode]=fit(f.gb);return `<tr><td>${esc(f.path)}</td><td>${f.gb} GB</td><td>${t}</td><td><select id="m_${j}"><option ${mode=='pool'?'selected':''}>pool</option><option ${mode=='solo'?'selected':''}>solo</option></select>
 <button onclick="addf(${j})">Add</button></td></tr>`}).join('')+'</table>'}
function addf(j){act('add',{name:$('#nm').value.trim().toLowerCase(),repo:REPO,quant:FL[j].path,mode:$('#m_'+j).value},load)}
function addsug(i){const s=SG[i];act('add',{name:nameOf(s.repo),repo:s.repo,quant:s.file,mode:s.mode},load)}
function ren(i){const n=INST[i].name;const v=prompt('New name for '+n+' (letters, numbers, - . _):',n);if(v&&v.trim()&&v.trim()!=n)act('rename',{name:n,new:v.trim()},load)}
function del(i){const n=INST[i].name;if(confirm('Remove '+n+'? Its file will be deleted.'))act('remove',{name:n},load)}
$('#scoutb').onclick=()=>act('scout',{});
async function loadModels(){
 const s=await api('suggestions');SG=s.items||[];$('#sugt').textContent=s.checked?'(checked '+s.checked+')':'';
 $('#sug').innerHTML=SG.length?'<table><tr><th>Model</th><th>Size</th><th>Runs on</th><th>Vision</th><th>Updated</th><th></th></tr>'+SG.map((x,i)=>`<tr><td>${esc(x.repo)}<br><small>${esc(x.file)}</small></td><td>${esc(x.gb)} GB</td><td>${esc(x.fits)}</td><td>${x.vision?'yes':''}</td><td>${esc(x.updated)}</td><td><button onclick="addsug(${i})">Add</button></td></tr>`).join('')+'</table>':'<div class="m">No suggestions yet.</div>';
 INST=await api('installed');$('#inst').innerHTML='<table><tr><th>Name</th><th>Runs on</th><th>Size</th><th></th></tr>'+INST.map((x,i)=>`<tr><td>${esc(x.name)}</td><td>${esc({solo:'server GPU',pool:'all GPUs',remote:'worker GPUs'}[x.mode]||x.mode)}</td><td>${esc(x.gb)} GB</td><td><button class="s" onclick="ren(${i})">Rename</button> <button class="s" onclick="del(${i})">Remove</button></td></tr>`).join('')+'</table>';
 const p=await api('progress');$('#prog').innerHTML=p.length?p.map(x=>`<div>${esc(x.name)}: ${x.done?'<span class="tag ok">ready - refresh Open WebUI</span>':x.failed?'<span class="tag bad">failed</span> <small>'+esc(x.last)+'</small>':'<span class="bar"><i style="width:'+(+x.pct||0)+'%"></i></span> '+(+x.pct||0)+'%'}</div>`).join(''):'none'}
/* ---------- GPUs ---------- */
let GP=[];
async function loadGpus(){const r=await api('gpus');if(r.error){$('#gl').textContent=r.error;return}GP=r.gpus;
 $('#gl').innerHTML='<table><tr><th>GPU</th><th>Where</th><th>Memory used</th><th>Status</th><th>Mode</th></tr>'+GP.map((g,i)=>{
  const mem=g.total?`<span class="bar"><i style="width:${Math.round(100*g.used/g.total)}%"></i></span> ${(g.used/1024).toFixed(1)} / ${(g.total/1024).toFixed(1)} GB`:'';
  const st=!g.online?'<span class="tag bad">offline</span>':g.busy?'<span class="tag ok">in use by a model</span>':g.slow?'<span class="tag mid">online, slow to answer</span>':'<span class="tag ok">online</span>';
  return `<tr><td><input id="gn_${i}" value="${esc(g.name)}" size="20" title="${esc(g.model)}"> <button class="s" onclick="gname(${i})" title="Save name">&#9998; Save</button>${g.model&&g.model!=g.name?'<br><small>'+esc(g.model)+'</small>':''}</td><td>${esc(g.where)}</td><td>${mem}</td><td>${st}${!g.online&&g.key!='server'?' <button class="s" onclick="gforget('+i+')">Forget</button>':''}${g.auto_off?'<br><span class="tag mid">off automatically: the PC is using it</span>':''}${g.key=='server'?'':g.reporter===false?'<br><small class="m">no GPU-use report from this PC</small>':g.pc_pct!=null?'<br><small class="m">PC\'s own use: '+esc(g.pc_pct)+'%'+(g.pc_top?' ('+esc(g.pc_top)+')':'')+'</small>':''}</td><td class="seg">${['use','standby','off'].map(m=>`<button class="${g.mode==m?'on':''}" onclick="setg(${i},'${m}')">${m[0].toUpperCase()+m.slice(1)}</button>`).join('')}</td></tr>`}).join('')+'</table>';
 if(r.guard&&![$('#gap'),$('#gab')].includes(document.activeElement)){$('#gap').value=r.guard.pct;$('#gab').value=r.guard.back}
 $('#run').innerHTML=r.running.length?r.running.map(x=>`<span class="tag ok">${esc(x.model)} - ${esc(x.state)}</span> `).join(''):'<span class="m">No model loaded right now.</span>'}
function gforget(i){if(confirm('Forget '+GP[i].name+' ('+GP[i].where+')? Its name and settings are removed. Use this for a PC that is gone or got a new address.'))act('gpu_forget',{key:GP[i].key},loadGpus)}
function gname(i){act('gpu_name',{key:GP[i].key,name:$('#gn_'+i).value},loadGpus)}
function setg(i,m){act('gpu_policy',{key:GP[i].key,value:m},()=>setTimeout(loadGpus,1500))}
$('#gas').onclick=()=>act('gpu_guard',{pct:$('#gap').value,back:$('#gab').value},loadGpus);
$('#unl').onclick=()=>act('unload',{},()=>setTimeout(loadGpus,1500));
async function badge(){}
/* ---------- settings ---------- */
async function loadSet(){const s=await api('settings');if(s.error){$('#out').textContent=s.error;return}
 document.querySelectorAll('.wurl').forEach(e=>e.textContent=`http://${location.hostname}:3000`);
 if(document.activeElement!==$('#tgc'))$('#tgc').value=s.telegram.chat_id;$('#tgst').textContent=s.telegram.set?'connected':'not set';
 if(document.activeElement!==$('#apid'))$('#apid').value=(s.api_devices||[]).join('\n');
 $('#sl').innerHTML='Logged-in devices: '+(s.sessions||[]).map(x=>`<br><small>${esc(x.ip)} - ${esc(x.created)} - ${esc((x.ua||'').slice(0,60))}</small>`).join('')}
$('#tgs').onclick=()=>act('settings',{section:'telegram',token:$('#tgt').value,chat_id:$('#tgc').value},()=>{$('#tgt').value='';loadSet()});
/* ---------- drives ---------- */
let DR=[];
function hrs(h){if(h==null)return'';const y=h/8766;return y>=1?`${h.toLocaleString()} h (${y.toFixed(1)} years)`:`${h.toLocaleString()} h (${Math.round(h/24)} days)`}
async function loadDrives(fresh){const r=await api('drives'+(fresh?'?fresh=1':''));if(r.error){$('#dl').textContent=r.error;return}DR=r.disks||[];
 $('#dtime').textContent='(read '+(r.time||'')+')';
 $('#dl').innerHTML=DR.map((d,i)=>{const hb={good:'<span class="tag ok">Good</span>',warning:'<span class="tag mid">Warning</span>',bad:'<span class="tag bad">Failing</span>'}[d.health]||'<span class="tag">Unknown</span>';
  const t=d.temp!=null?`<span class="tag ${d.temp>=55?'bad':d.temp>=48?'mid':'ok'}">${esc(d.temp)} &deg;C</span>`:'';
  const use=(d.usage||[]).map(u=>`<div>${esc(u.mount)}: <span class="bar"><i style="width:${+u.pct}%"></i></span> ${esc(u.used_gb)} / ${esc(u.total_gb)} GB (${+u.pct}%)</div>`).join('');
  const key=(d.attrs||[]).filter(a=>a.label);const all=d.attrs||[];
  const lt=d.last_test?`Last self-test: ${esc(d.last_test.type)} - ${esc(d.last_test.result)}`:'No self-test yet';
  return `<div class="card"><h2>${esc(d.name)} &middot; ${esc(d.model)} &middot; ${esc(d.size)} ${esc(d.kind)} ${hb} ${t}</h2>
  <div class="m">Serial ${esc(d.serial)}${d.rpm?' &middot; '+esc(d.rpm)+' rpm':''} &middot; Powered on ${hrs(d.hours)}${d.cycles!=null?' &middot; '+esc(d.cycles)+' power cycles':''}</div>
  ${d.problems&&d.problems.length?'<div style="margin:6px 0">'+d.problems.map(p=>'<span class="tag '+(d.health=='bad'?'bad':'mid')+'">'+esc(p)+'</span>').join(' ')+'</div>':''}
  ${use}
  <table style="margin-top:6px"><tr><th>Check</th><th>Value</th><th>Normalised (worst / limit)</th></tr>${key.map(a=>`<tr><td>${esc(a.label)}</td><td>${esc(a.raw_str||a.raw)}</td><td>${a.value!=null?esc(a.value)+(a.worst!=null?' ('+esc(a.worst)+' / '+esc(a.thresh)+')':''):''}</td></tr>`).join('')}</table>
  <details><summary class="m">All ${all.length} SMART values</summary><table>${all.map(a=>`<tr><td>${esc(a.id)}</td><td>${esc(a.name)}</td><td>${esc(a.raw_str||a.raw)}</td><td>${esc(a.value)} / ${esc(a.worst)} / ${esc(a.thresh)}</td><td>${a.failing?'<span class="tag bad">failing</span>':''}</td></tr>`).join('')}</table></details>
  <div class="row"><span class="m">${lt}${d.test_running?' &middot; <b>test running: '+esc(d.test_running)+'</b>':''}</span><button class="s" onclick="dtest(${i})">Run quick self-test (2 min)</button></div></div>`}).join('')||'No disks found.'}
function bar(p){p=Math.max(0,Math.min(100,+p||0));return `<span class="bar"><i style="width:${p}%;background:${p>=90?'#e33':p>=75?'#d90':'var(--a)'}"></i></span>`}
function dur(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+h+'h '+m+'m'}
const GB=b=>(b/1073741824).toFixed(1);
async function loadStatus(){const r=await api('status');if(r.error)return;
 $('#stime').textContent='('+r.time+')';
 $('#sv').innerHTML=r.problems.length?'<span class="tag bad">'+r.problems.length+' problem(s)</span>':'<span class="tag ok">All good</span>';
 $('#sp').innerHTML=r.problems.map(p=>'<span class="tag bad">'+esc(p)+'</span>').join(' ');
 const c=r.cpu,m=r.mem,n=r.net;
 let cards=`<div class="card"><div class="m">CPU &middot; ${esc(c.cores)} threads</div><div class="big">${c.pct==null?'...':esc(c.pct)+'%'}</div>${bar(c.pct)}<div class="m">load ${esc(c.load.join(' / '))}<br><small>${esc(c.model)}</small></div></div>
 <div class="card"><div class="m">Memory (RAM)</div><div class="big">${esc(m.pct)}%</div>${bar(m.pct)}<div class="m">${GB(m.total-m.avail)} / ${GB(m.total)} GB used${m.swap_total?'<br>swap '+GB(m.swap_used)+' / '+GB(m.swap_total)+' GB':''}</div></div>`;
 for(const g of r.gpu){const vp=g.mem_total?Math.round(100*g.mem_used/g.mem_total):0;
  cards+=`<div class="card"><div class="m">GPU &middot; ${esc(g.name)}</div><div class="big">${esc(g.util??'-')}%</div>${bar(g.util)}<div class="m">VRAM ${(g.mem_used/1024).toFixed(1)} / ${(g.mem_total/1024).toFixed(1)} GB ${bar(vp)}<br>${g.temp!=null?'<span class="tag '+(g.temp>=85?'bad':g.temp>=75?'mid':'ok')+'">'+esc(g.temp)+' &deg;C</span>':''} ${g.power!=null?esc(g.power)+' W':''} ${g.fan!=null?'fan '+esc(g.fan)+'%':''}</div></div>`}
 cards+=`<div class="card"><div class="m">Temperatures</div>${r.temps.map(t=>`<div>${esc(t.name)}: <span class="tag ${t.c>=88?'bad':t.c>=75?'mid':'ok'}">${esc(t.c)} &deg;C</span></div>`).join('')||'<div class="m">no sensors found</div>'}</div>
 <div class="card"><div class="m">Network &middot; ${esc(n.iface)}${n.speed>0?' &middot; '+(n.speed>=1000?(n.speed/1000)+' Gbit/s':n.speed+' Mbit/s'):''}</div><div>&darr; ${n.rx_mbs==null?'...':esc(n.rx_mbs)+' MB/s'}</div><div>&uarr; ${n.tx_mbs==null?'...':esc(n.tx_mbs)+' MB/s'}</div></div>
 <div class="card"><div class="m">Storage</div>${r.disks.map(d=>`<div>${esc(d.mount)} ${bar(d.pct)} ${esc(d.used_gb)} / ${esc(d.total_gb)} GB</div>`).join('')}
  ${(r.raids||[]).map(a=>`<div>RAID ${esc(a.label||a.name)} &middot; ${esc(a.level)} &middot; ${a.members.length} drives <span class="tag ${a.degraded?'bad':'ok'}">${a.degraded?'PROBLEM':'healthy'}</span>${a.sync?`<br><small>${esc(a.sync.what)} ${esc(a.sync.pct)}% &middot; about ${esc(a.sync.eta_min)} min left</small>`:''}</div>`).join('')}
  <div class="m"><small>Models: ${esc(r.models_dir)}<br>Backups: ${esc(r.backup_dir)}<br>Change drive / RAID: <b>Storage</b> tab</small></div></div>
 <div class="card"><div class="m">Up time</div><div class="big">${dur(r.uptime_s)}</div><div class="m">${esc(r.host)} &middot; kernel ${esc(r.kernel)}<br>Last backup: ${esc(r.backup.last||'not yet')} ${r.backup.result&&r.backup.result!='success'&&r.backup.last?'<span class="tag bad">'+esc(r.backup.result)+'</span>':''}</div></div>
 <div class="card"><div class="m">Models loaded now</div>${r.running.length?r.running.map(x=>'<div><span class="tag ok">'+esc(x.model)+'</span> '+esc(x.state)+'</div>').join(''):'<div class="m">none (they start on the first message)</div>'}</div>`;
 $('#sg').innerHTML=cards;
 $('#ss').innerHTML='<table><tr><th>Service</th><th>State</th></tr>'+r.services.map(x=>`<tr><td>${esc(x.label)}</td><td><span class="dot ${x.state=='active'?'up':'down'}"></span>${esc(x.state)}</td></tr>`).join('')+
  r.endpoints.map(e=>`<tr><td>${esc(e.label)} (answering?)</td><td><span class="dot ${e.ok?'up':'down'}"></span>${e.ok?'yes, '+e.ms+' ms':'NO'}</td></tr>`).join('')+'</table>'}
function dtest(i){act('drive_test',{name:DR[i].name},()=>setTimeout(()=>loadDrives(true),3000))}
$('#drf').onclick=()=>loadDrives(true);
setInterval(()=>{if(TAB!='drives'||!$('#dlive').checked||$('#app').style.display!='block')return;tick('status',loadStatus);
 if(!document.querySelector('#drives details[open]'))tick('drives',()=>loadDrives(false))},REFRESH_MS);
/* ---------- storage ---------- */
let ST={disks:[]};const STSEL=new Set();
const RAIDINFO={drive:[1,'One drive: space = the whole drive. If it dies the models must be downloaded again.'],
 '0':[2,'RAID 0: fastest and all space usable, but if ANY drive dies everything on it is lost.'],
 '1':[2,'RAID 1: every drive holds the same copy. Keeps working if a drive dies. Space = one drive.'],
 '5':[3,'RAID 5: keeps working if one drive dies. Space = all drives minus one.'],
 '10':[4,'RAID 10: fast, keeps working if a drive dies. Space = half of all drives.']};
function hb(b){const u=['B','KB','MB','GB','TB','PB'];let i=0;while(b>=1000&&i<5){b/=1000;i++}return b.toFixed(i>2?1:0)+' '+u[i]}
function raidRow(a){return `<div>RAID ${esc(a.label||a.name)} &middot; ${esc(a.level)} &middot; ${esc(a.members.join(', '))} <span class="tag ${a.degraded?'bad':'ok'}">${a.degraded?'PROBLEM':'healthy'}</span>${a.sync?` ${esc(a.sync.what)} ${esc(a.sync.pct)}% (about ${esc(a.sync.eta_min)} min left)`:''}${a.problem?`<br><small>${esc(a.problem)}</small>`:''}</div>`}
async function loadStorage(fresh){const r=await api('storage'+(fresh?'?fresh=1':''));const j=r.job||{};
 if(r.error){$('#stcur').textContent=r.error}else{ST=r;
  $('#stcur').innerHTML=`<div><b>Models:</b> ${esc(r.models_dir)} <span class="m">${esc(r.models_where)}</span></div><div><b>Backups:</b> ${esc(r.backup_dir)} <span class="m">${esc(r.backup_where)}</span></div>`;
  for(const d of [...STSEL])if(!r.disks.some(x=>x.dev==d&&x.ok))STSEL.delete(d);
  $('#stdrv').innerHTML='<table><tr><th></th><th>Drive</th><th>Size</th><th>Type</th><th>Model</th><th>State</th></tr>'+r.disks.map((d,i)=>`<tr><td>${d.ok?`<input type="checkbox" onchange="stsel(${i},this.checked)" ${STSEL.has(d.dev)?'checked':''}>`:''}</td><td>${esc(d.dev)}</td><td>${esc(d.size)}</td><td>${esc(d.kind)}</td><td>${esc(d.model)}</td><td><span class="tag ${d.ok?(d.state=='empty'?'ok':'mid'):'bad'}">${d.ok?'can be used - ':''}${esc(d.state_txt)}</span></td></tr>`).join('')+'</table>';
  $('#straid').innerHTML=(r.raids||[]).length?'<h2>RAID status</h2>'+r.raids.map(raidRow).join(''):''}
 stplan();
 $('#stjt').innerHTML={running:'<span class="tag mid">running...</span>',ok:'<span class="tag ok">finished</span>',failed:'<span class="tag bad">failed - see the messages</span>',stopped:'<span class="tag bad">stopped</span>'}[j.result]||'';
 $('#stlog').innerHTML=esc(j.log||'No storage job yet.')+(j.time?esc('\n\n(last update '+j.time+')'):'');
 ['#strun','#stfgo','#stbgo','#stprev','#stfprev'].forEach(x=>$(x).disabled=!!j.running)}
function stsel(i,on){const d=ST.disks[i].dev;if(on)STSEL.add(d);else STSEL.delete(d);stplan()}
function stplan(){const m=$('#stmode').value,[min,txt]=RAIDINFO[m],sel=(ST.disks||[]).filter(d=>STSEL.has(d.dev)),n=sel.length;let t=esc(txt);
 if(!n)t+='<br><b>Tick the drive(s) above.</b>';
 else if(m=='drive'&&n!=1)t+='<br><b>Tick exactly one drive - or choose a RAID to combine several.</b>';
 else if(n<min)t+=`<br><b>RAID ${m} needs at least ${min} drives (${n} ticked).</b>`;
 else{const s=Math.min(...sel.map(d=>d.bytes)),cap=m=='drive'?sel[0].bytes:m=='0'?s*n:m=='1'?s:m=='5'?s*(n-1):s*n/2;
  t+=`<br>Result: about <b>${hb(cap)}</b> usable, mounted at /mnt/ai-storage. <span style="color:#e44">Will be ERASED: <b>${sel.map(d=>esc(d.dev+' ('+d.size+(d.model?' '+d.model:'')+')')).join(', ')}</b></span>`;
  if(m!='drive'&&new Set(sel.map(d=>d.bytes)).size>1)t+='<br>The drives have different sizes: each is only used up to the size of the smallest one.';
  if(m!='drive'&&new Set(sel.map(d=>d.kind)).size>1)t+='<br>Mixing HDD and SSD works, but the RAID runs at the speed of the slowest drive.'}
 $('#stplan').innerHTML=t}
function stkind(){return $('#stmode').value=='drive'?'drive':'raid'}
function stbody(kind,extra){return Object.assign({kind,disks:[...STSEL],level:$('#stmode').value,backups:$('#stbk').checked,path:$('#stfolder').value.trim(),accept:$('#stacc').checked},extra||{})}
async function stprev(kind){$('#stpo').style.display='block';$('#stpo').innerHTML='Working out what would happen...';const r=await api('storage_preview',stbody(kind));$('#stpo').innerHTML=esc(r.error?'Error: '+r.error:r.output)}
$('#stmode').onchange=()=>{$('#stbk').checked=$('#stmode').value!='0';stplan()};
$('#stprev').onclick=()=>stprev(stkind());$('#stfprev').onclick=()=>stprev('folder');
$('#strun').onclick=async()=>{if(!STSEL.size){toast('Tick the drive(s) first');return}
 if(!confirm('Really ERASE '+[...STSEL].join(', ')+'? This cannot be undone.'))return;
 const r=await act('storage_run',stbody(stkind(),{confirm:$('#sterase').value.trim(),password:$('#stpw').value}));$('#stpw').value='';if(!r.error){$('#sterase').value='';STSEL.clear()}loadStorage(true)};
$('#stfgo').onclick=()=>act('storage_run',stbody('folder'),()=>loadStorage(true));
$('#stbgo').onclick=()=>act('storage_run',stbody('backups'),()=>loadStorage(true));
setInterval(()=>{if(TAB!='storage'||$('#app').style.display!='block')return;tick('storage',()=>loadStorage(false))},1000);
/* ---------- add a PC ---------- */
function cp(id){const e=$('#'+id);e.select();let ok=false;try{if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(e.value);ok=true}else ok=document.execCommand('copy')}catch(x){}toast(ok?'Copied':'Select the text and press Ctrl+C')}
async function loadAdd(){const host=location.hostname;const u=`http://${host}:8090/add-this-pc.bat`;$('#winlink').href=u;$('#winurl').value=u;
 $('#linuxcmd').value=`curl -fsSL http://${host}:8090/add-worker-linux.sh | bash`;
 const x=await api('workers_extra');if(document.activeElement!==$('#wx'))$('#wx').value=(x.items||[]).join('\n');
 const r=await api('gpus');const w=(r.gpus||[]).filter(g=>g.key!='server');
 $('#wl').innerHTML=w.length?'<table><tr><th>Name</th><th>Address</th><th>Status</th><th>Memory</th><th>Mode</th></tr>'+w.map(g=>`<tr><td>${esc(g.name)}</td><td>${esc(g.key)}</td><td>${!g.online?'<span class="tag bad">offline</span>':'<span class="tag ok">online</span>'}</td><td>${g.total?(g.total/1024).toFixed(1)+' GB':''}</td><td>${esc(g.mode)}</td></tr>`).join('')+'</table>':'No GPU PCs found yet.'}
$('#rsc').onclick=()=>act('rescan',{},()=>{$('#wl').textContent='Looking (takes a few seconds)...';loadAdd()});
$('#wxs').onclick=()=>act('workers_extra',{items:$('#wx').value.split(/[\s,]+/).filter(x=>x)},loadAdd);
/* ---------- login ---------- */
let SETUP=false;
async function gate(){const a=await api('auth');if(a.logged_in){$('#gate').style.display='none';$('#app').style.display='block';return true}
 $('#app').style.display='none';$('#gate').style.display='block';$('#gerr').textContent='';SETUP=a.setup_needed;
 if(a.setup_needed){$('#gmsg').textContent='First visit: create the admin password for this panel (10+ characters).'+(a.can_setup?'':' Enter the setup code printed at the end of the installer (also in /opt/ai/secrets/setup-code on the server).');
  $('#gcode').style.display=a.can_setup?'none':'block';$('#gpw2').style.display='block';$('#gbtn').textContent='Create password'}
 else{$('#gmsg').textContent='Log in with the admin password.';$('#gpw2').style.display='none';$('#gbtn').textContent='Log in'}
 $('#gpw').focus();return false}
async function gsubmit(){const pw=$('#gpw').value;if(SETUP&&pw!==$('#gpw2').value){$('#gerr').textContent='The two passwords are different.';return}
 const r=await api(SETUP?'setup':'login',{password:pw,code:$('#gcode').value.trim()});if(r.error){$('#gerr').textContent=r.error;return}$('#gpw').value='';$('#gpw2').value='';if(await gate()){try{INFO=await api('info')}catch(e){}load()}}
$('#gbtn').onclick=gsubmit;$('#gpw').onkeydown=$('#gpw2').onkeydown=e=>{if(e.key=='Enter')gsubmit()};
$('#lo').onclick=async()=>{await api('logout',{});gate()};
$('#loa').onclick=()=>{if(confirm('Log out every device, including this one?'))act('logout_all',{},gate)};
$('#cpw').onclick=()=>act('password',{old:$('#opw').value,new:$('#npw').value},()=>{$('#opw').value='';$('#npw').value='';loadSet()});
$('#apis').onclick=()=>act('api_devices',{ips:$('#apid').value.split(/[\s,]+/).filter(x=>x)},loadSet);
function load(){if($('#app').style.display!='block')return;return Promise.all([badge(),({models:loadModels,gpus:loadGpus,drives:()=>Promise.all([loadStatus(),loadDrives()]),storage:()=>loadStorage(false),addpc:loadAdd,set:loadSet})[TAB]()])}
gate().then(async ok=>{if(ok){try{INFO=await api('info')}catch(e){}load()}});setInterval(()=>{if($('#app').style.display!='block')return;const f=document.activeElement;
 if(TAB=='set'||TAB=='drives'||TAB=='storage'||(f&&(f.tagName=='INPUT'||f.tagName=='TEXTAREA'||f.tagName=='SELECT')))tick('badge',badge);else tick('load',load)},REFRESH_MS);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "aip": return v
        return ""

    def authed(self): return session_ok(self.token())

    def auth_api(self, p, b):
        """login / setup / logout. Returns (code, obj, extra_headers) or None."""
        ip = self.client_address[0]
        if p == "setup":
            if os.path.exists(PASSF): return 400, {"error": "The admin password already exists."}, []
            code_ok = False
            try: code_ok = hmac.compare_digest(open(SETUP_CODE).read().strip(), str(b.get("code", "")).strip().upper())
            except FileNotFoundError: pass
            if ip not in allowed_ips() and not code_ok:
                _fails.setdefault(ip, []).append(time.time())
                if too_many_fails(ip): return 429, {"error": "Too many wrong codes. Wait 10 minutes."}, []
                return 403, {"error": "Wrong setup code. It is printed at the end of the installer (and saved in /opt/ai/secrets/setup-code)."}, []
            pw = str(b.get("password", ""))
            if len(pw) < 10: return 400, {"error": "Use at least 10 characters."}, []
            write_file(PASSF, _hash_pw(pw))
            try: os.remove(SETUP_CODE)
            except OSError: pass
        elif p == "login":
            if too_many_fails(ip): return 429, {"error": "Too many wrong passwords. Wait 10 minutes."}, []
            if not _check_pw(str(b.get("password", ""))):
                _fails.setdefault(ip, []).append(time.time()); time.sleep(1)
                return 401, {"error": "Wrong password."}, []
        elif p == "logout":
            end_sessions(self.token())
            return 200, {"output": "Logged out."}, ["aip=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"]
        else:
            return None
        tok = new_session(ip, self.headers.get("User-Agent", ""))
        return 200, {"output": "ok"}, [f"aip={tok}; Max-Age={SESSION_DAYS*86400}; Path=/; HttpOnly; SameSite=Strict"]

    def send(self, code, obj, ctype="application/json", cookies=()):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        for c in cookies: self.send_header("Set-Cookie", c)
        self.send_header("X-Frame-Options", "DENY"); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); p = u.path.rstrip("/").split("/")[-1]
        try:
            if p in ("", "index.html") or u.path.endswith("/"): return self.send(200, PAGE, "text/html; charset=utf-8")
            if p == "health": return self.send(200, {"status": "ok"})
            if p == "auth":
                return self.send(200, {"setup_needed": not os.path.exists(PASSF), "logged_in": self.authed(),
                                       "can_setup": self.client_address[0] in allowed_ips()})
            if not self.authed(): return self.send(401, {"error": "Please log in."})
            if p == "info":
                c = conf(); return self.send(200, {"server_ip": c.get("SERVER_IP", ""), "solo_gb": float(c.get("SOLO_GB", 4.5)),
                                                   "pool_gb": float(c.get("POOL_GB", 13))})
            if p == "installed": return self.send(200, installed())
            if p == "progress": return self.send(200, progress())
            if p == "suggestions": return self.send(200, jload(SUG, {"items": []}))
            if p == "gpus": return self.send(200, gpus())
            if p == "workers_extra": return self.send(200, {"items": extra_workers()})
            if p == "drives": return self.send(200, drives(q.get("fresh", ["0"])[0] == "1"))
            if p == "status": return self.send(200, server_status())
            if p == "storage": return self.send(200, storage_info(q.get("fresh", ["0"])[0] == "1"))
            if p == "settings": return self.send(200, settings())
            if p == "search":
                s = urllib.parse.quote(q.get("q", [""])[0])
                r = hf(f"/api/models?search={s}&filter=gguf&sort=downloads&direction=-1&limit=20")
                return self.send(200, [{"id": x["id"], "downloads": x.get("downloads")} for x in r])
            if p == "files":
                repo = q.get("repo", [""])[0]
                if not re.match(r"^[\w.-]+/[\w.-]+$", repo): return self.send(400, {"error": "bad repo"})
                tree = hf(f"/api/models/{repo}/tree/main")
                g = [t for t in tree if t.get("path", "").endswith(".gguf")]
                files = [{"i": i, "path": t["path"], "gb": round(t.get("size", 0) / 2**30, 1)}
                         for i, t in enumerate(sorted([t for t in g if "mmproj" not in t["path"].lower() and "/" not in t["path"]], key=lambda t: t.get("size", 0)))]
                return self.send(200, {"files": files, "vision": any("mmproj" in t["path"].lower() for t in g)})
        except Exception as e:
            return self.send(500, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # changes only from this page (custom header = other websites can't send it) and only after login
        if self.headers.get("X-Panel") != "1" or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(403, {"error": "Changes are only allowed from this page."})
        p = urllib.parse.urlparse(self.path).path.rstrip("/").split("/")[-1]
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 20000: return self.send(413, {"error": "too large"})
            b = json.loads(self.rfile.read(n) or b"{}")
            r = self.auth_api(p, b)
            if r: return self.send(r[0], r[1], cookies=r[2])
            if not self.authed(): return self.send(401, {"error": "Please log in."})
            if p == "password":
                if not _check_pw(str(b.get("old", ""))): return self.send(400, {"error": "Current password is wrong."})
                if len(str(b.get("new", ""))) < 10: return self.send(400, {"error": "Use at least 10 characters."})
                write_file(PASSF, _hash_pw(str(b["new"]))); end_sessions()
                tok = new_session(self.client_address[0], self.headers.get("User-Agent", ""))
                return self.send(200, {"output": "Password changed. All other devices were logged out."},
                                 cookies=[f"aip={tok}; Max-Age={SESSION_DAYS*86400}; Path=/; HttpOnly; SameSite=Strict"])
            if p == "logout_all":
                end_sessions(); return self.send(200, {"output": "All devices logged out."}, cookies=["aip=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"])
            if p == "add":
                r = subprocess.run([ADD, b.get("name", ""), b.get("repo", ""), b.get("quant", ""), b.get("mode", "pool")], capture_output=True, text=True, timeout=120)
                out = (r.stdout + r.stderr).strip()
            elif p == "remove":
                if b.get("name") in VIRTUAL: return self.send(400, {"error": "cannot remove built-in virtual models"})
                r = subprocess.run([ADD, "--remove", b.get("name", "")], capture_output=True, text=True, timeout=120)
                out = (r.stdout + r.stderr).strip()
            elif p == "scout":
                subprocess.Popen(["/usr/bin/python3", "-I", "/opt/ai/bin/model-scout.py"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                out = "Checking Hugging Face... the list updates in about a minute."
            elif p == "gpu_policy": out = set_policy(b.get("key", ""), b.get("value", ""))
            elif p == "gpu_name": out = set_gpu_name(b.get("key", ""), b.get("name", ""))
            elif p == "gpu_guard": out = set_guard_settings(b.get("pct", 40), b.get("back", 5))
            elif p == "gpu_forget": out = forget_gpu(str(b.get("key", "")))
            elif p == "rename": out = rename_model(b.get("name", ""), b.get("new", ""))
            elif p == "unload":
                get(f"{SWAP}/unload", 30); out = "All models stopped."
            elif p == "settings": out = save_settings(b)
            elif p == "api_devices": out = set_api_devices(b.get("ips", []))
            elif p == "workers_extra": out = set_extra_workers(b.get("items", []))
            elif p == "drive_test":
                n = str(b.get("name", ""))
                if not re.match(r"^(sd[a-z]{1,2}|nvme\d+n\d+|vd[a-z])$", n): return self.send(400, {"error": "bad disk"})
                r = subprocess.run(["sudo", "-n", f"{HELPERS}/disk-health", "test", n], capture_output=True, text=True, timeout=60)
                out = (r.stdout + r.stderr).strip(); _drive_cache["t"] = 0
            elif p == "storage_preview":
                args, _ = storage_args(b)
                r = subprocess.run(["sudo", "-n", STORAGE_TOOL, *args, "--dry-run"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, timeout=120)
                out = ("Preview - nothing was changed:\n\n" + r.stdout).strip()
            elif p == "storage_run":
                args, erases = storage_args(b)
                if storage_running(): return self.send(409, {"error": "A storage job is already running. Wait for it to finish."})
                if erases:
                    ip = self.client_address[0]
                    if too_many_fails(ip): return self.send(429, {"error": "Too many wrong passwords. Wait 10 minutes."})
                    if str(b.get("confirm", "")).strip() != "ERASE": return self.send(400, {"error": "Type ERASE (capital letters) to confirm."})
                    if not _check_pw(str(b.get("password", ""))):
                        _fails.setdefault(ip, []).append(time.time()); time.sleep(1)
                        return self.send(403, {"error": "Wrong admin password - nothing was erased."})
                    args.append("--yes")
                r = subprocess.run(["sudo", "-n", STORAGE_TOOL, "job", *args], capture_output=True, text=True, timeout=60)
                if r.returncode != 0: return self.send(400, {"error": (r.stdout + r.stderr).strip()[-400:]})
                _st_cache["t"] = 0
                out = "Started - watch the progress below. The AI pauses while models are moved."
            elif p == "rescan":
                _gpu_cache["t"] = 0; out = "Looking for GPU PCs..."
            else: return self.send(404, {"error": "not found"})
            self.send(200, {"output": out})
        except Exception as e:
            self.send(400, {"error": str(e)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8091); ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    threading.Thread(target=guard_loop, daemon=True, name="gpu-auto-off").start()
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()

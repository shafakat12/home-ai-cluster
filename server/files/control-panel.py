#!/usr/bin/python3 -I
"""Home AI Cluster - control panel (port 8091): models, GPUs, server + drive health, adding GPU PCs, settings.
Runs on the AI server as the admin user. Usable from any home device after logging in with the admin password.
The admin password is created once with the setup code printed by the installer (or from ADMIN_PC_IP)."""
import fcntl, json, os, re, glob, time, subprocess, ipaddress, urllib.request, urllib.error, urllib.parse, argparse, hashlib, hmac, secrets, threading, signal, platform, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

ADD = "/opt/ai/bin/add-model.sh"
CFG = "/opt/ai/llama-swap.yaml"
LOGS = "/opt/ai/logs"
POL = "/opt/ai/gpu-policy.json"
SUG = "/opt/ai/suggestions.json"
HELPERS = "/usr/local/lib/home-ai"   # root-owned up to / (never under /opt/ai, which the admin user owns); see /etc/sudoers.d/home-ai
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
PCS_REGISTRY = "/opt/ai/pcs.json"              # {ip: {"api":bool,"share":bool,"name":str,"mac":str}} - per-PC firewall (pc-access-sync)
VERSION = "0.2.0-beta"
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


def pc_registry():
    """The per-PC firewall registry {ip: {"api","share","name","mac"}}. pc-access-sync applies it (8080 + the NFS ports)."""
    d = jload(PCS_REGISTRY, {})
    return d if isinstance(d, dict) else {}


def _valid_pc_ip(ip):
    a = ipaddress.ip_address(str(ip).strip())
    if not a.is_private or a.version != 4: raise ValueError(f"{ip} is not a home-network IPv4 address")
    return str(a)


def pc_sync():
    r = subprocess.run(["sudo", "-n", f"{HELPERS}/pc-access-sync"], capture_output=True, text=True, timeout=60)
    if r.returncode and ("password" in r.stderr or "authentication" in r.stderr):
        raise ValueError("The firewall helper isn't set up yet. Run  sudo bash install.sh 60-control-panel  on the server.")
    return (r.stdout + r.stderr).strip() or "Saved."


def pc_set(ip, name="", api=None, share=None, mac=""):
    ip = _valid_pc_ip(ip)
    reg = pc_registry(); cur = reg.get(ip, {}) if isinstance(reg.get(ip), dict) else {}
    e = {"api": bool(cur.get("api")), "share": bool(cur.get("share")),
         "name": re.sub(r"[^A-Za-z0-9 ._-]", "", str(name or cur.get("name", "")))[:40],
         "mac": str(mac or cur.get("mac", "")).lower()}
    if api is not None: e["api"] = bool(api)
    if share is not None: e["share"] = bool(share)
    reg[ip] = e; write_file(PCS_REGISTRY, json.dumps(reg, indent=1), 0o644)
    return pc_sync()


def pc_remove(ip):
    ip = _valid_pc_ip(ip)
    reg = pc_registry(); reg.pop(ip, None)
    write_file(PCS_REGISTRY, json.dumps(reg, indent=1), 0o644)
    return pc_sync()


def pc_reserve(ip):
    ip = _valid_pc_ip(ip)
    mac = pc_registry().get(ip, {}).get("mac") or _mac_for_ip(ip)
    if not mac: raise ValueError("Couldn't find this PC's MAC address yet - open the Network tab (or Scan now) and try again.")
    d = router_admin("reserve", {"mac": mac, "ip": ip, "name": pc_registry().get(ip, {}).get("name", "")})
    if not d.get("ok"): raise ValueError(d.get("error") or "the router did not confirm the reservation")
    return d.get("output", "Reserved.")


def _mac_for_ip(ip):
    for mac, dv in jload(f"{NET}/inventory.json", {}).items():
        if isinstance(dv, dict) and dv.get("ip") == ip and re.match(r"^[0-9a-f:]{17}$", str(mac).lower()):
            return str(mac).lower()
    return ""


_comfy = {"t": 0, "v": {}}
def comfyui_status():
    if time.time() - _comfy["t"] > 8:
        v = {}
        for ip, e in pc_registry().items():
            if isinstance(e, dict) and e.get("share"):
                try:
                    with socket.create_connection((ip, 8188), 0.6): v[ip] = True
                except OSError: v[ip] = False
        _comfy.update(t=time.time(), v=v)
    return _comfy["v"]


def pcs_info():
    try: brand = router_admin("show").get("brand", "")
    except Exception: brand = ""
    return {"pcs": pc_registry(), "comfyui": comfyui_status(), "can_reserve": brand == "openwrt"}


def api_devices():
    return sorted(ip for ip, e in pc_registry().items() if isinstance(e, dict) and e.get("api"))


def set_api_devices(ips):
    want = set()
    for ip in ips:
        ip = str(ip).strip()
        if ip: want.add(_valid_pc_ip(ip))
    reg = pc_registry()
    for ip in want:
        e = reg.get(ip) if isinstance(reg.get(ip), dict) else {}
        reg[ip] = {"api": True, "share": bool(e.get("share")), "name": e.get("name", ""), "mac": e.get("mac", "")}
    for ip, e in list(reg.items()):
        if ip not in want and isinstance(e, dict):
            e["api"] = False
            if not e.get("share"): reg.pop(ip, None)
    write_file(PCS_REGISTRY, json.dumps(reg, indent=1), 0o644)
    return pc_sync()


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
            if cur not in HIDDEN: names.append({"name": cur, "mode": "", "file": "", "ctx": None, "vision": False, "disk": ""})
        elif cur and names and names[-1]["name"] == cur:
            x = names[-1]
            mm = re.search(r"llama-run\.sh (\w+)", line)
            if mm: x["mode"] = mm.group(1)
            mf = re.search(r"-m (\S+\.gguf)", line)
            if mf:
                x["file"] = os.path.basename(mf.group(1)); x["path"] = mf.group(1)
                try: x["gb"] = round(os.path.getsize(mf.group(1)) / 2**30, 1)
                except OSError: x["gb"] = None
                rota = _rota(os.path.dirname(mf.group(1)))
                x["disk"] = "HDD" if rota else "SSD" if rota is False else ""
            mc = re.search(r"(?:^|\s)-c (\d+)", line)
            if mc: x["ctx"] = int(mc.group(1))
            if "--mmproj " in line: x["vision"] = True
    return names


def progress():
    out = []
    for f in sorted(glob.glob(f"{LOGS}/add-*.log"), key=os.path.getmtime, reverse=True)[:8]:
        name = os.path.basename(f)[4:-4]
        txt = open(f, errors="replace").read().replace("\r", "\n")
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        last = lines[-1] if lines else ""
        marks = [l for l in lines if re.match(r"^(READY|PAUSED|RESUMED|FAILED|Downloading)\b", l)]
        mark = marks[-1].split(":")[0].split()[0] if marks else ""
        pct = speed = left = None; c_done = c_total = None
        for l in reversed(lines):
            fld = l.split()
            if len(fld) >= 12 and re.match(r"^\d{1,3}$", fld[0]):
                pct, left, speed = int(fld[0]), fld[10], fld[11]
                c_total, c_done = _gb(fld[1]), _gb(fld[3]); break
        job, run = _job(name), _running(name)
        done_b = total = 0
        if job.get("TOTAL", "").isdigit() and job.get("MD"):
            total = int(job["TOTAL"]); md = job["MD"]
            done_b = sum(_size(f"{md}/{name}{x}") for x in (".gguf", ".gguf.part", "-mmproj.gguf", "-mmproj.gguf.part"))
            pct = min(100, int(100 * done_b / total)) if total else pct
        state = ("done" if mark == "READY" else "downloading" if run else
                 "paused" if mark == "PAUSED" and job else "failed" if job or "curl:" in last or mark == "FAILED" else
                 "downloading" if mark in ("Downloading", "RESUMED") and pct is not None and pct < 100 else "done" if "READY" in txt else "failed")
        sp = None
        if state == "downloading" and speed and speed != "0":
            m = re.match(r"^([\d.]+)([kMG]?)$", speed)
            if m: sp = round(float(m.group(1)) * {"": 1e-6, "k": 1e-3, "M": 1, "G": 1e3}[m.group(2)] * 1.048576, 1)
        if not total and c_total:          # download started before the job file existed: use curl's numbers
            done_b, total = int(c_done * 2**30), int(c_total * 2**30)
        out.append({"name": name, "state": state, "done": state == "done", "failed": state == "failed",
                    "pct": pct, "speed_mbs": sp, "left": left if state == "downloading" else None,
                    "gb": round(done_b / 2**30, 1) if total else None, "gb_total": round(total / 2**30, 1) if total else None,
                    "can_resume": bool(job) and not run and state != "done", "last": last[-160:],
                    "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f)))})
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
_pol_lock = threading.Lock()   # every read-change-write of POL (panel clicks and auto-off), so no change is lost


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
    with _pol_lock:
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


def _guard_set_mode(w, mode, expected):
    """Set GPU PC w to mode, but only if its mode is still `expected` (what auto-off saw at the start of the check).
    If someone changed it in the panel meanwhile, their choice wins and nothing is written."""
    with _pol_lock:
        pol = jload(POL, {})
        if pol.get("workers", {}).get(w, "use") != expected: return False
        pol.setdefault("workers", {})[w] = mode
        write_file(POL, json.dumps(pol, indent=1), 0o644)
    return True


def guard_tick():
    with _guard_lock: _guard_tick()


def forget_gpu(key):
    """Remove an offline GPU PC (name, mode, auto-off state), e.g. after it got a new IP address."""
    if not RE_WORKER.match(key): raise ValueError("bad gpu")
    if key in workers_in_use() or _gpu_cache["workers"].get(key, {}).get("online"):
        raise ValueError("This GPU PC is online. Only offline GPU PCs can be forgotten.")
    with _guard_lock:
        with _pol_lock:
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
                    if not _guard_set_mode(w, prev, "off"): continue   # changed by hand meanwhile: the next check sees it
                    e.update(auto_off=False, low_since=None)
                    _guard_note(f"✅ {name}: auto-off was turned off, so the AI may use this GPU again ({prev})." if thr == 0 else
                                f"✅ {name}: its PC is free again ({pct}%), so the AI may use this GPU again ({prev}).")
            else:
                e["low_since"] = None
        elif thr > 0 and pct > thr and mode in ("use", "standby") and not e.get("hold"):
            _guard["hits"][w] = _guard["hits"].get(w, 0) + 1
            if _guard["hits"][w] >= 2:                             # two checks in a row (about 10 s), not a short spike
                _guard["hits"][w] = 0
                if not _guard_set_mode(w, "off", mode): continue    # changed by hand meanwhile: keep that choice
                e.update(auto_off=True, prev=mode, since=now, low_since=None)
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
    sip = conf().get("SERVER_IP", "")
    rows = [{"key": "server", "where": f"AI server ({sip})" if sip else "AI server", **sg, "model": sg.get("name", ""),
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
    with _pol_lock:
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
    if key != "server" and not RE_WORKER.match(key): raise ValueError("bad gpu")
    with _pol_lock:
        pol = jload(POL, {})
        if key == "server": pol["server"] = value
        else: pol.setdefault("workers", {})[key] = value
        write_file(POL, json.dumps(pol, indent=1), 0o644)
    try: get(f"{SWAP}/unload", 30)
    except Exception: pass
    return "Saved. Running models were stopped; the next chat starts them with the new GPU choice."


_prev = {}
SERVICES = [("llama-swap", "AI models (llama-swap)"), ("open-webui", "Chat website (Open WebUI)"), ("ai-control-panel", "Control panel"),
            ("ai-worker-kit", "Worker kit (port 8090)"), ("docker", "Docker"), ("ai-health-check.timer", "Health check (10 min)"),
            ("ai-backup.timer", "Nightly backup"), ("ai-model-scout.timer", "Weekly model finder"),
            ("ai-net-exec", "Network changes runner"), ("ai-net-scan.timer", "Network scan (10 min)")]
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
    pv = _prev.get("s")
    if not pv or now - pv[0] > 0.5:       # several pages/pollers may ask at once: only measure over >= 0.5 s, else reuse the last result
        _prev["s"] = (now, tot, idle, rx, tx)
        if pv:
            dt = tot - pv[1]
            _prev["v"] = (round(100 * (1 - (idle - pv[2]) / dt), 1) if dt else 0,
                          round((rx - pv[3]) / (now - pv[0]) / 1e6, 2), round((tx - pv[4]) / (now - pv[0]) / 1e6, 2))
    cpu, rxr, txr = _prev.get("v", (None, None, None))
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
            rota = _rota(m)
            disks.append({"mount": m, "total_gb": round(total / 1e9), "used_gb": round((total - free) / 1e9), "pct": round(100 * (total - free) / total),
                          "kind": "HDD" if rota else "SSD" if rota is False else ""})
        except Exception: pass
    svc = [(u, lab) for u, lab in SERVICES if u == "docker" or os.path.exists(f"/etc/systemd/system/{u}")
           or os.path.exists(f"/etc/systemd/system/{u}.service")]
    names = [u for u, _ in svc]
    try: act = subprocess.run(["systemctl", "is-active", *names], capture_output=True, text=True, timeout=10).stdout.split()
    except Exception: act = []
    services = [{"unit": u, "label": lab, "state": act[i] if i < len(act) else "unknown", "can_restart": can_restart(u)}
                for i, (u, lab) in enumerate(svc)]
    def probe(item):
        url, lab = item; t0 = time.time()
        try: get(url, 2); return {"label": lab, "ok": True, "ms": round((time.time() - t0) * 1000)}
        except Exception: return {"label": lab, "ok": False}
    epl = [e for e in ENDPOINTS if not (e[1] == "Chat website" and "open-webui" not in names)
           and not (e[1] == "Web search" and not os.path.exists("/opt/ai/searxng/settings.yml"))]
    with ThreadPoolExecutor(len(epl)) as ex: eps = list(ex.map(probe, epl))
    try: bk = subprocess.run(["systemctl", "show", "ai-backup.service", "-p", "ExecMainExitTimestamp", "-p", "Result", "-p", "ActiveState"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception: bk = ""
    bkd = dict(l.split("=", 1) for l in bk.splitlines() if "=" in l)
    try: bts = subprocess.run(["systemctl", "show", "ai-backup.service", "--timestamp=unix", "-p", "ExecMainExitTimestamp", "--value"],
                              capture_output=True, text=True, timeout=10).stdout.strip().lstrip("@")
    except Exception: bts = ""
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
            "backup": {"last": bkd.get("ExecMainExitTimestamp", ""), "result": bkd.get("Result", ""), "running": bkd.get("ActiveState") == "activating",
                       "last_ts": int(bts) if bts.isdigit() else None}, "problems": problems}


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
    tg, ag = read_env("telegram"), read_env("adguard")
    try: pub = open(f"{SEC}/ssh_key.pub").read().strip()
    except Exception: pub = ""
    return {"telegram": {"set": bool(tg.get("TOKEN")), "chat_id": tg.get("CHAT_ID", "")}, "allow": sorted(allowed_ips()),
            "api_devices": api_devices(), "network": network_on(), "pubkey": pub,
            "adguard": {"url": ag.get("URL", ""), "user": ag.get("USER", ""), "has_pass": bool(ag.get("PASS"))},
            "sessions": [{"id": k[:16], "ip": v.get("ip"), "ua": v.get("ua"), "created": v.get("created")} for k, v in _sessions().items()]}


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
    if b.get("section") == "adguard":
        url = b.get("url", "").strip().rstrip("/")
        if not re.match(r"^https?://[\w.-]+(:\d{1,5})?$", url): raise ValueError("Address must look like http://192.168.1.2 or http://192.168.1.2:3000")
        old = read_env("adguard")
        write_env("adguard", {"URL": url, "USER": b.get("user", "").strip(), "PASS": b.get("pass") or old.get("PASS", "")})
        return "AdGuard saved. Press Test to check it."
    raise ValueError("unknown section")


def test(what):
    if what == "router": return router_summary(router_admin("test"))
    if what != "adguard": raise ValueError("unknown test")
    r = subprocess.run(["/usr/bin/python3", "-I", f"{HELPERS}/netctl", "adguard", "status"], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()[-1500:] or "(no output)"


# ------------------------------------------------------------------ models: details, load / unload / test, cancel
def _rota(path):
    """True = spinning disk (HDD), False = SSD, None = unknown. Walks up from a partition/LVM/RAID device to its queue info."""
    try:
        d = os.stat(path).st_dev; p = os.path.realpath(f"/sys/dev/block/{os.major(d)}:{os.minor(d)}")
        while len(p) > 5 and not os.path.exists(f"{p}/queue/rotational"): p = os.path.dirname(p)
        with open(f"{p}/queue/rotational") as f: return f.read().strip() == "1"
    except Exception:
        return None


_usage = {"t": 0, "d": {}}


def last_used():
    """model -> unix time of its last request, from llama-swap's "TTL of Ns reached" unload lines (unload time - TTL).
    Cached for a minute. Models that were stopped another way have no record."""
    if time.time() - _usage["t"] > 60:
        d = {}
        try:
            out = subprocess.run(["journalctl", "-u", "llama-swap", "--since", "-30d", "--no-pager", "-o", "short-unix", "-g", "TTL of"],
                                 capture_output=True, text=True, timeout=20).stdout
            for m in re.finditer(r"^(\d+)(?:\.\d+)? .*?<([^>]+)> Unloading model, TTL of (\d+)s reached", out, re.M):
                d[m.group(2)] = max(d.get(m.group(2), 0), int(m.group(1)) - int(m.group(3)))
        except Exception: pass
        _usage.update(t=time.time(), d=d)
    return _usage["d"]


def _model_names(): return [x["name"] for x in installed()]


def load_model(name):
    if name not in _model_names(): raise ValueError("model not found")
    def run():
        try: get(f"{SWAP}/upstream/{urllib.parse.quote(name)}/health", 1800)
        except Exception as ex: print("load", name, ex, flush=True)
    threading.Thread(target=run, daemon=True).start()
    return f"Loading {name}... It shows as loaded when it is ready (big models take a minute or two). Models it can't run next to are stopped."


def unload_model(name):
    if name not in _model_names(): raise ValueError("model not found")
    url = f"{SWAP}/api/models/unload/{urllib.parse.quote(name)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=30): pass
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405): raise
        get(url, 30)                                   # older llama-swap: GET
    return f"Stopped {name}. Its GPU memory is free again."


def test_model(name):
    """One short chat message to the model; returns the answer and the speed."""
    if name not in _model_names(): raise ValueError("model not found")
    body = json.dumps({"model": name, "max_tokens": 300, "stream": False,
                       "messages": [{"role": "user", "content": "In one short sentence: why is the sky blue?"}]}).encode()
    t0 = time.time()
    req = urllib.request.Request(f"{SWAP}/v1/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r: d = json.loads(r.read())
    secs = time.time() - t0
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    toks = (d.get("usage") or {}).get("completion_tokens")
    tps = (d.get("timings") or {}).get("predicted_per_second")
    return {"output": text[:800] or "(empty answer)", "tps": round(tps, 1) if tps else None, "tokens": toks, "secs": round(secs, 1),
            "tps_approx": None if tps or not toks else round(toks / secs, 1)}


def _job(name):
    j = {}
    try:
        with open(f"{LOGS}/add-{name}.job") as f:
            for l in f:
                k, _, v = l.strip().partition("="); j[k] = v.strip("'")
    except OSError: pass
    return j


def _running(name):
    try:
        with open(f"{LOGS}/add-{name}.pid") as f: pid = int(f.read().strip())
        os.kill(pid, 0); return True
    except (OSError, ValueError): return False


def _size(p):
    try: return os.path.getsize(p)
    except OSError: return 0


def _gb(v):                                # curl sizes like 7.75G, 569.1M, 812k -> GB
    m = re.match(r"^([\d.]+)([kMGT]?)$", v or "")
    return float(m.group(1)) * {"": 2**-30, "k": 2**-20, "M": 2**-10, "G": 1, "T": 1024}[m.group(2)] if m else None


def cancel_download(name):
    """Stop a download and delete its partial files. Never touches an installed model."""
    if not RE_NAME.match(name): raise ValueError("bad name")
    if name in _model_names(): raise ValueError("This model is already installed. Use Remove instead.")
    try:
        with open(f"{LOGS}/add-{name}.pid") as f: pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline") as f: is_dl = "model-download.sh" in f.read()       # not a reused pid
        if is_dl:
            try: os.killpg(pid, signal.SIGTERM)                               # the script and its curl
            except OSError: os.kill(pid, signal.SIGTERM)
            time.sleep(1)
    except (OSError, ValueError): pass
    freed = 0
    for d in {conf().get("MODELS_DIR", "/opt/ai/models"), _job(name).get("MD", "")} - {""}:
        for x in (".gguf", ".gguf.part", "-mmproj.gguf", "-mmproj.gguf.part"):
            p = f"{d}/{name}{x}"
            if os.path.isfile(p) and not os.path.islink(p): freed += os.path.getsize(p); os.remove(p)
    for x in (".job", ".pid", ".log"):
        try: os.remove(f"{LOGS}/add-{name}{x}")
        except OSError: pass
    return f"Cancelled {name}" + (f" and deleted {freed / 2**30:.1f} GB of partial files." if freed else ".")


# ------------------------------------------------------------------ services the panel may restart, logs, disk usage
_np = {"t": 0, "out": ""}
RESTARTABLE = ("llama-swap", "open-webui", "ai-net-exec")      # /etc/sudoers.d/home-ai allows exactly these


def nopasswd_rules():
    if time.time() - _np["t"] > 60:
        try: out = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10).stdout
        except Exception: out = ""
        _np.update(t=time.time(), out=out)
    return _np["out"]


def can_restart(unit): return unit in RESTARTABLE and f"restart --no-block {unit}.service" in nopasswd_rules()

# ------------------------------------------------------------------ power: restart / shut down the server (10 second delay)
PWR_DELAY = 10
PWR_UNITS = {"restart": "home-ai-restart", "shutdown": "home-ai-poweroff"}
POWER_CMDS = {   # /etc/sudoers.d/home-ai-power allows exactly these three command lines (sudoers writes each '=' as '\=')
    "restart": ["/usr/bin/systemd-run", "--unit=home-ai-restart", f"--on-active={PWR_DELAY}", "/usr/bin/systemctl", "reboot"],
    "shutdown": ["/usr/bin/systemd-run", "--unit=home-ai-poweroff", f"--on-active={PWR_DELAY}", "/usr/bin/systemctl", "poweroff"],
    "cancel": ["/usr/bin/systemctl", "stop", "home-ai-restart.timer", "home-ai-poweroff.timer"]}
OLD_POWER_RULE = "/usr/sbin/shutdown -r +1"   # older installs (1 minute delay)
TIMER_LIST = ["systemctl", "list-timers", "-o", "json", "home-ai-restart.timer", "home-ai-poweroff.timer"]   # no sudo needed


class Busy(ValueError): pass   # answered with 409


def power_scheduled():
    """The restart / poweroff timer that the power commands start, or None. Read without sudo."""
    try: timers = json.loads(subprocess.run(TIMER_LIST, capture_output=True, text=True, timeout=10).stdout or "[]")
    except Exception: return None
    for t in timers:
        for mode, unit in PWR_UNITS.items():
            if t.get("unit") == unit + ".timer":   # "next" is wall-clock microseconds; 0 once it has fired
                return {"mode": mode, "at": (t.get("next") or 0) / 1e6 or time.time()}
    return None


def power_info():
    """Scheduled restart/shutdown, the server's name, whether the sudo rule is there, and what a restart would interrupt."""
    try: models = [x.get("model") for x in json.loads(get(f"{SWAP}/running", 3)).get("running", []) if x.get("model")]
    except Exception: models = []
    names = [os.path.basename(f)[4:-4] for f in glob.glob(f"{LOGS}/add-*.job")]
    backup = subprocess.run(["systemctl", "is-active", "--quiet", "ai-backup.service"]).returncode == 0
    rules = nopasswd_rules().replace("\\", "")   # sudo -l may show '=' as '\='
    can = all(" ".join(c) in rules for c in POWER_CMDS.values())
    return {"scheduled": power_scheduled(), "hostname": os.uname().nodename, "now": time.time(), "delay": PWR_DELAY,
            "can": can, "old": not can and OLD_POWER_RULE in rules,
            "interrupts": {"downloads": [n for n in names if _running(n)], "models": models, "backup": backup}}


def power(action, confirm, who):
    """Restart / shut down in 10 seconds, or cancel it. Fixed command lines only, never user text."""
    if action not in POWER_CMDS: raise ValueError("unknown action")
    sch = power_scheduled()
    if action == "cancel" and not sch: raise ValueError("Nothing is scheduled.")
    if action != "cancel" and sch: raise Busy(f"A {sch['mode']} is already scheduled. Cancel it first.")
    if action == "shutdown" and str(confirm or "").strip() != os.uname().nodename:
        raise ValueError("Type the server's name exactly to confirm the shutdown.")
    r = subprocess.run(["sudo", "-n", *POWER_CMDS[action]], capture_output=True, text=True, timeout=20)
    # cancel stops both timers; systemctl exits 5 for the one that wasn't running, so check the result instead
    if r.returncode and not (action == "cancel" and not power_scheduled()):
        print(f"power: {action} FAILED for {who}: {r.stderr.strip()[-200:]}", flush=True)
        if "password" in r.stderr or "authentication" in r.stderr:
            raise ValueError("The panel isn't allowed to do this yet: run  sudo bash install.sh 60-control-panel  on the server once.")
        raise ValueError(r.stderr.strip()[-300:] or "the command failed")
    print(f"power: {action}" + (f" (in {PWR_DELAY} s)" if action != "cancel" else "") + f" by {who}", flush=True)
    return {"restart": f"The server restarts in {PWR_DELAY} seconds. You can still cancel it.",
            "shutdown": f"The server shuts down in {PWR_DELAY} seconds. You can still cancel it.",
            "cancel": "Cancelled. The server keeps running."}[action]


LOG_SOURCES = {"panel": ("Control panel", ["ai-control-panel"]), "llama": ("AI models (llama-swap)", ["llama-swap"]),
               "downloads": ("Model downloads", None), "starter": ("Starter model download", f"{LOGS}/model-download.log"),
               "health": ("Health check", ["ai-health-check"]), "backup": ("Backups", ["ai-backup"]),
               "network": ("Network watch", ["ai-net-scan", "ai-net-exec"])}
RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RE_CURL = re.compile(r"^\d{1,3}\s+[\d.]+[kMGT]?\s")      # a curl progress row


def read_log(src, n=200):
    if src not in LOG_SOURCES: raise ValueError("unknown log")
    what = LOG_SOURCES[src][1]
    if isinstance(what, list):
        cmd = ["journalctl", "-n", str(n * 10 if src == "panel" else n), "--no-pager", "-o", "short-iso"]
        for u in what: cmd += ["-u", u]
        lines = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.splitlines()
        if src == "panel": lines = [l for l in lines if not re.search(r" sudo\[\d+\]: ", l)][-n:]   # the panel's own sudo calls
    elif isinstance(what, str):
        try:
            with open(what, errors="replace") as f: lines = [l for l in f.read().replace("\r", "\n").split("\n") if l.strip()][-n:]
        except OSError: lines = []
    else:                                                # download logs: curl's progress rows only once per run
        lines = []
        for f in sorted(glob.glob(f"{LOGS}/add-*.log"), key=os.path.getmtime)[-5:]:
            name = os.path.basename(f)[4:-4]
            with open(f, errors="replace") as fh: ls = [l.strip() for l in fh.read().replace("\r", "\n").split("\n") if l.strip()]
            for i, l in enumerate(ls):
                if l.startswith("%") or "Dload" in l: continue
                if RE_CURL.match(l) and i + 1 < len(ls) and RE_CURL.match(ls[i + 1]): continue
                lines.append(f"[{name}] {l}")
        lines = lines[-n:]
    return {"src": src, "lines": [RE_ANSI.sub("", l)[:600] for l in lines],
            "sources": [{"id": k, "label": v[0]} for k, v in LOG_SOURCES.items()]}


_use = {"t": 0, "data": None}
_use_lock = threading.Lock()


def backups_list():
    """Backups from the backup service log ("backup 1.2G -> <folder>/2026-11-22"); the folder itself is root-only."""
    try: out = subprocess.run(["journalctl", "-u", "ai-backup", "--since", "-60d", "--no-pager", "-o", "short-unix", "-g", "^backup "],
                              capture_output=True, text=True, timeout=20).stdout
    except Exception: out = ""
    seen = {}
    for m in re.finditer(r"^(\d+)\S* \S+ \S+ backup (\S+) -> (\S+)$", out, re.M):
        seen[m.group(3)] = {"when": int(m.group(1)), "size": m.group(2), "gb": _gb(m.group(2)), "name": os.path.basename(m.group(3))}
    return sorted(seen.values(), key=lambda x: x["name"], reverse=True)[:7]      # the backup keeps the newest 7


def disk_usage(force=False):
    """What is where: disks, folders with sizes, the biggest files, the backups."""
    with _use_lock:
        if not force and _use["data"] and time.time() - _use["t"] < 120: return _use["data"]
        c = conf(); core, md, bd = c.get("CORE_DIR", ""), c.get("MODELS_DIR", "/opt/ai/models"), c.get("BACKUP_DIR", "/opt/ai/backups")
        folders = [(core, "Starter models", "The models from the installer. On the system disk, never moved."),
                   (md, "Downloaded models", "Models you add in the panel. Moved with the Storage tab (drive or RAID)."),
                   (bd, "Backups", "Nightly backup of settings and chats. The last 7 days are kept."),
                   ("/opt/ai/llama.cpp", "AI engine (llama.cpp)", "The program that runs the models."),
                   ("/opt/ai/logs", "Logs", "Install and download logs.")]
        folders = [f for i, f in enumerate(folders) if f[0] and os.path.isdir(f[0]) and f[0] not in [g[0] for g in folders[:i]]]
        mounts, disks = [], []
        for p in ["/"] + [f[0] for f in folders]:
            m = os.path.realpath(p)
            while m != "/" and not os.path.ismount(m): m = os.path.dirname(m)
            if m not in mounts: mounts.append(m)
        for m in mounts:
            try:
                st = os.statvfs(m); total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize; rota = _rota(m)
                disks.append({"mount": m, "label": "System disk" if m == "/" else f"Disk at {m}", "kind": "HDD" if rota else "SSD" if rota is False else "",
                              "total_gb": round(total / 1e9), "used_gb": round((total - free) / 1e9), "pct": round(100 * (total - free) / total)})
            except Exception: pass
        try: du = subprocess.run(["du", "-s", "-B1", *[f[0] for f in folders]], capture_output=True, text=True, timeout=60).stdout
        except Exception: du = ""
        sizes = {l.split("\t", 1)[1]: int(l.split("\t", 1)[0]) for l in du.splitlines() if "\t" in l}
        bks = backups_list(); out = []
        for p, name, what in folders:
            approx = p == bd; rota = _rota(p)
            gb = sum(b["gb"] or 0 for b in bks) if approx else sizes.get(p, 0) / 2**30
            out.append({"path": p, "name": name, "what": what, "gb": round(gb, 1), "approx": approx, "kind": "HDD" if rota else "SSD" if rota is False else ""})
        try: fo = subprocess.run(["find", *[f[0] for f in folders if f[0] != bd], "-xdev", "-type", "f", "-size", "+200M", "-printf", r"%s\t%p\n"],
                                 capture_output=True, text=True, timeout=60).stdout
        except Exception: fo = ""
        big = sorted(((int(a), b) for a, _, b in (l.partition("\t") for l in fo.splitlines()) if a.isdigit()), reverse=True)[:12]
        data = {"disks": disks, "folders": out, "backups": bks, "time": time.strftime("%H:%M:%S"),
                "biggest": [{"path": p, "gb": round(s / 2**30, 1)} for s, p in big]}
        _use.update(t=time.time(), data=data)
        return data


# ------------------------------------------------------------------ network watch (optional, server/steps/45-network.sh)
NET = "/var/lib/home-ai/net"
RE_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
RE_REQ = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def network_on(): return os.path.exists(f"{HELPERS}/net-exec") and conf().get("NETWORK_WATCH") == "yes"


def netctl():
    try:
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("netctl", f"{HELPERS}/netctl")
        sp = importlib.util.spec_from_loader("netctl", ld); m = importlib.util.module_from_spec(sp); ld.exec_module(m)
        return m
    except Exception:
        return None


def network():
    inv = jload(f"{NET}/inventory.json", {}); ports = jload(f"{NET}/ports.json", {}); labels = jload(f"{NET}/labels.json", {})
    devs = []
    for mac, d in inv.items():
        lab = labels.get(mac, {})
        devs.append({"mac": mac, "ip": d.get("ip", ""), "vendor": d.get("vendor", ""), "name": d.get("name", ""),
                     "label": lab.get("label", d.get("label", "")), "known": bool(lab.get("known", d.get("known"))),
                     "last_seen": d.get("last_seen", ""), "first_seen": d.get("first_seen", ""),
                     "risky": ports.get(d.get("ip", ""), {}).get("risky", [])})
    def ipkey(d):
        try: return tuple(int(x) for x in d["ip"].split("."))
        except Exception: return (999,)
    devs.sort(key=ipkey)
    alerts = []
    try:
        with open(f"{NET}/alerts.log") as f:
            for l in f.readlines()[-60:]:
                try: alerts.append(json.loads(l))
                except Exception: pass
    except FileNotFoundError: pass
    try: scanned = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f"{NET}/inventory.json")))
    except OSError: scanned = None
    return {"devices": devs, "alerts": alerts[::-1], "scanned": scanned}


def set_label(mac, known, label):
    mac = str(mac).lower()
    if not RE_MAC.match(mac): raise ValueError("bad MAC")
    labels = jload(f"{NET}/labels.json", {})
    labels[mac] = {"known": bool(known), "label": re.sub(r"[^\w .'-]", "", str(label))[:40]}
    write_file(f"{NET}/labels.json", json.dumps(labels, indent=1), 0o644)
    return "Saved."


def requests_list():
    N = netctl(); out = []
    d = f"{NET}/requests"
    for f in sorted(os.listdir(d) if os.path.isdir(d) else [], reverse=True)[:60]:
        r = jload(f"{d}/{f}", None)
        if not r: continue
        try: r["text"] = N.describe(r) if N else r["action"]
        except Exception: r["text"] = r.get("action")
        out.append(r)
    return [r for r in out if r.get("status") == "pending"] + [r for r in out if r.get("status") != "pending"]


def decide(rid, decision):
    if not RE_REQ.match(rid or "") or decision not in ("approve", "reject"): raise ValueError("bad request")
    p = f"{NET}/requests/{rid}.json"
    if os.path.islink(p) or not os.path.isfile(p): raise ValueError("not found")
    r = json.load(open(p))
    if r.get("status") != "pending": return f"Already {r.get('status')}."
    r["status"] = "approved" if decision == "approve" else "rejected"
    r["decided"] = time.strftime("%Y-%m-%d %H:%M:%S"); r["decided_via"] = "control panel"
    write_file(p, json.dumps(r, indent=1), 0o644)
    return "Approved - it runs within a few seconds." if decision == "approve" else "Rejected."


def net_request(action, mac, name):
    """Ask for a router block/unblock (it still needs the owner's approval, in the Approvals tab or in Telegram)."""
    if action not in ("router_block_mac", "router_unblock_mac"): raise ValueError("bad action")
    mac = str(mac).lower()
    if not RE_MAC.match(mac): raise ValueError("bad MAC")
    params = json.dumps({"mac": mac, "name": re.sub(r"[^A-Za-z0-9 ._-]", "", str(name))[:40]})
    r = subprocess.run(["sudo", "-n", f"{HELPERS}/netctl", "request", action, params, "asked in the control panel"],
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).strip()
    if r.returncode: raise ValueError(out[-300:] or "could not create the request")
    return out + " Approve it in the Approvals tab (or in Telegram)."


ROUTER_KEYS = ("brand", "host", "port", "user", "pass", "key", "secret", "site", "omadac", "client_id", "client_secret")
NO_ROUTER_ADMIN = "The network watch isn't installed. Set INSTALL_NETWORK_WATCH=yes in config.env and run  sudo bash install.sh 45-network"


def router_admin(cmd, payload=None):
    """router-admin (root, exact sudo rules from step 45): show / save / test / detect / forget-pin. Returns its JSON."""
    if cmd not in ("show", "save", "test", "detect", "forget-pin"): raise ValueError("bad router command")
    try:
        r = subprocess.run(["sudo", "-n", f"{HELPERS}/router-admin", cmd], input=json.dumps(payload or {}),
                           capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        raise ValueError("The router did not answer in time.")
    try: return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        if "password" in r.stderr or not os.path.exists(f"{HELPERS}/router-admin"): raise ValueError(NO_ROUTER_ADMIN)
        raise ValueError((r.stderr or "router helper failed").strip()[-300:])


def router_summary(t):
    if not t.get("ok"): return "Not connected: " + str(t.get("error") or "unknown error")
    s = t.get("status") or {}
    what = " ".join(str(s.get(k)) for k in ("model", "version") if s.get(k))
    return f"Connected{(': ' + what) if what else ''}. {t.get('clients', 0)} devices found. This router type can: {', '.join(t.get('caps') or [])}."


# ------------------------------------------------------------------ settings helpers, About
def end_session_id(sid):
    """Log out one device (id = start of its session hash, as shown in settings)."""
    if not re.match(r"^[0-9a-f]{16}$", sid or ""): raise ValueError("bad id")
    with _lock:
        s = _sessions(); k = next((k for k in s if k.startswith(sid)), None)
        if not k: raise ValueError("That device is already logged out.")
        s.pop(k); write_file(SESSF, json.dumps(s, indent=1))
    return "That device is logged out."


def telegram_test():
    c = read_env("telegram")
    if not (c.get("TOKEN") and c.get("CHAT_ID")): raise ValueError("Save the bot token and your user ID first.")
    req = urllib.request.Request(f"https://api.telegram.org/bot{c['TOKEN']}/sendMessage", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"chat_id": c["CHAT_ID"], "text": "🔔 Test from the Home AI control panel."}).encode())
    try:
        with urllib.request.urlopen(req, timeout=15): pass
    except urllib.error.HTTPError as e:                 # message only: the URL contains the bot token
        raise ValueError(f"Telegram refused it (HTTP {e.code}). Check the bot token and your user ID, and press Start in the bot chat.")
    except Exception:
        raise ValueError("Could not reach Telegram. Check the server's internet connection.")
    return "Test message sent. Check Telegram (press Start in the bot chat first if nothing arrives)."


_about = {"t": 0, "v": {}}


def about():
    """Server address, versions of the main parts, uptime. Versions are cached for 10 minutes."""
    if time.time() - _about["t"] > 600:
        def ver(cmd, rx):
            try:
                o = subprocess.run(cmd, capture_output=True, text=True, timeout=10); m = re.search(rx, o.stdout + o.stderr)
                return m.group(1) if m else None
            except Exception: return None
        try:
            with open("/etc/os-release") as f: osn = next(l.split("=", 1)[1].strip().strip('"') for l in f if l.startswith("PRETTY_NAME="))
        except Exception: osn = None
        try: owu = json.loads(get("http://127.0.0.1:3000/api/version", 3)).get("version")
        except Exception: owu = None
        _about.update(t=time.time(), v={"llama.cpp": ver([LLAMA, "--version"], r"version: (.+)"),
                                        "llama-swap": ver(["/opt/ai/bin/llama-swap", "--version"], r"version: (\S+)"),
                                        "Open WebUI": owu, "System": osn, "Linux kernel": os.uname().release, "Python": platform.python_version()})
    with open("/proc/uptime") as f: up = int(float(f.read().split()[0]))
    return {"host": os.uname().nodename, "ip": conf().get("SERVER_IP", ""), "uptime_s": up, "versions": _about["v"]}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Home AI Control Panel</title>
<script>(function(){try{var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t;if(localStorage.getItem('mini')=='1')document.documentElement.classList.add('mini')}catch(e){}})()</script>
<style>
/* ---- design tokens (light; dark below, also forced by the theme button) ---- */
:root{color-scheme:light;--bg:#f5f6f8;--bgt:#f5f6f8d9;--card:#fff;--fg:#16181d;--mut:#646b77;--b:#e3e6eb;--b2:#cdd2d9;--hov:#eef0f3;--track:#e6e9ee;
 --a:#3a62de;--a2:#3a62de17;--af:#fff;--ok:#14864a;--okb:#14864a17;--warn:#a86500;--warnb:#e09b0024;--bad:#cc3434;--badb:#cc343417;
 --sh:0 1px 2px #1018280d;--sh2:0 12px 32px #10182829,0 2px 6px #1018280f;--r:12px}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0e1014;--bgt:#0e1014d9;--card:#171a1f;--fg:#e8eaee;--mut:#99a0ac;--b:#262a31;--b2:#383d46;--hov:#1e2228;--track:#2a2f37;
 --a:#7b96ff;--a2:#7b96ff21;--af:#0b0d12;--ok:#41c483;--okb:#41c4831f;--warn:#e9b44c;--warnb:#e9b44c1f;--bad:#ff6b6b;--badb:#ff6b6b1f;--sh:0 1px 2px #0007;--sh2:0 16px 40px #000b}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0e1014;--bgt:#0e1014d9;--card:#171a1f;--fg:#e8eaee;--mut:#99a0ac;--b:#262a31;--b2:#383d46;--hov:#1e2228;--track:#2a2f37;
 --a:#7b96ff;--a2:#7b96ff21;--af:#0b0d12;--ok:#41c483;--okb:#41c4831f;--warn:#e9b44c;--warnb:#e9b44c1f;--bad:#ff6b6b;--badb:#ff6b6b1f;--sh:0 1px 2px #0007;--sh2:0 16px 40px #000b}
/* ---- base ---- */
*{box-sizing:border-box}html{background:var(--bg);-webkit-text-size-adjust:100%}
body{margin:0;font:14.5px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:var(--fg);background:var(--bg);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
h1{font-size:24px;line-height:1.25;margin:0;font-weight:650;letter-spacing:-.02em}
h2{font-size:15px;line-height:1.35;margin:32px 0 8px;font-weight:620}
small,.m{color:var(--mut)}small{font-size:12.5px}a{color:var(--a);text-underline-offset:2px}b{font-weight:600}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--hov);border:1px solid var(--b);padding:1px 6px;border-radius:6px;word-break:break-all}
ol{padding-left:22px}li{margin:6px 0}details summary{cursor:pointer;margin:8px 0}
.ic{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
:focus-visible{outline:2px solid var(--a);outline-offset:2px}
/* ---- layout: sidebar (desktop) / bottom bar (phone) + top bar ---- */
.shell{display:flex;min-height:100vh}
#side{position:sticky;top:0;height:100vh;width:236px;flex:none;display:flex;flex-direction:column;gap:2px;padding:16px 12px;background:var(--card);border-right:1px solid var(--b);transition:width .2s ease;overflow:hidden;z-index:30}
.brand{display:flex;align-items:center;gap:12px;padding:0 6px 20px;font-weight:650;font-size:15px;white-space:nowrap;letter-spacing:-.01em}
.logo{width:34px;height:34px;border-radius:10px;background:var(--a);color:var(--af);display:grid;place-items:center;flex:none;box-shadow:0 0 0 4px var(--a2)}
.logo .ic{width:18px;height:18px}
nav{display:flex;flex-direction:column;gap:2px;flex:1}
.nb{display:flex;align-items:center;justify-content:flex-start;gap:12px;width:100%;padding:9px 12px;border:0;border-radius:10px;background:none;color:var(--mut);font-weight:500;white-space:nowrap;position:relative;box-shadow:none}
.nb:hover{background:var(--hov);color:var(--fg);filter:none}.nb.on{background:var(--a2);color:var(--a);font-weight:600}
#pc{margin-left:auto}
.badge{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:var(--bad);color:#fff;font-size:11px;font-weight:700;line-height:1}
@media(min-width:721px){.mini #side{width:72px}.mini .brand span,.mini .nb .l{display:none}.mini .nb{justify-content:center;padding:10px 0}
 .mini .brand{justify-content:center;padding-left:0;padding-right:0}.mini #col .ic{transform:rotate(180deg)}.mini #pc{position:absolute;top:3px;right:9px}}
#main{flex:1;min-width:0}
#top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;min-height:64px;padding:10px 32px;background:var(--bgt);
 -webkit-backdrop-filter:saturate(1.5) blur(12px);backdrop-filter:saturate(1.5) blur(12px);border-bottom:1px solid var(--b)}
.srv{display:flex;flex-direction:column;min-width:0;margin-right:auto;line-height:1.3}
.srv b,.srv small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.srv b{font-size:14.5px}#upd.stale{color:var(--warn)}
.chip{white-space:nowrap;display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:var(--card);border:1px solid var(--b);color:var(--fg);font-size:13px;font-weight:550;box-shadow:var(--sh)}
button.chip:hover{background:var(--hov);filter:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mut);flex:none;vertical-align:middle}
.dot.ok,.dot.up{background:var(--ok);box-shadow:0 0 0 3px var(--okb)}.dot.mid{background:var(--warn);box-shadow:0 0 0 3px var(--warnb)}.dot.bad,.dot.down{background:var(--bad);box-shadow:0 0 0 3px var(--badb)}
td .dot{margin-right:8px}
.ib{display:inline-grid;place-items:center;width:38px;height:38px;padding:0;border-radius:10px;border:1px solid transparent;background:none;color:var(--mut);box-shadow:none}
.ib:hover{background:var(--hov);color:var(--fg);filter:none}
.content{max-width:1200px;margin:0 auto;padding:28px 32px 72px}
#ptitle{margin-bottom:4px}.tab>h2:first-child{margin-top:20px}
.tab{display:none}.tab.on{display:block;animation:fade .22s ease-out}@keyframes fade{from{opacity:0;transform:translateY(4px)}}
/* ---- controls ---- */
input,select,textarea,button{font:inherit;color:inherit}
input,select,textarea{padding:8px 12px;border:1px solid var(--b2);border-radius:10px;background:var(--card);max-width:100%;transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.8}input[readonly]{background:var(--bg)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--a);box-shadow:0 0 0 3px var(--a2)}
input[type=checkbox]:not(.sw),input[type=radio]{accent-color:var(--a);width:16px;height:16px;padding:0;vertical-align:middle}
label{display:inline-flex;align-items:center;gap:8px;cursor:pointer}
button{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 14px;border:1px solid transparent;border-radius:10px;background:var(--a);color:var(--af);
 font-weight:550;line-height:1.3;cursor:pointer;transition:background .15s,filter .15s,box-shadow .15s,transform .1s}
button:hover{filter:brightness(1.07)}button:active{transform:translateY(1px)}
button.s{background:var(--card);color:var(--fg);border-color:var(--b2);box-shadow:var(--sh)}button.s:hover{background:var(--hov);filter:none}
button.r{background:var(--bad);color:#fff}button:disabled{opacity:.5;cursor:not-allowed;filter:none;transform:none}
/* toggle switch: <input type=checkbox class="sw"> */
input.sw{appearance:none;-webkit-appearance:none;position:relative;width:38px;height:22px;flex:none;margin:0;padding:0;border:0;border-radius:999px;background:var(--b2);cursor:pointer;vertical-align:middle;transition:background .18s}
input.sw::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 3px #0003;transition:transform .18s}
input.sw:checked{background:var(--a)}input.sw:checked::after{transform:translateX(16px)}input.sw:focus{box-shadow:0 0 0 3px var(--a2)}
/* segmented control */
.seg{display:inline-flex;gap:2px;padding:3px;background:var(--hov);border:1px solid var(--b);border-radius:11px;white-space:nowrap}
.seg button{padding:6px 12px;border:0;border-radius:8px;background:none;color:var(--mut);font-weight:500;box-shadow:none}
.seg button:hover{color:var(--fg);filter:none}.seg button.on{background:var(--card);color:var(--fg);font-weight:600;box-shadow:var(--sh),0 0 0 1px var(--b)}
/* ---- surfaces ---- */
.card{background:var(--card);border:1px solid var(--b);border-radius:var(--r);padding:16px 20px;margin:16px 0;box-shadow:var(--sh)}
.card>h2:first-child{margin-top:0}
table{display:block;overflow-x:auto;background:var(--card);border:1px solid var(--b);border-radius:var(--r);box-shadow:var(--sh)}
table>tbody{display:table;width:100%;border-collapse:separate;border-spacing:0}
.card table{border:0;box-shadow:none;border-radius:0;background:none}
td,th{padding:10px 14px;border-bottom:1px solid var(--b);text-align:left;vertical-align:middle;font-size:14px}
th{font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);background:var(--bg);white-space:nowrap}.card th{background:none}
tr:last-child>td{border-bottom:0}tr:hover>td{background:var(--hov)}.card tr:hover>td{background:none}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
.out{white-space:pre-wrap;font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
/* status pill */
.tag{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;background:var(--hov);color:var(--mut);font-size:12px;font-weight:550;line-height:1.5;white-space:nowrap;vertical-align:middle}
.tag::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.tag.ok{background:var(--okb);color:var(--ok)}.tag.mid{background:var(--warnb);color:var(--warn)}.tag.bad{background:var(--badb);color:var(--bad)}
#sp .tag{white-space:normal}
/* progress bar + ring */
.bar{display:inline-block;vertical-align:middle;width:140px;height:6px;border-radius:999px;background:var(--track);overflow:hidden}
.bar i{display:block;height:100%;border-radius:inherit;background:var(--a)}
.ring{width:56px;height:56px;flex:none;transform:rotate(-90deg)}.ring circle{fill:none;stroke-width:4}.ring .rt{stroke:var(--track)}.ring .rf{stroke-linecap:round}
/* stat tiles */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}.grid .card{margin:0}
.stat .sh{display:flex;justify-content:space-between;align-items:center;gap:12px}.stat .st{min-width:0}
.stat .k{font-size:12.5px;font-weight:550;color:var(--mut)}.stat .sub{margin-top:8px;font-size:13px;color:var(--mut)}
.stat .bar{display:block;width:100%;margin:4px 0 8px}.kv{display:flex;justify-content:space-between;gap:8px}
.big{font-size:28px;font-weight:650;letter-spacing:-.02em;line-height:1.2}
.spark{display:block;width:100%;height:44px;margin:10px 0 6px;overflow:visible}
.legend{display:flex;gap:12px;font-size:12px}.legend i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
/* overview: quick actions + problems */
.qa{margin:4px 0 20px}.qa button .ic{width:18px;height:18px}
.ph{display:flex;align-items:center;gap:10px}.ph h2{margin:0}
.pl{list-style:none;margin:8px 0 0;padding:0}.pl li{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--b)}.pl li:first-child{border-top:0}
.pl .dot{margin-top:8px}.pl b{display:block}.pl span{font-size:13px;color:var(--mut)}
.okbox{display:flex;align-items:center;gap:12px}.okbox>.ic{width:28px;height:28px;color:var(--ok)}
/* models */
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}.mgrid>.card{margin:0;display:flex;flex-direction:column}
.mh{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.mh .st{min-width:0}.mn{font-size:16px;font-weight:650;overflow-wrap:anywhere}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 16px;margin:16px 0 8px}.facts span{display:block;font-size:12px;color:var(--mut)}.facts b{font-size:14px;font-weight:600}
.mb{margin:auto 0 0;padding-top:12px}button.s.rd{color:var(--bad)}
.foot{margin:28px 2px 8px;padding-top:14px;border-top:1px solid var(--b);color:var(--mut);font-size:12px;text-align:center}.foot a{color:var(--mut);text-decoration:underline}
.tres{margin:8px 0 0;padding:10px 12px;border:1px solid var(--b);border-radius:10px;background:var(--bg);font-size:13.5px}.ans{margin-top:4px;white-space:pre-wrap;max-height:140px;overflow:auto}
.dlb{display:flex;align-items:center;gap:12px;margin:12px 0 4px}.dlb .bar{flex:1;width:auto;height:8px}#prog>.card{margin:0 0 12px}
tr.rec>td{background:var(--a2)}.note{margin:12px 0;padding:10px 12px;border-radius:10px;background:var(--badb);color:var(--bad)}
.mgrid .bar{display:block;width:100%;margin:6px 0 8px}.mgrid .kv{font-size:13px;color:var(--mut)}
.note.ok{background:var(--okb);color:var(--ok)}.note.mid{background:var(--warnb);color:var(--warn)}
a.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border:1px solid var(--b2);border-radius:10px;background:var(--card);color:var(--fg);font-weight:550;text-decoration:none;box-shadow:var(--sh)}a.btn:hover{background:var(--hov)}
.ao{gap:12px}.ao input[type=range]{flex:1 1 180px;accent-color:var(--a);padding:0;border:0;box-shadow:none;min-width:140px}.aov{min-width:4em}
/* logs */
.lg select{min-width:210px}.lg input[type=search]{flex:1 1 200px}
.logbox{height:min(65vh,640px);margin-top:8px;padding:12px 14px;overflow:auto;background:var(--card);border:1px solid var(--b);border-radius:var(--r);box-shadow:var(--sh);
 font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.logbox .le{color:var(--bad)}.logbox .lw{color:var(--warn)}mark{background:var(--warnb);color:inherit;border-radius:3px;padding:0 1px}
/* settings forms */
.fl{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:12px 0}
.fl label{display:flex;flex-direction:column;align-items:stretch;gap:4px;font-size:12.5px;color:var(--mut);cursor:default}.fl input,.fl textarea{width:100%;color:var(--fg);font-size:14.5px}
#outc:has(#out:empty){display:none}tr.unk>td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
#more,#sheet{display:none}
.card.warnc{border-color:var(--badb);box-shadow:var(--sh),inset 3px 0 0 var(--bad)}
/* empty state + skeleton */
.empty{display:flex;flex-direction:column;align-items:center;gap:4px;padding:28px 16px;text-align:center;color:var(--mut);border:1px dashed var(--b2);border-radius:var(--r)}
.empty .ic{width:28px;height:28px;margin-bottom:4px}.empty b{color:var(--fg)}.empty span{font-size:13px;max-width:440px}
.sk{display:block;height:14px;margin:12px 0;border-radius:7px;background:linear-gradient(90deg,var(--hov) 25%,var(--track) 50%,var(--hov) 75%);background-size:200% 100%;animation:sk 1.3s linear infinite}
.sk.h{height:40px}.sk.w6{width:60%}@keyframes sk{to{background-position:-200% 0}}
/* toasts */
#toasts{position:fixed;right:24px;bottom:24px;z-index:60;display:flex;flex-direction:column;align-items:flex-end;gap:8px;pointer-events:none}
.toast{pointer-events:auto;display:flex;align-items:flex-start;gap:10px;max-width:420px;padding:12px 12px 12px 14px;border:1px solid var(--b);border-radius:12px;background:var(--card);color:var(--fg);
 box-shadow:var(--sh2);font-size:14px;white-space:pre-wrap;animation:tin .22s ease-out;transition:opacity .2s,transform .2s}
.toast>.ic{color:var(--ok);margin-top:1px}.toast.err>.ic{color:var(--bad)}.toast .tx{flex:1;min-width:0;overflow-wrap:anywhere}
.toast .ib{width:24px;height:24px;margin:-2px -2px 0 0}.toast .ib .ic{width:16px;height:16px}.toast.out{opacity:0;transform:translateY(6px)}
@keyframes tin{from{opacity:0;transform:translateY(8px)}}
/* dialog */
dialog{width:min(440px,calc(100vw - 32px));padding:24px;border:1px solid var(--b);border-radius:16px;background:var(--card);color:var(--fg);box-shadow:var(--sh2)}
dialog[open]{animation:din .18s ease-out}@keyframes din{from{opacity:0;transform:translateY(8px) scale(.98)}}
dialog::backdrop{background:#0b0d1266}
.pwrb{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 18px;padding:12px 16px;border-radius:12px;background:var(--badb);color:var(--bad);font-weight:700;font-size:1.15em}
.pwrb[hidden]{display:none}.pwrb span{flex:1;min-width:160px}.pwrb button{font-size:1em;padding:12px 28px;min-height:48px}
dialog h3{margin:0 0 6px;font-size:17px;font-weight:650}dialog p{margin:0 0 20px;color:var(--mut);white-space:pre-wrap}#dlgi{width:100%;margin:0 0 20px}
.acts{display:flex;flex-direction:row-reverse;flex-wrap:wrap;gap:8px}
/* login */
#app{display:none}
#gate{display:none;width:calc(100% - 32px);max-width:400px;margin:14vh auto 0;padding:32px;background:var(--card);border:1px solid var(--b);border-radius:16px;box-shadow:var(--sh2)}
#gate .logo{margin-bottom:16px}#gate h1{font-size:21px;margin-bottom:4px}#gate input{width:100%;margin:6px 0}#gate button{width:100%;margin-top:8px;padding:10px}.err{color:var(--bad)}
/* ---- phones ---- */
@media(max-width:720px){
 #side{position:fixed;top:auto;left:0;right:0;bottom:0;width:auto;height:auto;flex-direction:row;padding:4px 4px calc(4px + env(safe-area-inset-bottom));border-right:0;border-top:1px solid var(--b);
  background:var(--bgt);-webkit-backdrop-filter:saturate(1.5) blur(12px);backdrop-filter:saturate(1.5) blur(12px)}
 .brand,#col{display:none}
 nav{flex-direction:row;gap:0;overflow-x:auto;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
 .nb{flex:1 0 64px;width:auto;flex-direction:column;justify-content:center;gap:3px;padding:6px 2px;font-size:11px}
 .nb.on{background:none}.nb.on .ic{stroke-width:2.3}.nb .l{max-width:100%;overflow:hidden;text-overflow:ellipsis}
 #pc{position:absolute;top:2px;left:calc(50% + 6px);margin:0}
 #top{min-height:56px;padding:8px 16px;gap:8px}.chip{padding:5px 10px;font-size:12.5px}
 #lo .l{display:none}#lo{padding:8px 10px}
 .content{padding:16px 16px calc(96px + env(safe-area-inset-bottom))}
 h1{font-size:21px}h2{margin-top:24px}.card{padding:14px 16px}
 .grid{grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}.big{font-size:22px}.ring{width:44px;height:44px}
 td,th{padding:9px 10px}input,select,textarea{font-size:16px}
 .row>input:not([type=checkbox]):not([type=number]){flex:1 1 160px;min-width:0}
 .facts{grid-template-columns:repeat(2,minmax(0,1fr))}.mgrid{grid-template-columns:1fr}
 .hp{display:none}
 nav{overflow:visible}nav .nb.x{display:none}#more{display:flex}#side>nav{flex:4 1 0}#side>#more{flex:1 1 0}
 #sheet:not([hidden]){display:block;position:fixed;inset:0;z-index:40;background:#0b0d1240}
 .sheet-in{position:absolute;left:8px;right:8px;bottom:calc(72px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(4,1fr);gap:4px;padding:8px;
  background:var(--card);border:1px solid var(--b);border-radius:16px;box-shadow:var(--sh2);animation:tin .18s ease-out}
 .sheet-in .nb{flex-direction:column;gap:4px;padding:10px 2px;font-size:11.5px;width:auto}
 .qa{display:grid;grid-template-columns:1fr 1fr}.qa button{justify-content:flex-start;text-align:left;padding:8px 10px;font-size:13.5px}
 #toasts{left:12px;right:12px;bottom:calc(84px + env(safe-area-inset-bottom));align-items:stretch}.toast{max-width:none}
 #gate{margin-top:8vh;padding:24px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="i-server" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01"/></symbol>
<symbol id="i-home" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8M21 3v5h-5"/></symbol>
<symbol id="i-backup" viewBox="0 0 24 24"><path d="M21 8v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8M1.5 3h21v5h-21zM10 12h4"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-disk" viewBox="0 0 24 24"><path d="M22 12H2M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1zM6 16h.01M10 16h.01"/></symbol>
<symbol id="i-logs" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8"/></symbol>
<symbol id="i-more" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></symbol>
<symbol id="i-models" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.3 7 12 12l8.7-5M12 22V12"/></symbol>
<symbol id="i-gpu" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></symbol>
<symbol id="i-net" viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0 1 14 0M1.5 9a16 16 0 0 1 21 0M8.5 16.1a6 6 0 0 1 7 0M12 20h.01"/></symbol>
<symbol id="i-appr" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></symbol>
<symbol id="i-health" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></symbol>
<symbol id="i-addpc" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4M12 7v6M9 10h6"/></symbol>
<symbol id="i-set" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></symbol>
<symbol id="i-auto" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></symbol>
<symbol id="i-out" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></symbol>
<symbol id="i-coll" viewBox="0 0 24 24"><path d="m11 17-5-5 5-5M18 17l-5-5 5-5"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 7v6M12 17h.01"/></symbol>
<symbol id="i-power" viewBox="0 0 24 24"><path d="M12 2v10M18.4 6.6a9 9 0 1 1-12.8 0"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
<symbol id="i-inbox" viewBox="0 0 24 24"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/></symbol>
</svg>
<div id="gate"><div class="logo"><svg class="ic"><use href="#i-server"/></svg></div><h1>Home AI Control Panel</h1><p id="gmsg" class="m"></p>
<input id="gcode" placeholder="Setup code (shown at the end of the installer)" style="display:none" autocomplete="off">
<input id="gpw" type="password" placeholder="Admin password" autocomplete="current-password"><input id="gpw2" type="password" placeholder="Type it again" style="display:none" autocomplete="new-password">
<button id="gbtn">Log in</button><p id="gerr" class="err"></p></div>
<div id="app"><div class="shell">
<aside id="side">
<div class="brand"><div class="logo"><svg class="ic"><use href="#i-server"/></svg></div><span>Home AI</span></div>
<nav aria-label="Sections">
<button class="nb on" data-t="ov" title="Overview"><svg class="ic"><use href="#i-home"/></svg><span class="l">Overview</span></button>
<button class="nb" data-t="models" title="Models"><svg class="ic"><use href="#i-models"/></svg><span class="l">Models</span></button>
<button class="nb" data-t="gpus" title="GPUs"><svg class="ic"><use href="#i-gpu"/></svg><span class="l">GPUs</span></button>
<button class="nb x netonly" data-t="net" title="Network"><svg class="ic"><use href="#i-net"/></svg><span class="l">Network</span></button>
<button class="nb netonly" data-t="appr" title="Approvals"><svg class="ic"><use href="#i-appr"/></svg><span class="l">Approvals</span><span id="pc"></span></button>
<button class="nb x" data-t="drives" title="Health"><svg class="ic"><use href="#i-health"/></svg><span class="l">Health</span></button>
<button class="nb x" data-t="storage" title="Storage"><svg class="ic"><use href="#i-disk"/></svg><span class="l">Storage</span></button>
<button class="nb x" data-t="logs" title="Logs"><svg class="ic"><use href="#i-logs"/></svg><span class="l">Logs</span></button>
<button class="nb x" data-t="addpc" title="Add a PC"><svg class="ic"><use href="#i-addpc"/></svg><span class="l">Add a PC</span></button>
<button class="nb x" data-t="set" title="Settings"><svg class="ic"><use href="#i-set"/></svg><span class="l">Settings</span></button>
</nav>
<button class="nb" id="more" aria-haspopup="true" aria-expanded="false"><svg class="ic"><use href="#i-more"/></svg><span class="l">More</span></button>
<button class="nb" id="col" title="Collapse or expand the menu"><svg class="ic"><use href="#i-coll"/></svg><span class="l">Collapse</span></button>
</aside>
<div id="main">
<header id="top">
<div class="srv"><b>Home AI server</b><small id="upd">Connecting...</small></div>
<button class="chip" id="schip" onclick="go('ov')" title="Open Overview"><span class="dot" id="sdot"></span><span id="stxt">Checking...</span></button>
<button class="ib" id="pwr" title="Restart or shut down the server" aria-label="Restart or shut down the server"><svg class="ic"><use href="#i-power"/></svg></button>
<button class="ib" id="thm"></button>
<button class="s" id="lo" title="Log out"><svg class="ic"><use href="#i-out"/></svg><span class="l">Log out</span></button>
</header>
<main class="content">
<div id="pwrb" class="pwrb" role="status" hidden><svg class="ic"><use href="#i-power"/></svg><span id="pwrt"></span><button class="r" id="pwrc">Cancel</button></div>
<h1 id="ptitle">Overview</h1>

<section id="ov" class="tab on">
<div class="row qa"><button class="s" id="qa_stop"><svg class="ic"><use href="#i-stop"/></svg>Stop all models</button>
<button class="s" id="qa_pcs"><svg class="ic"><use href="#i-refresh"/></svg>Refresh GPU PCs</button>
<button class="s" id="qa_bk"><svg class="ic"><use href="#i-backup"/></svg>Run backup now</button>
<button class="s" id="qa_scout"><svg class="ic"><use href="#i-search"/></svg>Check for new models</button></div>
<div id="ovp" data-sk></div>
<div class="grid" id="ovg" data-sk></div>
</section>

<section id="models" class="tab">
<h2>Installed models</h2>
<div id="inst" class="mgrid" data-sk></div>
<h2>Downloads</h2><div id="prog" data-sk></div><div id="done"></div>
<h2>Suggested new models <small id="sugt"></small></h2><div class="m">Checked every week on Hugging Face. Only models that fit your GPUs. Nothing downloads until you press Add.</div>
<div id="sug" class="mgrid" style="margin-top:12px" data-sk></div><div class="row"><button class="s" id="scoutb">Check now</button></div>
<h2>Search Hugging Face</h2><div class="row"><input id="q" placeholder="e.g. Qwen3.5 9B, gemma 4, coder" size="40" aria-label="Search Hugging Face"><button id="qb">Search</button></div>
<div id="res"></div><div id="files"></div>
</section>

<section id="gpus" class="tab">
<div class="m" style="max-width:800px"><b>Use</b> = every model may use it. <b>Standby</b> = kept ready, only used by big models that need all GPUs (or when nothing else is free).
<b>Off</b> = never used (for example while you play games on that PC). Changing it stops the running models; they start again with the new choice on the next message.
Image generation (ComfyUI) is separate and isn't affected.</div>
<div id="gl" class="mgrid" style="margin-top:16px" data-sk></div>
<h2>Switch off automatically while a PC is busy</h2>
<div class="card"><div class="m">When a GPU PC's own programs (a game, a video...) use its GPU more than the limit, the AI stops using that GPU.
It gets it back once the PC has stayed below the limit for the minutes you choose.</div>
<div class="row ao"><label for="gap">Limit</label><input id="gap" type="range" min="0" max="100" step="5"><b id="gapv" class="aov"></b>
<label for="gab">Give it back after</label><span><input id="gab" type="number" min="1" max="120" style="width:5.5em"> <span class="m">min</span></span><button id="gas">Save</button></div></div>
<h2>Running now</h2><div id="run" data-sk></div><div class="row"><button class="s" id="unl">Stop all running models</button></div>
</section>

<section id="net" class="tab">
<h2>Devices on your network</h2><div class="row"><span id="nsum" class="m"></span><button class="s" id="scanb">Scan now</button></div>
<div class="m">Tick <b>Mine</b> for devices you recognise and type a name for each (e.g. "CCTV front gate"), then press Save. New unknown devices trigger an alert.
<b>Block</b> asks for the change; it only happens after you approve it in the Approvals tab or in Telegram.</div>
<div id="dev" data-sk></div>
<h2>Security alerts</h2><div id="al" data-sk></div>
</section>

<section id="appr" class="tab">
<h2>Changes waiting for you</h2><div class="m">Changes to your network (blocking a device or a website) are only <i>asked for</i>: by you in the Network tab, or by a script or AI agent you allowed (docs/network.md). Nothing happens until you approve here or in Telegram.</div>
<div id="rq" data-sk></div>
</section>

<section id="drives" class="tab">
<h2>Server status <small id="stime"></small> <span id="sv"></span></h2>
<div id="sp" style="margin-bottom:8px"></div>
<div class="grid" id="sg" data-sk></div>
<h2>Services</h2><div id="ss" data-sk></div>
<h2>Drive health (SMART) <small id="dtime"></small></h2>
<div class="m">Live health data straight from each disk. <b>Good</b> = no warning signs. <b>Warning</b> = early signs of wear (keep backups current, plan a replacement).
<b>Failing</b> = copy important data off now. The server also checks disks and services every 10 minutes and sends a Telegram alert if something new goes wrong.</div>
<div class="row"><button class="s" id="drf">Refresh drives now</button><label class="m"><input type="checkbox" class="sw" id="dlive" checked> Live (every 0.5 s; SMART is re-read from the disks every 5 s)</label></div>
<div id="dl" class="mgrid" style="margin-top:12px" data-sk></div>
</section>
<section id="storage" class="tab">
<div id="sdisk" class="mgrid" style="margin-top:16px" data-sk></div>
<h2>What is where</h2><div id="sfold" data-sk></div>
<h2>Biggest files</h2><div class="m">Files over 200 MB on the server (backups not included).</div><div id="sbig" style="margin-top:12px" data-sk></div>
<h2>Backups</h2><div class="m">Every night the server backs up its settings and the chats (Open WebUI). The last 7 days are kept.
Models are not backed up because they can be downloaded again.</div>
<div id="sbk" style="margin-top:12px" data-sk></div>
<div class="row"><button class="s" id="sbkb">Run backup now</button><button class="s" id="srf">Measure again</button><small class="m" id="stt"></small></div>

<h2>Where downloaded models and backups are kept</h2>
<div class="card"><div id="stcur" data-sk></div>
<div class="m" style="margin-top:8px">The starter models from the installer stay on the system disk. Models you download later, and the backups, can move to another drive or a RAID.</div></div>
<h2>Use another drive or a RAID <small style="color:var(--bad)">erases the drives you tick</small></h2>
<div class="card"><div class="m">Tick the drive(s) to use. The system disk, drives that are in use and drives already in a RAID cannot be ticked.</div>
<div id="stdrv" style="margin-top:8px" data-sk></div><div id="straid"></div>
<div class="fl"><label>How<select id="stmode"><option value="drive">One drive (no RAID)</option><option value="1">RAID 1 - mirror, safe (2+ drives)</option>
<option value="5">RAID 5 - safe, more space (3+ drives)</option><option value="10">RAID 10 - fast and safe (4+ drives)</option><option value="0">RAID 0 - fastest, NOT safe (2+ drives)</option></select></label></div>
<label class="m"><input type="checkbox" class="sw" id="stbk" checked> Also keep the nightly backups there</label>
<div id="stplan" class="m" style="margin-top:8px"></div>
<div class="row"><button class="s" id="stprev">Show what will happen (changes nothing)</button></div>
<div class="note"><b>Everything on the ticked drives will be erased.</b> Downloaded models are moved there automatically.</div>
<div class="fl"><label>Type ERASE<input id="sterase" autocomplete="off"></label><label>Your admin password<input id="stpw" type="password" autocomplete="current-password"></label></div>
<div class="row"><button class="r" id="strun">Erase and set up</button></div></div>
<h2>Or use a folder <small>erases nothing</small></h2>
<div class="card"><div class="m">A folder on a drive that is already set up, e.g. <b>/mnt/data/ai-models</b>. To go back to the system disk use <b>/opt/ai/models</b> (backups: <b>/opt/ai/backups</b>).</div>
<div class="fl"><label>Folder<input id="stfolder" placeholder="/mnt/data/ai-models"></label></div>
<label class="m"><input type="checkbox" class="sw" id="stacc"> Use it even if it is a Windows-format, network or temporary drive (slower, or missing after a restart)</label>
<div class="row"><button id="stfgo">Move the models there</button><button class="s" id="stbgo">Move the backups there</button><button class="s" id="stfprev">Preview</button></div>
<pre id="stpo" class="out m" style="display:none"></pre></div>
<h2>Progress <small id="stjt"></small></h2>
<pre id="stlog" class="logbox" style="height:auto;max-height:50vh">No storage job yet.</pre>
<div class="m"><b>RAID 1/5/10</b> keep working when one drive dies. <b>RAID 0</b> loses everything if any drive dies (fine for models - they can be
downloaded again - not for backups). RAID is not a backup. The same tool on the server: <b>sudo ai-storage</b>. Details: docs/storage.md</div>
</section>

<section id="logs" class="tab">
<div class="row lg"><select id="lsrc" aria-label="Which log">
<option value="panel">Control panel</option><option value="llama">AI models (llama-swap)</option><option value="downloads">Model downloads</option>
<option value="starter">Starter model download</option><option value="health">Health check</option><option value="backup">Backups</option><option value="network">Network watch</option></select>
<input id="lq" type="search" placeholder="Filter lines (e.g. error)" aria-label="Filter lines">
<label class="m"><input type="checkbox" class="sw" id="lauto" checked> Auto-scroll</label><button class="s" id="lpause">Pause</button></div>
<small class="m" id="lmeta"></small>
<div id="lbox" class="logbox" role="log" data-sk></div>
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
<ol><li>Open this link in its browser and save the file:<br><a id="winlink" href="#" download>add-this-pc.bat</a><div class="row"><input id="winurl" readonly size="40" aria-label="Link to add-this-pc.bat"><button class="s" onclick="cp('winurl')">Copy link</button></div></li>
<li>Double-click <b>add-this-pc.bat</b> and click <b>Yes</b> when Windows asks for permission.</li>
<li>Wait until it shows <b>WORKER-OK</b> (the first time it downloads ~300 MB).</li>
<li>Come back to this tab - the new GPU appears in the list below within a minute.</li></ol>
<div class="m">To remove it later: on that PC open Task Scheduler and delete the tasks <b>AI GPU Worker</b> and <b>AI GPU Status</b> (or just set it to <b>Off</b> in the GPUs tab).</div></div>
<div class="card"><h2>Ubuntu / Linux PC</h2><div class="m">On the PC you want to add, open a Terminal and paste this command (it asks for that PC's password):</div>
<div class="row"><input id="linuxcmd" readonly size="62" style="font-family:ui-monospace,monospace"><button class="s" onclick="cp('linuxcmd')">Copy</button></div>
<div class="m">If it says an NVIDIA driver was installed, restart that PC and run the same command again.
To remove it later: <code>sudo systemctl disable --now ai-gpu-worker ai-gpu-status</code></div></div>
<div class="card"><h2>GPU PCs the server can see</h2><div id="wl" data-sk></div>
<div class="row"><button class="s" id="rsc">Look for new PCs now</button></div>
<div class="m">Found it? Go to the <b>GPUs</b> tab to give it a name and choose <b>Use</b>, <b>Standby</b> or <b>Off</b>. Models pick it up the next time they start
(or press "Stop all running models" there).</div></div>
<div class="card"><h2>PCs with extra access</h2>
<div class="m">Give a PC the <b>AI API</b> (port 8080, for VS Code and scripts) or the <b>image-model share</b> (ComfyUI reads the models over the network). The firewall only opens these ports to the PCs listed here; removing a PC closes them again.</div>
<div id="pcl" data-sk></div>
<div class="fl" style="margin-top:12px;display:flex;flex-wrap:wrap;gap:10px;align-items:end">
 <label>PC address<input id="pcip" placeholder="192.168.x.x" size="14"></label>
 <label>Name (optional)<input id="pcname" placeholder="e.g. studio-pc" size="14"></label>
 <label class="sw" style="align-self:center"><input type="checkbox" id="pcapi"><span>AI API</span></label>
 <label class="sw" style="align-self:center"><input type="checkbox" id="pcshare"><span>Image share</span></label>
 <button id="pcadd">Add PC</button></div></div>
<div class="card"><h2>PC on a different network?</h2><div class="m">PCs on this home network are found automatically. Only if a PC is on another subnet (e.g. 192.168.1.x), type its address here, one per line (ip or ip:port):</div>
<textarea id="wx" rows="3" cols="30"></textarea>
<div class="row"><button id="wxs">Save</button></div></div>
</section>

<section id="set" class="tab">
<h2>Alerts and Telegram</h2>
<div class="mgrid">
<div class="card"><div class="mh"><div class="st"><b class="mn">Alerts bot</b><div><small>Disk and service problems, new devices, Approve / Reject buttons</small></div></div><span id="tgst"></span></div>
<ol class="m"><li>In Telegram open <b>@BotFather</b>, send /newbot, name it (e.g. "Home AI Alerts") and copy the token.</li>
<li>Open <b>@userinfobot</b> to get your user ID (a number).</li><li>Open your new bot and press <b>Start</b>. Only taps from your own account count.</li></ol>
<div class="fl"><label>Bot token<input id="tgt" type="password" autocomplete="off"></label><label>Your user ID<input id="tgc" inputmode="numeric" placeholder="123456789"></label></div>
<div class="row mb"><button id="tgs">Save</button><button class="s" id="tgtest">Send test</button></div></div>
</div>

<h2>Who can use the AI</h2>
<div class="card"><div class="m">Everyone at home chats in <b>Open WebUI</b> at <a id="chatlink" target="_blank" rel="noopener"></a> (phones and tablets too; add it to the home screen).
Family members get their own accounts there (Open WebUI &rarr; Admin Panel &rarr; Users). This control panel works from any home device after logging in.</div>
<div class="m" style="margin-top:12px"><b>Direct AI access</b> (VS Code, Chrome AI extensions, scripts) is only allowed for the devices listed here, one IP address per line (plus ADMIN_PC_IP from the installer, if set).</div>
<div class="fl"><label>Allowed devices<textarea id="apid" rows="4"></textarea></label></div>
<div class="row"><button id="apis">Save allowed devices</button></div></div>

<h2>Security</h2>
<div class="mgrid">
<div class="card"><b class="mn">Admin password</b><div><small>Changing it logs out every other device.</small></div>
<div class="fl"><label>Current password<input id="opw" type="password" autocomplete="current-password"></label><label>New password (10+ characters)<input id="npw" type="password" autocomplete="new-password"></label></div>
<div class="row mb"><button id="cpw">Change password</button></div></div>
<div class="card"><b class="mn">Logged-in devices</b><div><small>Devices stay logged in for 30 days.</small></div>
<div id="sl" style="margin-top:8px" data-sk></div><div class="row mb"><button class="s rd" id="loa">Log out all devices</button></div></div>
</div>

<div id="netset" hidden>
<h2>Home network helpers</h2>
<div class="m">Used by the network watch. It can then read your router's device list and, only after you approve, block a website or a device.</div>
<div class="mgrid" style="margin-top:12px">
<div class="card"><b class="mn">AdGuard Home</b><div class="m" style="margin-top:8px">The address you open AdGuard with, and your AdGuard login. Then websites can be blocked, and the "generic" router type can block a device (by IP).</div>
<div class="fl"><label>Address<input id="agu" placeholder="http://192.168.1.2"></label><label>Username<input id="agn"></label><label>Password<input id="agp" type="password" autocomplete="off"></label></div>
<div class="row mb"><button id="ags">Save</button><button class="s" id="agt">Test</button></div></div>
<div class="card"><div class="mh"><div class="st"><b class="mn">Router</b><div><small>Lets the network watch list your devices and, only after your approval, block one</small></div></div><span id="rtpill"></span></div>
<div id="rtc"></div>
<div class="fl"><label>Router type<select id="rtb" aria-label="Router type"></select></label></div>
<div id="rtinfo"></div>
<div class="fl" id="rtf"></div>
<div class="row mb"><button id="rts">Save</button><button class="s" id="rtt">Test connection</button><button class="s" id="rtd">Auto-detect</button></div>
<div id="rtres"></div><small class="m" id="rtdoc"></small></div>
</div>
<div class="card"><b class="mn">Public key of the AI server</b><div class="m" style="margin-top:8px">For routers read over SSH (OpenWrt, pfSense, ASUS, DD-WRT): add this key on the router.</div><div style="margin-top:8px"><code class="pk"></code></div></div>
<div class="card"><b class="mn">Phones, tablets and TVs</b><div class="m" style="margin-top:8px">They can't be logged into, but they are protected through the network: every device is listed in the Network tab,
new ones raise an alert, AdGuard shows what each one connects to, and an approved block cuts a device off at the router.</div></div>
</div>

<h2>Power</h2>
<div class="card"><div class="m">Restart or shut down the server. Both wait 10 seconds, so you can still cancel. Everything starts again by itself after a restart.</div>
<div id="pwrs" class="m" style="margin-top:8px"></div>
<div class="row"><button class="s" id="pwrr">Restart server</button><button class="r" id="pwrh">Shut down server</button></div></div>

<h2>About</h2>
<div class="card" id="about" data-sk></div>
<div class="card" id="outc"><small class="m">Last result</small><div id="out" class="out"></div></div>
</section>
<footer class="foot">Home AI Cluster &middot; Made by <a href="https://shafakat12.github.io/shafakat/" target="_blank" rel="noopener">Shafakat Sayeedi</a> &middot; v{{VERSION}}</footer>
</main></div></div>
<div id="sheet" hidden><div class="sheet-in" id="sheetin" role="menu" aria-label="More sections"></div></div></div>
<div id="toasts" aria-live="polite"></div>
<dialog id="dlg"><form method="dialog"><h3 id="dlgt"></h3><p id="dlgx"></p><input id="dlgi" autocomplete="off">
<div class="acts"><button value="1" id="dlgok">OK</button><button value="2" id="dlgalt" class="r" style="display:none"></button><button value="0" class="s">Cancel</button></div></form></dialog>
<script>
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* fast live refresh: redraw only when something changed, never overlap requests, pause while clicking */
(()=>{const ih=Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML'),tc=Object.getOwnPropertyDescriptor(Node.prototype,'textContent');
 Object.defineProperty(Element.prototype,'innerHTML',{get(){return ih.get.call(this)},set(v){if(this._lastH===v)return;this._lastH=v;ih.set.call(this,v)},configurable:true});
 Object.defineProperty(Node.prototype,'textContent',{get(){return tc.get.call(this)},set(v){this._lastH=undefined;tc.set.call(this,v)},configurable:true});})();
let PDOWN=0;document.addEventListener('pointerdown',()=>PDOWN=Date.now(),true);document.addEventListener('pointerup',()=>PDOWN=Date.now(),true);
const BUSY={};function tick(key,fn){if(BUSY[key]||Date.now()-PDOWN<700)return;BUSY[key]=1;Promise.resolve().then(fn).catch(()=>{}).finally(()=>BUSY[key]=0)}
const REFRESH_MS=500;let LASTOK=0,HOST='';
const api=(p,b)=>fetch('api/'+p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Panel':'1'},body:JSON.stringify(b)}).then(r=>{if(r.status==401&&!['login','setup'].includes(p))gate();else LASTOK=Date.now();return r.json()});
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{v==null?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}}};
/* ---------- components ---------- */
const icon=n=>`<svg class="ic"><use href="#i-${n}"/></svg>`;
const SK='<i class="sk h"></i><i class="sk"></i><i class="sk w6"></i>';
document.querySelectorAll('[data-sk]').forEach(e=>e.innerHTML=SK);
function empty(title,hint){return `<div class="empty">${icon('inbox')}<b>${esc(title)}</b>${hint?'<span>'+esc(hint)+'</span>':''}</div>`}
const lvl=p=>p>=90?'var(--bad)':p>=75?'var(--warn)':'var(--a)';
function ring(p){p=Math.max(0,Math.min(100,+p||0));const c=2*Math.PI*16;
 return `<svg class="ring" viewBox="0 0 40 40" aria-hidden="true"><circle class="rt" cx="20" cy="20" r="16"/><circle class="rf" cx="20" cy="20" r="16" style="stroke:${lvl(p)}" stroke-dasharray="${(c*p/100).toFixed(1)} ${c.toFixed(1)}"/></svg>`}
function tile(k,v,sub,p){return `<div class="card stat"><div class="sh"><div class="st"><div class="k">${k}</div>${v!==''?`<div class="big">${v}</div>`:''}</div>${p!==undefined?ring(p):''}</div>${sub?`<div class="sub">${sub}</div>`:''}</div>`}
function toast(t,kind){const box=$('#toasts'),e=document.createElement('div'),err=kind=='err'||/^Error/.test(t);
 e.className='toast'+(err?' err':'');e.setAttribute('role',err?'alert':'status');
 e.innerHTML=`${icon(err?'alert':'check')}<div class="tx"></div><button class="ib" aria-label="Close">${icon('x')}</button>`;e.querySelector('.tx').textContent=t;
 const close=()=>{e.classList.add('out');setTimeout(()=>e.remove(),220)};e.querySelector('button').onclick=close;
 box.appendChild(e);while(box.children.length>4)box.firstChild.remove();setTimeout(close,Math.min(12000,4000+t.length*40))}
/* nice confirm/prompt: ask({title,text,ok,no,danger,input,match,alt}) -> true/false, or the typed text/null when input is given.
   match: OK stays disabled until the typed text equals it. alt: a second action button; choosing it returns 'alt'. */
function ask(o){const d=$('#dlg'),i=$('#dlgi'),b=$('#dlgok'),a=$('#dlgalt'),inp=o.input!=null;
 $('#dlgt').textContent=o.title;$('#dlgx').textContent=o.text||'';$('#dlgx').style.display=o.text?'':'none';
 i.style.display=inp?'':'none';i.value=inp?o.input:'';b.textContent=o.ok||'OK';$('#dlg .acts .s').textContent=o.no||'Cancel';b.className=o.danger?'r':'';
 a.style.display=o.alt?'':'none';a.textContent=o.alt||'';
 b.disabled=!!o.match&&i.value.trim()!==o.match;i.oninput=o.match?()=>{b.disabled=i.value.trim()!==o.match}:null;d.returnValue='';d.showModal();
 if(inp){i.focus();i.select()}else b.focus();
 return new Promise(res=>d.onclose=()=>{const y=d.returnValue=='1';b.disabled=false;res(inp?(y?i.value:null):d.returnValue=='2'?'alt':y)})}
async function act(p,b,after){const r=await api(p,b);const t=r.error?('Error: '+r.error):(r.output||'Done');toast(t,r.error?'err':'');$('#out').textContent=t;if(after)after();return r}
/* ---------- layout: tabs, theme, sidebar, top bar ---------- */
let TAB='models';
function show(t){const b=document.querySelector(`nav .nb[data-t="${t}"]`);if(!b)return false;TAB=t;
 document.querySelectorAll('.nb[data-t],#more,.tab').forEach(x=>{x.classList.remove('on');x.removeAttribute('aria-current')});
 b.classList.add('on');b.setAttribute('aria-current','page');document.querySelectorAll(`#sheet .nb[data-t="${t}"]`).forEach(x=>x.classList.add('on'));if(b.classList.contains('x'))$('#more').classList.add('on');$('#'+t).classList.add('on');$('#ptitle').textContent=b.querySelector('.l').textContent;
 store.set('tab',t);b.scrollIntoView({block:'nearest',inline:'nearest'});return true}
function go(t){if(show(t)){window.scrollTo(0,0);load()}}
document.querySelectorAll('nav .nb').forEach(b=>b.onclick=()=>go(b.dataset.t));
document.querySelectorAll('nav .nb.x').forEach(b=>{const c=b.cloneNode(true);c.setAttribute('role','menuitem');c.onclick=()=>{closeSheet();go(c.dataset.t)};$('#sheetin').appendChild(c)});
function closeSheet(){$('#sheet').hidden=true;$('#more').setAttribute('aria-expanded','false')}
$('#more').onclick=()=>{const sh=$('#sheet');sh.hidden=!sh.hidden;$('#more').setAttribute('aria-expanded',String(!sh.hidden))};
$('#sheet').onclick=e=>{if(e.target.id=='sheet')closeSheet()};document.addEventListener('keydown',e=>{if(e.key=='Escape')closeSheet()});
show(store.get('tab')||'ov');
const THEMES=['auto','light','dark'],TNAME={auto:'follows your system',light:'light',dark:'dark'};let TH=store.get('theme')||'auto';
function theme(t){const h=document.documentElement;if(t=='auto')delete h.dataset.theme;else h.dataset.theme=t;store.set('theme',t=='auto'?null:t);
 const b=$('#thm');b.innerHTML=icon(t=='auto'?'auto':t=='light'?'sun':'moon');b.title='Theme: '+TNAME[t]+' (click to change)';b.setAttribute('aria-label',b.title)}
theme(TH);$('#thm').onclick=()=>theme(TH=THEMES[(THEMES.indexOf(TH)+1)%3]);
$('#col').onclick=()=>store.set('mini',document.documentElement.classList.toggle('mini')?'1':null);
function setTop(r){if(!r||!r.problems)return;sample(r);HOST=r.host||'';const P=findProblems(r,OVD.gp,OVD.dr,OVD.pg),n=P.length;
 $('#sdot').className='dot '+(!n?'ok':P.some(p=>p.lv=='bad')?'bad':'mid');$('#stxt').textContent=n?n+(n>1?' issues':' issue'):'All good';
 $('#schip').title=n?'Needs attention (click for Overview):\n'+P.map(p=>p.t).join('\n'):'Everything is running normally';return P}
setInterval(()=>{if(!LASTOK)return;const age=(Date.now()-LASTOK)/1000,t=new Date(LASTOK).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}),e=$('#upd');
 e.textContent=(HOST?HOST+' · ':'')+(age>15?'no answer since '+t:'updated '+t);e.classList.toggle('stale',age>15);
 if(age>15){$('#sdot').className='dot mid';$('#stxt').textContent='Not connected'}},1000);
setInterval(()=>{if($('#app').style.display=='block'&&TAB!='drives'&&TAB!='ov')tick('top',()=>fetchOv(30000).then(()=>setTop(OVD.st)))},5000);
/* ---------- power: restart / shut down the server (10 second delay, can be cancelled; banner on every tab) ---------- */
let PWR=null,PWRT=0;
async function pwrGet(){try{const r=await api('power');if(r&&!r.error){PWR=r;PWRT=Date.now()}}catch(e){}pwrShow();return PWR}
function pwrLeft(){const s=PWR&&PWR.scheduled;return s?Math.max(0,Math.ceil(s.at-PWR.now-(Date.now()-PWRT)/1000)):0}
function pwrShow(){const s=PWR&&PWR.scheduled;$('#pwrb').hidden=!s;let t='';
 if(s){const l=pwrLeft();t=(s.mode=='restart'?'Restarting':'Shutting down')+(l>0?' in '+l+'…':' now…');$('#pwrt').textContent=t}
 $('#pwrs').textContent=!PWR?'':PWR.old?'Needs update: run  sudo bash install.sh 60-control-panel  on the server again.':
  !PWR.can?'Not set up yet: run  sudo bash install.sh 60-control-panel  on the server once.':s?t:'Nothing scheduled.';
 $('#pwrr').disabled=$('#pwrh').disabled=!PWR||!PWR.can||!!s}
function pwrList(i){const L=[];
 if(i.downloads.length)L.push('Model downloads in progress: '+i.downloads.join(', ')+' (you can continue them afterwards)');
 if(i.models.length)L.push('Loaded models: '+i.models.join(', ')+' (answers being written are cut off)');
 if(i.backup)L.push('The backup that is running now');
 return L.length?'This interrupts:\n- '+L.join('\n- '):'Nothing is running that would be interrupted.'}
async function pwrAsk(mode){const p=await pwrGet();if(!p)return toast('Could not reach the server.','err');
 if(!p.can)return toast((p.old?'Needs update':'Not set up yet')+': run  sudo bash install.sh 60-control-panel  on the server.','err');
 if(p.scheduled)return toast('A '+p.scheduled.mode+' is already scheduled. Cancel it first.','err');
 const list=pwrList(p.interrupts);let confirm='';
 if(mode=='restart'){if(!await ask({title:'Restart the server?',ok:'Restart in 10 seconds',
   text:'The server restarts in 10 seconds and is back after a few minutes. Meanwhile the chat, the AI and this panel are unavailable. You can cancel during those 10 seconds.\n\n'+list}))return}
 else{confirm=await ask({title:'Shut down the server?',ok:'Shut down in 10 seconds',danger:true,input:'',match:p.hostname,
   text:'The server switches off in 10 seconds. The chat, the AI and this panel stop until it is switched on again. You can cancel during those 10 seconds.\n\nThe panel cannot turn the server back on. You will need the power button.\n\n'+list+'\n\nTo confirm, type the name of the server: '+p.hostname});
  if(confirm==null)return}
 const r=await act('power',{action:mode,confirm});if(r.power){PWR=r.power;PWRT=Date.now();pwrShow()}}
async function pwrCancel(){const r=await act('power',{action:'cancel'});if(r.power){PWR=r.power;PWRT=Date.now();pwrShow()}}
async function pwrMenu(){const p=await pwrGet();
 if(p&&p.scheduled){if(await ask({title:'Cancel the '+p.scheduled.mode+'?',text:$('#pwrt').textContent,ok:'Cancel it',no:'Keep it'}))pwrCancel();return}
 const c=await ask({title:'Restart or shut down?',text:'Both wait 10 seconds, so you can still cancel.',ok:'Restart...',alt:'Shut down...',no:'Close'});
 if(c===true)pwrAsk('restart');else if(c==='alt')pwrAsk('shutdown')}
$('#pwr').onclick=pwrMenu;$('#pwrc').onclick=pwrCancel;$('#pwrr').onclick=()=>pwrAsk('restart');$('#pwrh').onclick=()=>pwrAsk('shutdown');
setInterval(()=>{if($('#app').style.display!='block')return;if(PWR&&PWR.scheduled)tick('pwr',pwrGet);pwrShow()},1000);   // every second while a timer runs
setInterval(()=>{if($('#app').style.display=='block')tick('pwr',pwrGet)},5000);
/* ---------- overview ---------- */
const HIST={},SPAN=300000;   /* last 5 min of [time,value] per series, kept in this browser only */
function sample(r){if(!r.cpu||r.cpu.pct==null)return;const t=Date.now(),push=(k,v)=>{const h=HIST[k]=HIST[k]||[];
  if(v==null||(h.length&&t-h[h.length-1][0]<900))return;h.push([t,+v]);while(h.length&&t-h[0][0]>SPAN)h.shift()};
 push('cpu',r.cpu.pct);(r.gpu||[]).forEach((g,i)=>push('g'+i,g.util));push('rx',r.net.rx_mbs);push('tx',r.net.tx_mbs)}
function spark(series,max){const W=300,H=44,now=Date.now(),t0=Math.min(now-30000,...series.map(([k])=>(HIST[k]||[[now]])[0][0])),span=now-t0;let out='';
 for(const[k,col]of series){const h=HIST[k]||[];if(h.length<2)continue;
  const pts=h.map(([t,v])=>((t-t0)/span*W).toFixed(1)+' '+(H-2-Math.min(v,max)/max*(H-6)).toFixed(1)),line='M'+pts.join('L');
  out+=`<path d="${line}L${pts[pts.length-1].split(' ')[0]} ${H}L${pts[0].split(' ')[0]} ${H}Z" style="fill:${col};opacity:.1"/><path d="${line}" style="stroke:${col}" fill="none" stroke-width="1.8" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`}
 return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${out}</svg>`}
const maxOf=(...ks)=>Math.max(1,...ks.flatMap(k=>(HIST[k]||[]).map(x=>x[1])))*1.15;
function ago(ms){const m=Math.round(ms/60000);return m<1?'just now':m<60?m+' min ago':m<2880?Math.round(m/60)+' h ago':Math.round(m/1440)+' days ago'}
/* last backup time: the unix timestamp if the server sends it, else the text ("... UTC" read as UTC) */
function whenBk(b){if(b&&b.last_ts)return new Date(b.last_ts*1000);const s=(b&&b.last)||'',m=/(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)/.exec(s);
 return m?new Date(m[1]+'T'+m[2]+(/ UTC$/.test(s)?'Z':'')):null}
const diskName=d=>(d.kind?d.kind+' ':'')+'('+d.mount+')';
/* everything that needs attention, each with what to do */
function findProblems(st,gp,dr,pg){const P=[],add=(lv,t,h)=>P.push({lv,t,h});
 for(const s of st.services||[])if(s.state!='active')add('bad',`${s.label} is ${s.state}`,`It normally restarts by itself. If it stays like this, run on the server: <code>sudo systemctl restart ${esc(s.unit)}</code>`);
 for(const e of st.endpoints||[])if(!e.ok)add('bad',`${e.label} is not answering`,'Its service may be starting or stopped. See Services on the Health tab.');
 for(const r of st.raids||[])if(r.degraded)add('bad',r.problem||('RAID '+(r.label||r.name)+' has a problem'),'See the Storage tab and docs/storage.md.');
 for(const d of st.disks||[])if(d.pct>=90)add('bad',`${diskName(d)} is ${d.pct}% full`,'Remove models you no longer use (Models tab) or old downloads.');
 if(st.mem&&st.mem.pct>=92)add('mid',`Memory almost full (${st.mem.pct}%)`,'Stop the models you are not using ("Stop all models" above).');
 for(const t of st.temps||[])if(t.c>=88)add('mid',`${t.name} is hot (${t.c} °C)`,'Check the fans and that air can flow around the server.');
 for(const g of st.gpu||[])if(g.temp>=85)add('mid',`GPU is hot (${g.temp} °C)`,'Check the GPU fans. Long heavy use heats it up.');
 const b=st.backup||{},bt=whenBk(b);
 if(b.running){}else if(b.last&&b.result&&b.result!='success')add('bad','The last backup failed','Press "Run backup now" to try again. If it fails again, check that the HDD is mounted.');
 else if(!bt)add('mid','No backup yet','Press "Run backup now". Backups also run every night.');
 else if(Date.now()-bt>36*3600e3)add('mid',`Last backup was ${ago(Date.now()-bt)}`,'The nightly backup did not run. Press "Run backup now" and check the Nightly backup service.');
 for(const d of (dr&&dr.disks)||[]){if(d.health=='bad')add('bad',`Disk ${d.name} (${d.model}) is failing`,'Copy important data off it now and replace it.');
  else if(d.health=='warning')add('mid',`Disk ${d.name} (${d.model}) shows early wear`,'Keep backups current and plan a replacement.');
  if(d.temp>=55)add('mid',`Disk ${d.name} is hot (${d.temp} °C)`,'Check airflow around the disks.')}
 for(const g of (gp&&gp.gpus)||[]){if(g.key=='server')continue;
  if(!g.online&&g.mode!='off')add('mid',`GPU PC "${g.name}" is offline`,'Turn that PC on, or set it to Off on the GPUs tab (or Forget it if it is gone).');
  else if(g.slow)add('mid',`GPU PC "${g.name}" is slow to answer`,'Check its network cable or Wi-Fi.')}
 for(const x of pg||[])if(x.state=='failed')add('bad',`Download of ${x.name} stopped`,x.can_resume?'Press Continue under Downloads on the Models tab.':'Remove it and add it again on the Models tab.');
 return P.sort((a,b)=>(a.lv=='bad'?0:1)-(b.lv=='bad'?0:1))}
const OVD={},OVT={};
async function every(k,ms,fn){if(k in OVD&&Date.now()-(OVT[k]||0)<ms)return;OVT[k]=Date.now();try{const r=await fn();if(!r.error)OVD[k]=r}catch(e){}}
const fetchOv=slow=>Promise.all([every('st',0,()=>api('status')),every('gp',slow,()=>api('gpus')),every('pg',slow,()=>api('progress')),every('dr',slow*3,()=>api('drives'))]);
async function loadOv(){await fetchOv(5000);
 const st=OVD.st;if(!st)return;const P=setTop(st);
 $('#ovp').innerHTML=P.length?`<div class="card warnc"><div class="ph"><h2>Needs your attention</h2><span class="tag ${P.some(p=>p.lv=='bad')?'bad':'mid'}">${P.length}</span></div><ul class="pl">${
   P.map(p=>`<li><span class="dot ${p.lv}"></span><div><b>${esc(p.t)}</b><span>${p.h}</span></div></li>`).join('')}</ul></div>`
  :`<div class="card okbox">${icon('check')}<div><b>All good</b><div class="m">Nothing needs your attention right now.</div></div></div>`;
 const c=st.cpu,m=st.mem,n=st.net,A='var(--a)';
 let h=tile('CPU',c.pct==null?'&hellip;':esc(c.pct)+'%',spark([['cpu',A]],100)+`Load ${esc(c.load.join(' / '))} &middot; ${esc(c.cores)} threads`)
  +tile('Memory (RAM)',esc(m.pct)+'%',`${GB(m.total-m.avail)} of ${GB(m.total)} GB used`,m.pct);
 (st.gpu||[]).forEach((g,i)=>{const vp=g.mem_total?Math.round(100*g.mem_used/g.mem_total):0;
  h+=tile('Server GPU &middot; '+esc(g.name.replace(/^NVIDIA (GeForce )?/,'')),esc(g.util??'-')+'%',spark([['g'+i,A]],100)
   +`<div class="kv"><span>VRAM</span><span>${(g.mem_used/1024).toFixed(1)} / ${(g.mem_total/1024).toFixed(1)} GB</span></div>${bar(vp)}${g.temp!=null?tmp(g.temp):''} ${g.power!=null?esc(g.power)+' W':''}`)});
 h+=tile(`Network${n.speed>0?' &middot; '+(n.speed>=1000?(n.speed/1000)+' Gbit/s':n.speed+' Mbit/s'):''}`,`${n.rx_mbs==null?'&hellip;':esc(n.rx_mbs)}<small> MB/s down</small>`,
   spark([['rx',A],['tx','var(--ok)']],maxOf('rx','tx'))+`<div class="legend"><span><i style="background:var(--a)"></i>down</span><span><i style="background:var(--ok)"></i>up ${n.tx_mbs==null?'':esc(n.tx_mbs)+' MB/s'}</span></div>`)
  +tile('Storage','',(st.disks||[]).map(d=>`<div class="kv"><span>${esc(diskName(d))}</span><span>${esc(d.used_gb)} / ${esc(d.total_gb)} GB</span></div>${bar(d.pct)}`).join('')||'No disks found');
 const ws=((OVD.gp&&OVD.gp.gpus)||[]).filter(g=>g.key!='server');
 const cnt={on:ws.filter(g=>g.online&&!g.busy&&g.mode!='off').length,use:ws.filter(g=>g.busy).length,off:ws.filter(g=>g.mode=='off').length,down:ws.filter(g=>!g.online&&g.mode!='off').length};
 h+=tile('GPU PCs',ws.length?`${cnt.on+cnt.use}<small> of ${ws.length} online</small>`:'0',ws.length?
   `<div class="row" style="margin:6px 0 8px">${cnt.use?`<span class="tag ok">${cnt.use} in use</span>`:''}${cnt.on?`<span class="tag ok">${cnt.on} ready</span>`:''}${cnt.off?`<span class="tag">${cnt.off} off</span>`:''}${cnt.down?`<span class="tag bad">${cnt.down} offline</span>`:''}</div>`
   +ws.map(g=>`<div class="kv"><span>${esc(g.name)}</span><span>${g.auto_off?'off (PC busy)':!g.online?(g.mode=='off'?'off':'offline'):g.busy?'in use':g.mode=='off'?'off':g.mode}</span></div>`).join(''):'None added yet. See the Add a PC tab.')
  +tile('Models loaded now',String(st.running.length),st.running.length?st.running.map(x=>`<div style="margin:4px 0"><span class="tag ok">${esc(x.model)}</span> ${esc(x.state)}</div>`).join(''):'None. They start by themselves on the first chat message.');
 const b=st.backup||{},bt=whenBk(b);
 h+=tile('Up time',dur(st.uptime_s),`<div class="kv"><span>Last backup</span><span>${b.running?'<span class="tag mid">running now</span>':bt?ago(Date.now()-bt):'not yet'}</span></div>
   ${bt&&!b.running?`<small>${esc(b.last)}</small> ${b.result&&b.result!='success'?'<span class="tag bad">'+esc(b.result)+'</span>':'<span class="tag ok">ok</span>'}`:''}`);
 $('#ovg').innerHTML=h}
$('#qa_stop').onclick=async()=>{if(await ask({title:'Stop all models?',text:'This frees all GPU memory. Chats that are answering right now stop. Models start again on the next message.',ok:'Stop all',danger:true}))act('unload',{})};
$('#qa_pcs').onclick=()=>act('rescan',{},()=>setTimeout(()=>OVT.gp=0,500));
$('#qa_bk').onclick=()=>act('backup',{});
$('#qa_scout').onclick=()=>act('scout',{});
/* ---------- models ---------- */
let SR=[],FL=[],FMETA={},REPO='',SG=[],INST=[],USE={running:[],last_used:{}};const TEST={},LOADING={};
const RUNS={solo:'Server GPU',pool:'All GPUs',remote:'GPU PCs'},CTX=32768;
const F=(k,v,t)=>`<div${t?` title="${t}"`:''}><span>${k}</span><b>${v}</b></div>`;
const kfmt=n=>n>=1024?Math.round(n/1024)+'k':String(n);
function nameOf(repo){return repo.split('/')[1].replace(/-?gguf$/i,'').replace(/^(google|qwen|openai|meta|mistralai)_/i,'').toLowerCase().replace(/[^a-z0-9._-]/g,'-').slice(0,40)}
const quantOf=p=>(/((?:UD-)?I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4)/i.exec(p)||[p.replace(/\.gguf$/i,'')])[0];
/* GPU memory in GB that models can use now: the server GPU alone, and all online GPUs that are not Off */
function caps(){const g=(OVD.gp&&OVD.gp.gpus)||[],s=g.find(x=>x.key=='server');
 return{solo:s&&s.total?s.total/1024*.92:0,pool:g.filter(x=>x.online&&x.mode!='off'&&x.total).reduce((a,x)=>a+x.total/1024*.92,0)}}
/* memory a model needs: the file + chat memory for a 32k-token conversation (about 16 KB per billion parameters per token) + 0.5 GB.
   shortcut: rough rule of thumb, real use depends on the model's layout; good enough for a fits / doesn't fit hint */
function need(gb,pB){const kv=(pB||gb*1.7)*0.016*CTX/1024;return{kv:+kv.toFixed(1),total:+(gb+kv+.5).toFixed(1)}}
function fitOf(gb,pB){const c=caps(),n=need(gb,pB).total;
 if(!c.pool)return gb<=INFO.solo_gb?['ok','Fits: server GPU','solo']:gb<=INFO.pool_gb?['ok','Fits: all GPUs','pool']:gb<=INFO.pool_gb*1.6?['mid','Partly in RAM (slow)','pool']:['bad','Too big','pool'];
 return n<=c.solo?['ok','Fits: server GPU','solo']:n<=c.pool?['ok','Fits: all GPUs','pool']:n<=c.pool*1.6?['mid','Partly in RAM (slow)','pool']:['bad','Too big','pool']}
let INFO={solo_gb:4.5,pool_gb:13,network:false};
const PREF=['Q4_K_M','Q4_K_XL','Q4_K_S','IQ4_XS','Q4_0','Q5_K_M','Q3_K_M'];
function recIdx(files,pB){const cap=caps().pool||INFO.pool_gb,fits=f=>need(f.gb,pB).total<=cap;
 for(const q of PREF){const j=files.findIndex(f=>quantOf(f.path).toUpperCase().endsWith(q)&&fits(f));if(j>=0)return j}
 let best=-1;files.forEach((f,j)=>{if(fits(f))best=j});return best}
/* search + pick a size */
$('#qb').onclick=search;$('#q').onkeydown=e=>{if(e.key=='Enter')search()};
async function search(){const q=$('#q').value.trim();if(!q)return;$('#res').innerHTML=SK;$('#files').innerHTML='';
 const r=await api('search?q='+encodeURIComponent(q));if(r.error){$('#res').innerHTML=empty('Search failed',r.error);return}SR=r;
 $('#res').innerHTML=r.length?'<table><tr><th>Model</th><th>Downloads</th><th></th></tr>'+r.map((x,i)=>`<tr><td><b>${esc(x.id.split('/')[1])}</b><br><small>${esc(x.id.split('/')[0])}</small></td><td>${(+x.downloads||0).toLocaleString()}</td><td style="text-align:right"><button class="s" onclick="files(${i})">Show sizes</button></td></tr>`).join('')+'</table>'
  :empty('Nothing found','Try a shorter name, e.g. "qwen3 8b".')}
async function files(i){REPO=SR[i].id;$('#files').innerHTML=SK;
 const[r]=await Promise.all([api('files?repo='+encodeURIComponent(REPO)),every('gp',30000,()=>api('gpus'))]);
 if(r.error){$('#files').innerHTML=empty('Could not read this model',r.error);return}FL=r.files;FMETA=r;drawFiles();$('#files').scrollIntoView({behavior:'smooth',block:'start'})}
function drawFiles(){const r=FMETA,pB=r.params_b,c=caps(),rec=recIdx(FL,pB),G=v=>v.toFixed(1);
 const anyFits=FL.some(f=>fitOf(f.gb,pB)[0]=='ok');
 $('#files').innerHTML=`<div class="card"><h2>${esc(REPO)}</h2>
 <div class="row"><span class="tag ${r.vision?'ok':''}">Vision: ${r.vision?'yes':'no'}</span>${pB?`<span class="tag">${pB} billion parameters</span>`:''}${r.ctx_max?`<span class="tag">Max context ${kfmt(r.ctx_max)}</span>`:''}</div>
 <div class="m">${c.pool?`Your GPU memory now: server GPU ${G(c.solo)} GB, all GPUs together ${G(c.pool)} GB (online and not Off). `:''}<b>Needs</b> = the file + about ${need(0,pB||FL[0]&&FL[0].gb*1.7).kv} GB chat memory for a 32k-token conversation + 0.5 GB.
 Smaller files are faster and a bit less smart.</div>
 ${anyFits?'':`<div class="note">None of these fit in your GPU memory. They would run partly on the CPU and be slow.</div>`}
 <div class="row"><label>Name in the menu <input id="nm" value="${esc(nameOf(REPO))}" size="24"></label></div>
 <table><tr><th>Size option</th><th>File</th><th>Needs</th><th>Fits</th><th></th></tr>${FL.map((f,j)=>{const[lv,txt,mode]=fitOf(f.gb,pB),n=need(f.gb,pB);
  return `<tr class="${j==rec?'rec':''}"><td><b>${esc(quantOf(f.path))}</b>${j==rec?' <span class="tag ok">Recommended</span>':''}<br><small>${esc(f.path)}</small></td><td>${f.gb} GB</td><td>~${n.total} GB</td>
  <td><span class="tag ${lv}" title="Needs about ${n.total} GB: file ${f.gb} GB + ${n.kv} GB chat memory + 0.5 GB">${txt}</span></td>
  <td><div class="row" style="margin:0;flex-wrap:nowrap;justify-content:flex-end"><select id="m_${j}" aria-label="Runs on"><option value="pool" ${mode=='pool'?'selected':''}>All GPUs</option><option value="solo" ${mode=='solo'?'selected':''}>Server GPU</option></select><button onclick="addf(${j})">Add</button></div></td></tr>`}).join('')}</table></div>`}
const fresh=()=>{OVT.m_inst=OVT.m_use=OVT.m_sug=0;load()};
function addf(j){act('add',{name:$('#nm').value.trim().toLowerCase(),repo:REPO,quant:FL[j].path,mode:$('#m_'+j).value},fresh)}
function addsug(i){const s=SG[i];act('add',{name:nameOf(s.repo),repo:s.repo,quant:s.file,mode:s.mode},fresh)}
$('#scoutb').onclick=()=>act('scout',{});
/* installed models */
async function ren(i){const n=INST[i].name;const v=await ask({title:'Rename '+n,text:'Use letters, numbers, dash, dot or underscore.',input:n,ok:'Rename'});if(v&&v.trim()&&v.trim()!=n)act('rename',{name:n,new:v.trim()},fresh)}
async function del(i){const n=INST[i].name;if(await ask({title:'Remove '+n+'?',text:'Its file is deleted from the server. You can download it again later.',ok:'Remove',danger:true}))act('remove',{name:n},fresh)}
async function mtest(i){const n=INST[i].name;TEST[n]={busy:1};drawInst();const r=await api('model_test',{name:n});TEST[n]=r.error?{error:r.error}:r;drawInst()}
function mload(i){const n=INST[i].name;LOADING[n]=Date.now();drawInst();act('model_load',{name:n},()=>OVT.m_use=0)}
async function munload(i){const n=INST[i].name;if(await ask({title:'Unload '+n+'?',text:'Its GPU memory is freed. A chat that is answering with it right now stops. It starts again on the next message.',ok:'Unload'}))act('model_unload',{name:n},()=>OVT.m_use=0)}
function mcard(x,i){const run=USE.running.find(r=>r.model==x.name),lu=USE.last_used[x.name],t=TEST[x.name],ld=!run&&LOADING[x.name]&&Date.now()-LOADING[x.name]<300000;
 const pill=run?(run.state=='ready'?'<span class="tag ok">Loaded</span>':`<span class="tag mid">${esc(run.state)}</span>`):ld?'<span class="tag mid">Loading&hellip;</span>':'<span class="tag">Not loaded</span>';
 const res=!t?'':t.busy?'<div class="tres m">Testing&hellip; if the model is not loaded yet this can take a minute.</div>'
  :t.error?`<div class="tres err">${esc(t.error)}</div>`
  :`<div class="tres"><div class="m"><b>${esc(t.tps??t.tps_approx??'?')} tokens/s</b>${t.tps?'':' (including loading time)'} &middot; ${esc(t.tokens??'?')} tokens in ${esc(t.secs)} s</div><div class="ans">${esc(t.output)}</div></div>`;
 return `<div class="card"><div class="mh"><div class="st"><b class="mn">${esc(x.name)}</b><div><small>${esc(x.file)}</small></div></div>${pill}</div>
 <div class="facts">${F('Size',x.gb!=null?esc(x.gb)+' GB':'?')}${F('Stored on',esc(x.disk||'?'))}${F('Runs on',esc(RUNS[x.mode]||x.mode||'?'))}
 ${F('Context',x.ctx?kfmt(x.ctx)+' tokens':'default')}${F('Vision',x.vision?'Yes':'No')}${F('Last used',run?'In use now':lu?ago(Date.now()-lu*1000):'No record','From the AI server log (last 30 days)')}</div>${res}
 <div class="row mb"><button class="s" onclick="mtest(${i})" ${t&&t.busy?'disabled':''}>Test</button>${run?`<button class="s" onclick="munload(${i})">Unload</button>`:`<button class="s" onclick="mload(${i})" ${ld?'disabled':''}>Load now</button>`}
 <button class="s" onclick="ren(${i})">Rename</button><button class="s rd" onclick="del(${i})">Remove</button></div></div>`}
function drawInst(){$('#inst').innerHTML=INST.length?INST.map(mcard).join(''):empty('No models yet','Add one from the suggestions or search Hugging Face below.')}
/* downloads */
function left(s){const m=/^(\d+):(\d\d):(\d\d)$/.exec(s||'');if(!m)return'';const h=+m[1],mi=+m[2];return h?`${h} h ${mi} min left`:mi?`${mi} min left`:'less than a minute left'}
function dlRow(x){const n=esc(x.name),pc=Math.max(0,Math.min(100,+x.pct||0)),btn=(t,a,c='s')=>`<button class="${c}" onpointerdown="dl('${a}','${n}')">${t}</button>`,D=x.state=='downloading';
 const st=D?'<span class="tag ok">Downloading</span>':x.state=='paused'?'<span class="tag mid">Paused</span>':'<span class="tag bad">Stopped</span>';
 const info=[x.gb_total?`${esc(x.gb)} of ${esc(x.gb_total)} GB`:'',D?(x.speed_mbs!=null?`<b>${esc(x.speed_mbs)} MB/s</b>`:'starting&hellip;'):'',D?left(x.left):''].filter(Boolean).join(' &middot; ');
 return `<div class="card dl"><div class="mh"><b class="mn">${n}</b>${st}</div><div class="dlb"><span class="bar"><i style="width:${pc}%"></i></span><b>${pc}%</b></div>
 <div class="m">${info}${x.state=='failed'&&x.last?'<br><small>'+esc(x.last)+'</small>':''}</div>
 <div class="row">${D?btn('Pause','dl_pause'):x.can_resume||x.state=='paused'?btn('Continue','dl_resume'):''}${btn('Cancel','dl_cancel','s rd')}</div></div>`}
async function dl(a,n){if(a=='dl_cancel'&&!await ask({title:'Cancel the download of '+n+'?',text:'The partly downloaded file is deleted. You can start again later.',ok:'Cancel download',no:'Keep it',danger:true}))return;act(a,{name:n},fresh)}
async function loadModels(){
 await Promise.all([every('m_sug',30000,()=>api('suggestions')),every('m_inst',3000,()=>api('installed')),every('m_use',2000,()=>api('model_usage')),every('gp',30000,()=>api('gpus')),
  api('progress').then(p=>{if(Array.isArray(p))OVD.m_pg=p}).catch(()=>{})]);
 const s=OVD.m_sug||{};SG=s.items||[];$('#sugt').textContent=s.checked?'(checked '+s.checked+')':'';
 $('#sug').innerHTML=SG.length?SG.map((x,i)=>`<div class="card"><div class="mh"><div class="st"><b class="mn">${esc(nameOf(x.repo))}</b><div><small>${esc(x.repo)}</small></div></div>${x.vision?'<span class="tag ok">Vision</span>':''}</div>
  <div class="facts">${F('Size',esc(x.gb)+' GB')}${F('Runs on',esc(x.fits))}${F('Updated',esc(x.updated))}</div><div><small>${esc(x.file)}</small></div>
  <div class="row mb"><button onclick="addsug(${i})">Add</button></div></div>`).join('')
  :empty('No suggestions yet','The server looks for new models every week. Press "Check now" to look today.');
 INST=OVD.m_inst||[];USE=OVD.m_use||USE;drawInst();
 const p=OVD.m_pg||[],act_=p.filter(x=>x.state!='done'),done=p.filter(x=>x.state=='done');
 $('#prog').innerHTML=act_.length?act_.map(dlRow).join(''):done.length?'<div class="m">No downloads running right now.</div>':empty('No downloads','Downloads you start show up here with their speed and time left.');
 $('#done').innerHTML=done.length?`<div class="card"><div class="k m">Finished recently</div><table>${done.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td class="m">${esc(x.when||'')}</td><td style="text-align:right"><span class="tag ok">Ready</span></td></tr>`).join('')}</table></div>`:''}
/* ---------- GPUs ---------- */
let GP=[];
/* pointer: act on press, so a re-render between press and release can't swallow it; keyboard: act on click */
const pd=js=>`onpointerdown="${js}" onclick="if(!event.detail){${js}}"`;
function gstat(g){return !g.online?(g.mode=='off'?'<span class="tag">Off &middot; offline</span>':'<span class="tag bad">Offline</span>')
 :g.auto_off?'<span class="tag mid">Off automatically</span>':g.busy?'<span class="tag ok">In use</span>':g.slow?'<span class="tag mid">Slow to answer</span>'
 :g.mode=='off'?'<span class="tag">Off</span>':'<span class="tag ok">Online</span>'}
function gcard(g,i){const sv=g.key=='server',G=v=>(v/1024).toFixed(1);
 const mem=g.total?(g.used!=null?`<div class="kv"><span>GPU memory</span><span>${G(g.used)} / ${G(g.total)} GB</span></div>${bar(Math.round(100*g.used/g.total))}`
  :`<div class="kv"><span>GPU memory</span><span>${G(g.total)} GB</span></div>`):'';
 const own=sv?(g.util!=null?F('Busy right now',esc(g.util)+'%'):''):g.reporter===false?F("PC's own GPU use",'no report','This PC does not send its GPU use (step 17 on that PC)')
  :g.pc_pct!=null?F("PC's own GPU use",esc(g.pc_pct)+'%'+(g.pc_top?' &middot; '+esc(g.pc_top):'')):'';
 return `<div class="card"><div class="mh"><div class="st"><b class="mn">${esc(g.name)}</b><div><small>${esc(g.where)}${g.model&&g.model!=g.name?' &middot; '+esc(g.model):''}</small></div></div>${gstat(g)}</div>
 <div style="margin-top:14px">${mem}</div>${own?`<div class="facts" style="grid-template-columns:1fr;margin-top:4px">${own}</div>`:''}
 ${g.auto_off?'<div class="m"><small>Its PC is busy (a game or a video), so the AI is not using it right now. It comes back by itself.</small></div>':''}
 <div class="row mb"><div class="seg" role="group" aria-label="Mode">${['use','standby','off'].map(m=>`<button class="${g.mode==m?'on':''}" aria-pressed="${g.mode==m}" ${pd(`setg(${i},'${m}')`)}>${m[0].toUpperCase()+m.slice(1)}</button>`).join('')}</div>
 <button class="s" onclick="gname(${i})">Rename</button>${!g.online&&!sv?`<button class="s rd" onclick="gforget(${i})">Forget</button>`:''}</div></div>`}
async function loadGpus(){const r=await api('gpus');if(r.error){$('#gl').innerHTML=empty('Could not read the GPUs',r.error);return}GP=r.gpus;OVD.gp=r;
 $('#gl').innerHTML=GP.map(gcard).join('');
 if(r.guard&&![$('#gap'),$('#gab')].includes(document.activeElement)){$('#gap').value=r.guard.pct;$('#gab').value=r.guard.back;gapv()}
 $('#run').innerHTML=r.running.length?`<div class="row">${r.running.map(x=>`<span class="tag ok">${esc(x.model)} &middot; ${esc(x.state)}</span>`).join('')}</div>`
  :empty('No model running','Models start by themselves on the first chat message.')}
function gapv(){const v=+$('#gap').value;$('#gapv').textContent=v?v+' %':'Never'}
$('#gap').oninput=gapv;
async function gforget(i){const g=GP[i];if(await ask({title:'Forget '+g.name+'?',text:g.where+'. Its name and settings are removed. Use this for a PC that is gone or got a new address.',ok:'Forget',danger:true}))act('gpu_forget',{key:g.key},loadGpus)}
async function gname(i){const g=GP[i];const v=await ask({title:'Rename this GPU',text:g.where,input:g.name,ok:'Save'});if(v!=null&&v.trim()!=g.name)act('gpu_name',{key:g.key,name:v},loadGpus)}
function setg(i,m){if(GP[i].mode==m)return;act('gpu_policy',{key:GP[i].key,value:m},()=>setTimeout(loadGpus,1500))}
$('#gas').onclick=()=>act('gpu_guard',{pct:$('#gap').value,back:$('#gab').value},loadGpus);
$('#unl').onclick=async()=>{if(await ask({title:'Stop all models?',text:'This frees all GPU memory. A chat that is answering right now stops. Models start again on the next message.',ok:'Stop all',danger:true}))act('unload',{},()=>setTimeout(loadGpus,1500))};
/* ---------- network ---------- */
let DV=[];
async function loadNet(){const r=await api('network');DV=r.devices;const unk=DV.filter(d=>!d.known).length;
 $('#nsum').textContent=`${DV.length} devices, ${unk} not marked as yours. Last scan: ${r.scanned||'never'}`;
 $('#dev').innerHTML='<table><tr><th>IP</th><th>Name</th><th>Maker</th><th>MAC</th><th>Mine</th><th>Risky services</th><th>Last seen</th><th></th></tr>'+DV.map((d,i)=>
 `<tr class="${d.known?'':'unk'}"><td>${esc(d.ip)}${d.known?'':' <span class="tag mid">new</span>'}</td><td><input id="lb_${i}" value="${esc(d.label||d.name)}" size="16"></td><td>${esc(d.vendor)}</td><td><small>${esc(d.mac)}</small></td>
 <td><input type="checkbox" class="sw" aria-label="Mine" id="kn_${i}" ${d.known?'checked':''}></td><td>${d.risky.map(x=>'<span class="tag bad">'+esc(x)+'</span>').join(' ')}</td><td><small>${esc(d.last_seen)}</small></td>
 <td><div style="display:flex;flex-wrap:wrap;gap:6px;min-width:130px"><button class="s" onclick="lab(${i})">Save</button><button class="s rd" onclick="nreq(${i},1)">Block&hellip;</button><button class="s" onclick="nreq(${i},0)">Unblock&hellip;</button></div></td></tr>`).join('')+'</table>';
 $('#al').innerHTML=r.alerts.length?'<table>'+r.alerts.map(a=>`<tr><td><small>${esc(a.time)}</small></td><td><span class="tag ${a.kind=='info'?'':'bad'}">${esc(a.kind)}</span></td><td>${esc(a.text)}</td></tr>`).join('')+'</table>':empty('No alerts','New unknown devices and risky services show up here.')}
function lab(i){act('label',{mac:DV[i].mac,known:$('#kn_'+i).checked,label:$('#lb_'+i).value},loadNet)}
async function nreq(i,block){const d=DV[i],n=d.label||d.name||d.ip;if(!await ask({title:(block?'Block ':'Unblock ')+n+'?',text:'This only asks for the change. It runs after you approve it in the Approvals tab or in Telegram.',ok:block?'Ask to block':'Ask to unblock',danger:!!block}))return;
 act('net_request',{action:block?'router_block_mac':'router_unblock_mac',mac:d.mac,name:n},()=>go('appr'))}
$('#scanb').onclick=()=>act('scan',{});
/* ---------- approvals ---------- */
let RQ=[];
async function loadAppr(){RQ=await api('requests');
 $('#rq').innerHTML=RQ.length?'<table><tr><th>When</th><th>Change</th><th>Why</th><th>Status</th><th></th></tr>'+RQ.map((r,i)=>
 `<tr><td><small>${esc(r.created)}</small></td><td>${esc(r.text)}</td><td>${esc(r.reason)}</td><td><span class="tag ${r.status=='done'?'ok':r.status=='pending'?'mid':r.status=='failed'?'bad':''}">${esc(r.status)}</span>
 <br><small>${esc(r.result||'')}</small></td><td>${r.status=='pending'?`<button onclick="dec(${i},'approve')">Approve</button> <button class="r" onclick="dec(${i},'reject')">Reject</button>`:''}</td></tr>`).join('')+'</table>':empty('Nothing waiting','When a change is asked for, it shows up here and in Telegram.')}
async function dec(i,d){const r=RQ[i];if(d=='approve'&&!await ask({title:'Approve this change?',text:r.text,ok:'Approve'}))return;act('decide',{id:r.id,decision:d},loadAppr)}
async function badge(){if(!INFO.network)return;try{const r=await api('requests');const n=r.filter(x=>x.status=='pending').length;$('#pc').innerHTML=n?`<span class="badge">${n}</span>`:''}catch(e){}}
/* ---------- settings ---------- */
let SESS=[];
/* "Chrome on Windows" from a browser's user-agent text */
function devName(ua){ua=ua||'';const b=/Edg\//.test(ua)?'Edge':/OPR\//.test(ua)?'Opera':/Firefox\//.test(ua)?'Firefox':/Chrome\//.test(ua)?'Chrome':/Safari\//.test(ua)?'Safari':'';
 const o=/iPhone/.test(ua)?'iPhone':/iPad/.test(ua)?'iPad':/Android/.test(ua)?'Android':/Windows/.test(ua)?'Windows':/Mac OS X/.test(ua)?'Mac':/Linux/.test(ua)?'Linux':'';
 return b&&o?b+' on '+o:b||o||ua.slice(0,40)||'Unknown device'}
async function loadSet(){const[s]=await Promise.all([api('settings'),every('about',60000,()=>api('about'))]);if(s.error){$('#out').textContent=s.error;return}
 const pill=(on,a,b)=>on?`<span class="tag ok">${a}</span>`:`<span class="tag">${b}</span>`;
 document.querySelectorAll('.pk').forEach(e=>e.textContent=s.pubkey||'(created by the network setup step)');
 $('#tgc').value=s.telegram.chat_id;$('#tgt').placeholder=s.telegram.set?'saved (leave empty to keep it)':'paste the token';
 $('#tgst').innerHTML=pill(s.telegram.set,'Connected','Not set');
 $('#agu').value=s.adguard.url;$('#agn').value=s.adguard.user;$('#agp').placeholder=s.adguard.has_pass?'saved (leave empty)':'';
 $('#netset').hidden=!s.network;if(s.network)loadRouter();
 const chat=`http://${location.hostname}:3000`;$('#chatlink').href=chat;$('#chatlink').textContent=chat;
 if(document.activeElement!==$('#apid'))$('#apid').value=(s.api_devices||[]).join('\n');
 SESS=s.sessions||[];
 $('#sl').innerHTML=SESS.length?`<table>${SESS.map((x,i)=>`<tr><td><b>${esc(devName(x.ua))}</b>${x.current?' <span class="tag ok">This device</span>':''}<br><small class="m">${esc(x.ip)} &middot; since ${esc(x.created)}</small></td>
  <td style="text-align:right"><button class="s" onclick="sessEnd(${i})">Log out</button></td></tr>`).join('')}</table>`:empty('No devices','');
 const ab=OVD.about;if(ab)$('#about').innerHTML=`<div class="facts">${F('Server',esc(ab.host))}${F('Address',esc(ab.ip))}${F('Up time',dur(ab.uptime_s))}
  ${Object.entries(ab.versions||{}).map(([k,v])=>F(esc(k),esc(v||'unknown'))).join('')}</div>
  <div class="row"><a class="btn" href="http://${esc(ab.ip)}:3000" target="_blank" rel="noopener">Open the chat (Open WebUI)</a></div>`}
async function sessEnd(i){const x=SESS[i];if(x.current&&!await ask({title:'Log out this device?',text:'You will see the login page.',ok:'Log out'}))return;
 act('session_end',{id:x.id},x.current?gate:loadSet)}
$('#tgs').onclick=()=>act('settings',{section:'telegram',token:$('#tgt').value,chat_id:$('#tgc').value},()=>{$('#tgt').value='';loadSet()});
$('#tgtest').onclick=()=>act('telegram_test',{});
$('#ags').onclick=()=>act('settings',{section:'adguard',url:$('#agu').value,user:$('#agn').value,pass:$('#agp').value},()=>{$('#agp').value='';loadSet()});
$('#agt').onclick=()=>act('test',{what:'adguard'});
/* router: brand drop-down shows only the fields that brand needs; secrets are never shown again ("saved") */
let RTI=null,RTB=null;
async function loadRouter(){const r=await api('router_info');RTI=r;
 if(!r.ok){$('#rtc').innerHTML=`<div class="note mid">${esc(r.error||'Could not read the router settings.')}</div>`;$('#rtb').disabled=true;return}
 $('#rtc').innerHTML='';$('#rtb').disabled=false;
 const sel=$('#rtb');if(!sel.options.length)sel.innerHTML='<option value="auto">Auto-detect</option>'+r.catalog.map(c=>`<option value="${esc(c.brand)}">${esc(c.label)}</option>`).join('');
 const cur=r.catalog.find(c=>c.brand==r.brand);$('#rtpill').innerHTML=cur?`<span class="tag ${r.brand=='generic'?'':'ok'}">${esc(cur.label.split(' (')[0].split(' /')[0])}</span>`:'';
 if(RTB==null){RTB=r.brand;sel.value=RTB;drawRouter()}}
function drawRouter(){const c=RTI&&RTI.catalog.find(x=>x.brand==RTB);if(!c)return;const same=RTB==RTI.brand;
 $('#rtinfo').innerHTML=`<div class="row">${c.tested?'<span class="tag ok">Tested on real hardware</span>':'<span class="tag mid">Not tested on real hardware yet</span>'}
  ${c.caps.includes('clients')?'<span class="tag">Lists devices</span>':''}${c.caps.includes('block')?'<span class="tag">Can block (after your approval)</span>':'<span class="tag">Read-only</span>'}</div>
  <div class="m"><small>${esc(c.note)}</small></div>`;
 $('#rtf').innerHTML=c.fields.map(f=>{const k=f.key.toLowerCase(),v=same?(RTI[k]||''):'';
  if(f.kind=='port')return `<label>${esc(f.label)}<select id="rtx_${k}">${c.ports.map(p=>`<option ${String(p)==String(same?RTI.port:'')?'selected':''}>${p}</option>`).join('')}</select></label>`;
  if(f.kind=='secret')return `<label>${esc(f.label)}<input id="rtx_${k}" type="password" autocomplete="off" placeholder="${same&&RTI.saved[f.key]?'saved (leave empty to keep it)':''}"></label>`;
  return `<label>${esc(f.label)}<input id="rtx_${k}" value="${esc(v)}" placeholder="${f.key=='HOST'?esc(RTI.gateway||'192.168.1.1'):''}"></label>`}).join('');
 $('#rtdoc').textContent=c.doc?'Built from: '+c.doc:'';$('#rtres').innerHTML=''}
async function rdetect(){$('#rtres').innerHTML=SK;const h=$('#rtx_host');const r=await api('router_detect',h&&h.value?{host:h.value}:{});
 if(r.error){$('#rtres').innerHTML=`<div class="note">${esc(r.error)}</div>`;return}
 if(r.suggested){RTB=r.suggested;$('#rtb').value=RTB;drawRouter()}else $('#rtb').value=RTB;
 $('#rtres').innerHTML=`<div class="note mid">${esc(r.output)}<br><small>${(r.evidence||[]).map(esc).join('<br>')}</small></div>`}
$('#rtb').onchange=e=>{if(e.target.value=='auto'){rdetect();return}RTB=e.target.value;drawRouter()};
$('#rtd').onclick=rdetect;
$('#rts').onclick=()=>{const c=RTI.catalog.find(x=>x.brand==RTB),b={brand:RTB};c.fields.forEach(f=>{const e=$('#rtx_'+f.key.toLowerCase());if(e&&e.value)b[f.key.toLowerCase()]=e.value});
 act('router_save',b,()=>{RTB=null;loadRouter()})};
$('#rtt').onclick=async()=>{$('#rtres').innerHTML=SK;const r=await api('router_test',{});
 $('#rtres').innerHTML=r.error&&!r.output?`<div class="note">${esc(r.error)}</div>`:`<div class="note ${r.ok?'ok':''}">${esc(r.output)}</div>`
  +(/certificate changed/.test(r.error||'')?'<div class="row"><button class="s rd" onclick="rforget()">Trust the new certificate</button></div>':'')};
async function rforget(){if(await ask({title:'Trust the new certificate?',text:'Only do this if you replaced the router or changed its certificate yourself. Otherwise something may be listening in.',ok:'Trust it',danger:true}))act('router_forget_pin',{},()=>$('#rtt').click())}
/* ---------- drives ---------- */
let DR=[];
function hrs(h){if(h==null)return'';const y=h/8766;return y>=1?`${h.toLocaleString()} h (${y.toFixed(1)} years)`:`${h.toLocaleString()} h (${Math.round(h/24)} days)`}
async function loadDrives(fresh){const r=await api('drives'+(fresh?'?fresh=1':''));if(r.error){$('#dl').textContent=r.error;return}DR=r.disks||[];
 $('#dtime').textContent='(read '+(r.time||'')+')';
 $('#dl').innerHTML=DR.map((d,i)=>{const hb={good:'<span class="tag ok">Good</span>',warning:'<span class="tag mid">Warning</span>',bad:'<span class="tag bad">Failing</span>'}[d.health]||'<span class="tag">Unknown</span>';
  const t=d.temp!=null?`<span class="tag ${d.temp>=55?'bad':d.temp>=48?'mid':'ok'}">${esc(d.temp)} &deg;C</span>`:'';
  const use=(d.usage||[]).map(u=>`<div class="kv"><span>${esc(u.mount)}</span><span>${esc(u.used_gb)} / ${esc(u.total_gb)} GB</span></div>${bar(u.pct)}`).join('');
  const key=(d.attrs||[]).filter(a=>a.label),all=d.attrs||[];
  const lt=d.last_test?`Last self-test: ${esc(d.last_test.type)}, ${esc(d.last_test.result)}`:'No self-test yet';
  return `<div class="card${d.health=='bad'?' warnc':''}"><div class="mh"><div class="st"><b class="mn">${esc(d.model)}</b><div><small>${esc(d.name)} &middot; ${esc(d.size)} ${esc(d.kind)} &middot; serial ${esc(d.serial)}${d.rpm?' &middot; '+esc(d.rpm)+' rpm':''}</small></div></div>
  <div class="row" style="margin:0;flex-wrap:nowrap">${t}${hb}</div></div>
  ${d.problems&&d.problems.length?`<div class="note${d.health=='bad'?'':' mid'}">${d.problems.map(esc).join('<br>')}</div>`:''}
  <div class="facts">${F('Powered on',hrs(d.hours)||'?')}${d.cycles!=null?F('Power cycles',esc(d.cycles)):''}
  ${key.map(a=>F(esc(a.label),esc(a.raw_str||a.raw),a.value!=null?`Normalised ${a.value} (worst ${a.worst}, limit ${a.thresh})`:'')).join('')}</div>
  <div style="margin-top:8px">${use}</div>
  <details><summary class="m">All ${all.length} SMART values</summary><table><tr><th>ID</th><th>Name</th><th>Raw</th><th>Value / worst / limit</th><th></th></tr>${all.map(a=>`<tr><td>${esc(a.id)}</td><td>${esc(a.name)}</td><td>${esc(a.raw_str||a.raw)}</td><td>${esc(a.value)} / ${esc(a.worst)} / ${esc(a.thresh)}</td><td>${a.failing?'<span class="tag bad">failing</span>':''}</td></tr>`).join('')}</table></details>
  <div class="row mb"><button class="s" onclick="dtest(${i})" ${d.test_running?'disabled':''}>Run quick self-test (2 min)</button><small class="m">${lt}${d.test_running?' &middot; <b>test running: '+esc(d.test_running)+'</b>':''}</small></div></div>`}).join('')||empty('No disks found','The health check could not see any disks.')}
function bar(p){p=Math.max(0,Math.min(100,+p||0));return `<span class="bar"><i style="width:${p}%;background:${lvl(p)}"></i></span>`}
function dur(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+h+'h '+m+'m'}
const GB=b=>(b/1073741824).toFixed(1);
const tmp=c=>`<span class="tag ${c>=88?'bad':c>=75?'mid':'ok'}">${esc(c)} &deg;C</span>`;
async function loadStatus(){const r=await api('status');if(r.error)return;setTop(r);
 $('#stime').textContent='('+r.time+')';
 $('#sv').innerHTML=r.problems.length?'<span class="tag bad">'+r.problems.length+' problem(s)</span>':'<span class="tag ok">All good</span>';
 $('#sp').innerHTML=r.problems.map(p=>'<span class="tag bad">'+esc(p)+'</span>').join(' ');
 const c=r.cpu,m=r.mem,n=r.net;
 let cards=tile(`CPU &middot; ${esc(c.cores)} threads`,c.pct==null?'&hellip;':esc(c.pct)+'%',`Load ${esc(c.load.join(' / '))}<br><small>${esc(c.model)}</small>`,c.pct)
  +tile('Memory (RAM)',esc(m.pct)+'%',`${GB(m.total-m.avail)} / ${GB(m.total)} GB used${m.swap_total?'<br>Swap '+GB(m.swap_used)+' / '+GB(m.swap_total)+' GB':''}`,m.pct);
 for(const g of r.gpu){const vp=g.mem_total?Math.round(100*g.mem_used/g.mem_total):0;
  cards+=tile('GPU &middot; '+esc(g.name),esc(g.util??'-')+'%',`<div class="kv"><span>VRAM</span><span>${(g.mem_used/1024).toFixed(1)} / ${(g.mem_total/1024).toFixed(1)} GB</span></div>${bar(vp)}
   ${g.temp!=null?tmp(g.temp):''} ${g.power!=null?esc(g.power)+' W':''} ${g.fan!=null?'&middot; fan '+esc(g.fan)+'%':''}`,g.util)}
 cards+=tile('Temperatures','',r.temps.map(t=>`<div class="kv"><span>${esc(t.name)}</span>${tmp(t.c)}</div>`).join('<div style="height:4px"></div>')||'No sensors found')
  +tile(`Network &middot; ${esc(n.iface)}${n.speed>0?' &middot; '+(n.speed>=1000?(n.speed/1000)+' Gbit/s':n.speed+' Mbit/s'):''}`,'',
   `<div class="kv"><span>&darr; Download</span><b>${n.rx_mbs==null?'&hellip;':esc(n.rx_mbs)+' MB/s'}</b></div><div class="kv"><span>&uarr; Upload</span><b>${n.tx_mbs==null?'&hellip;':esc(n.tx_mbs)+' MB/s'}</b></div>`)
  +tile('Storage','',r.disks.map(d=>`<div class="kv"><span>${esc(d.mount)}</span><span>${esc(d.used_gb)} / ${esc(d.total_gb)} GB</span></div>${bar(d.pct)}`).join(''))
  +tile('Up time',dur(r.uptime_s),`${esc(r.host)} &middot; kernel ${esc(r.kernel)}<br>Last backup: ${esc(r.backup.last||'not yet')} ${r.backup.result&&r.backup.result!='success'&&r.backup.last?'<span class="tag bad">'+esc(r.backup.result)+'</span>':''}`)
  +tile('Models loaded now','',r.running.length?r.running.map(x=>'<div style="margin:4px 0"><span class="tag ok">'+esc(x.model)+'</span> '+esc(x.state)+'</div>').join(''):'None. They start on the first message.');
 $('#sg').innerHTML=cards;
 SVC=r.services;const anyR=SVC.some(x=>x.can_restart);
 $('#ss').innerHTML='<table><tr><th>Service</th><th>State</th><th></th></tr>'+SVC.map((x,i)=>`<tr><td>${esc(x.label)}<br><small class="m">${esc(x.unit)}</small></td><td><span class="dot ${x.state=='active'?'up':'down'}"></span>${esc(x.state)}</td>
  <td style="text-align:right">${x.can_restart?`<button class="s" onclick="srestart(${i})">Restart</button>`:''}</td></tr>`).join('')+
  r.endpoints.map(e=>`<tr><td>${esc(e.label)} <small class="m">answering?</small></td><td><span class="dot ${e.ok?'up':'down'}"></span>${e.ok?'yes, '+e.ms+' ms':'NO'}</td><td></td></tr>`).join('')+'</table>'
  +(anyR?'':'<div class="m" style="margin-top:8px"><small>Restart buttons appear once the sudo rule from the installer is in place (sudo bash install.sh 60-control-panel).</small></div>')}
let SVC=[];
async function srestart(i){const x=SVC[i];if(await ask({title:'Restart '+x.label+'?',text:x.unit=='llama-swap'?'Running models stop. Chats that are answering right now are cut off.':'It is unavailable for a few seconds.',ok:'Restart'}))act('service_restart',{unit:x.unit},()=>setTimeout(loadStatus,3000))}
function dtest(i){act('drive_test',{name:DR[i].name},()=>setTimeout(()=>loadDrives(true),3000))}
$('#drf').onclick=()=>loadDrives(true);
setInterval(()=>{if(TAB!='drives'||!$('#dlive').checked||$('#app').style.display!='block')return;tick('status',loadStatus);
 if(!document.querySelector('#drives details[open]'))tick('drives',()=>loadDrives(false))},REFRESH_MS);
/* ---------- storage ---------- */
async function loadStore(fresh){if(fresh){OVT.stor=0;$('#stt').textContent='Measuring...'}
 await every('stor',fresh?0:30000,()=>api('disk_usage'+(fresh?'?fresh=1':'')));const s=OVD.stor;if(!s)return;$('#stt').textContent='Measured at '+s.time;
 $('#sdisk').innerHTML=s.disks.map(d=>`<div class="card"><div class="mh"><div class="st"><b class="mn">${esc(d.label)}</b><div><small>${esc(d.mount)}</small></div></div>${d.kind?`<span class="tag">${esc(d.kind)}</span>`:''}</div>
  <div class="big" style="margin-top:12px">${d.pct}%<small class="m" style="font-size:14px;font-weight:500"> used</small></div>${bar(d.pct)}
  <div class="kv"><span>${d.used_gb} of ${d.total_gb} GB used</span><span>${d.total_gb-d.used_gb} GB free</span></div></div>`).join('')||empty('No disks found','');
 const mx=Math.max(1,...s.folders.map(f=>f.gb));
 $('#sfold').innerHTML=`<table><tr><th>Folder</th><th>Disk</th><th>Size</th><th class="hp" style="width:28%"></th></tr>${s.folders.map(f=>`<tr><td><b>${esc(f.name)}</b><br><small>${esc(f.what)} <code>${esc(f.path)}</code></small></td>
  <td>${f.kind?`<span class="tag">${esc(f.kind)}</span>`:''}</td><td style="white-space:nowrap">${f.approx?'about ':''}${f.gb} GB</td><td class="hp"><span class="bar" style="width:100%"><i style="width:${Math.round(100*f.gb/mx)}%"></i></span></td></tr>`).join('')}</table>`;
 $('#sbig').innerHTML=s.biggest.length?`<table><tr><th>File</th><th>Size</th></tr>${s.biggest.map(f=>{const i=f.path.lastIndexOf('/');
  return `<tr><td><b>${esc(f.path.slice(i+1))}</b><br><small>${esc(f.path.slice(0,i))}</small></td><td style="white-space:nowrap">${f.gb} GB</td></tr>`}).join('')}</table>`:empty('No big files','Nothing over 200 MB.');
 $('#sbk').innerHTML=s.backups.length?`<table><tr><th>Backup</th><th>Finished</th><th>Size</th></tr>${s.backups.map((b,i)=>`<tr><td><b>${esc(b.name)}</b>${i==0?' <span class="tag ok">Latest</span>':''}</td>
  <td>${new Date(b.when*1000).toLocaleString()} <small class="m">(${ago(Date.now()-b.when*1000)})</small></td><td>${esc(b.size)}</td></tr>`).join('')}</table>`:empty('No backups yet','Press "Run backup now", or wait for the nightly backup.')}
$('#srf').onclick=()=>loadStore(true);
$('#sbkb').onclick=()=>act('backup',{},()=>setTimeout(()=>loadStore(true),120000));
/* ---------- drives / RAID (ai-storage) ---------- */
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
  $('#stcur').innerHTML=`<div><b>Downloaded models:</b> ${esc(r.models_dir)} <span class="m">${esc(r.models_where)}</span></div><div><b>Backups:</b> ${esc(r.backup_dir)} <span class="m">${esc(r.backup_where)}</span></div>`;
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
 if(!await ask({title:'Really erase '+[...STSEL].join(', ')+'?',text:'Everything on these drives is deleted. This cannot be undone.',ok:'Erase',danger:true}))return;
 const r=await act('storage_run',stbody(stkind(),{confirm:$('#sterase').value.trim(),password:$('#stpw').value}));$('#stpw').value='';if(!r.error){$('#sterase').value='';STSEL.clear()}loadStorage(true)};
$('#stfgo').onclick=()=>act('storage_run',stbody('folder'),()=>loadStorage(true));
$('#stbgo').onclick=()=>act('storage_run',stbody('backups'),()=>loadStorage(true));
setInterval(()=>{if(TAB!='storage'||$('#app').style.display!='block')return;tick('storage',()=>loadStorage(false))},1000);
/* ---------- logs (read-only) ---------- */
let LSRC=store.get('logsrc')||'panel',LPAUSE=false,LLINES=[];$('#lsrc').value=LSRC;
const reEsc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
/* journal lines: show local time, drop the host name and process id */
function tidy(l){const m=/^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:?\d\d)\s+\S+\s+([\w.@-]+)(\[\d+\])?:\s?/.exec(l);if(!m)return l;const d=new Date(m[1]);
 if(isNaN(d))return l;const p=n=>String(n).padStart(2,'0');return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}  ${m[2]}: ${l.slice(m[0].length)}`}
async function loadLogs(){if(LPAUSE)return;await every('log_'+LSRC,2000,()=>api('logs?src='+LSRC));const r=OVD['log_'+LSRC];if(!r||r.src!=LSRC)return;LLINES=r.lines.map(tidy);drawLogs()}
function drawLogs(force){const q=$('#lq').value.trim().toLowerCase(),box=$('#lbox'),L=q?LLINES.filter(l=>l.toLowerCase().includes(q)):LLINES,rq=q&&new RegExp(reEsc(esc(q)),'gi');
 const cls=l=>/\b(error|errors|failed|fail|fatal|traceback|exception)\b/i.test(l)?'le':/\bwarn(ing)?\b/i.test(l)?'lw':'';
 const before=box._lastH;
 box.innerHTML=L.length?L.map(l=>`<div class="${cls(l)}">${rq?esc(l).replace(rq,x=>`<mark>${x}</mark>`):esc(l)}</div>`).join(''):`<div class="m">${q?'No lines match the filter.':'This log is empty.'}</div>`;
 $('#lmeta').textContent=`${L.length} of ${LLINES.length} lines (the last 200)${LPAUSE?' \u00b7 paused':''}`;
 if($('#lauto').checked&&(force||box._lastH!==before))box.scrollTop=box.scrollHeight}
$('#lsrc').onchange=e=>{LSRC=e.target.value;store.set('logsrc',LSRC);LLINES=[];$('#lbox').innerHTML=SK;e.target.blur();loadLogs()};
$('#lq').oninput=()=>drawLogs(true);
$('#lauto').onchange=e=>{e.target.blur();drawLogs(true)};
$('#lpause').onclick=e=>{LPAUSE=!LPAUSE;e.target.textContent=LPAUSE?'Resume':'Pause';drawLogs();if(!LPAUSE)loadLogs()};
/* ---------- add a PC ---------- */
function cp(id){const e=$('#'+id);e.select();let ok=false;try{if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(e.value);ok=true}else ok=document.execCommand('copy')}catch(x){}toast(ok?'Copied':'Select the text and press Ctrl+C')}
async function loadAdd(){const host=location.hostname;const u=`http://${host}:8090/add-this-pc.bat`;$('#winlink').href=u;$('#winurl').value=u;
 $('#linuxcmd').value=`curl -fsSL http://${host}:8090/add-worker-linux.sh | bash`;
 const x=await api('workers_extra');if(document.activeElement!==$('#wx'))$('#wx').value=(x.items||[]).join('\n');
 const r=await api('gpus');const w=(r.gpus||[]).filter(g=>g.key!='server');
 $('#wl').innerHTML=w.length?'<table><tr><th>Name</th><th>Address</th><th>Status</th><th>Memory</th><th>Mode</th></tr>'+w.map(g=>`<tr><td>${esc(g.name)}</td><td>${esc(g.key)}</td><td>${!g.online?'<span class="tag bad">offline</span>':'<span class="tag ok">online</span>'}</td><td>${g.total?(g.total/1024).toFixed(1)+' GB':''}</td><td>${esc(g.mode)}</td></tr>`).join('')+'</table>':empty('No GPU PCs found yet','Follow the steps above on the PC you want to add.');
 const pc=await api('pcs');const rows=Object.entries(pc.pcs||{});
 $('#pcl').innerHTML=rows.length?'<table><tr><th>Name</th><th>Address</th><th>AI API</th><th>Image share</th><th>ComfyUI</th><th></th></tr>'+rows.map(([ip,e])=>{
  const cf=e.share?(pc.comfyui&&pc.comfyui[ip]?'<span class="tag ok">running</span>':'<span class="tag mid">not reachable</span>'):'<span class="m">—</span>';
  return `<tr><td>${esc(e.name||'')}</td><td>${esc(ip)}</td>
   <td><button class="s" onclick="pcTog('${esc(ip)}','api',${!e.api})">${e.api?'on':'off'}</button></td>
   <td><button class="s" onclick="pcTog('${esc(ip)}','share',${!e.share})">${e.share?'on':'off'}</button></td>
   <td>${cf}</td>
   <td style="white-space:nowrap">${pc.can_reserve?`<button class="s" onclick="pcReserve('${esc(ip)}')">Reserve IP</button> `:''}<button class="s rd" onclick="pcDel('${esc(ip)}')">Remove</button></td></tr>`}).join('')+'</table>':empty('No PCs added','Add a PC below to open the AI API or the image share to it.')}
$('#rsc').onclick=()=>act('rescan',{},()=>{$('#wl').innerHTML=SK;loadAdd()});
$('#wxs').onclick=()=>act('workers_extra',{items:$('#wx').value.split(/[\s,]+/).filter(x=>x)},loadAdd);
window.pcTog=(ip,k,v)=>act('pc_set',{ip,[k]:v},loadAdd);
window.pcDel=async ip=>{if(await ask({title:'Remove this PC?',text:'Closes the AI API and image-share ports for '+ip+'. It can be added again later.',ok:'Remove',danger:true}))act('pc_remove',{ip},loadAdd)};
window.pcReserve=async ip=>{if(await ask({title:'Reserve this address in the router?',text:'Asks your router to always give '+ip+' to this PC. A backup of the router’s DHCP settings is saved first. Nothing else on the router changes.',ok:'Reserve it'}))act('pc_reserve',{ip},loadAdd)};
$('#pcadd').onclick=()=>{const ip=$('#pcip').value.trim();if(!ip)return toast('Type the PC address','err');
 act('pc_set',{ip,name:$('#pcname').value.trim(),api:$('#pcapi').checked,share:$('#pcshare').checked},()=>{$('#pcip').value='';$('#pcname').value='';$('#pcapi').checked=$('#pcshare').checked=false;loadAdd()})};
/* ---------- login ---------- */
let SETUP=false;
async function gate(){const a=await api('auth');if(a.logged_in){$('#gate').style.display='none';$('#app').style.display='block';return true}
 $('#app').style.display='none';$('#gate').style.display='block';$('#gerr').textContent='';SETUP=a.setup_needed;
 if(a.setup_needed){$('#gmsg').textContent='First visit: create the admin password for this panel (10+ characters).'+(a.can_setup?'':' Enter the setup code printed at the end of the installer (also in /opt/ai/secrets/setup-code on the server).');
  $('#gcode').style.display=a.can_setup?'none':'block';$('#gpw2').style.display='block';$('#gbtn').textContent='Create password'}
 else{$('#gmsg').textContent='Log in with the admin password.';$('#gcode').style.display='none';$('#gpw2').style.display='none';$('#gbtn').textContent='Log in'}
 $('#gpw').focus();return false}
async function gsubmit(){const pw=$('#gpw').value;if(SETUP&&pw!==$('#gpw2').value){$('#gerr').textContent='The two passwords are different.';return}
 const r=await api(SETUP?'setup':'login',{password:pw,code:$('#gcode').value.trim()});if(r.error){$('#gerr').textContent=r.error;return}$('#gpw').value='';$('#gpw2').value='';if(await gate())start()}
$('#gbtn').onclick=gsubmit;$('#gpw').onkeydown=$('#gpw2').onkeydown=e=>{if(e.key=='Enter')gsubmit()};
$('#lo').onclick=async()=>{await api('logout',{});gate()};
$('#loa').onclick=async()=>{if(await ask({title:'Log out every device?',text:'This device is logged out too.',ok:'Log out all',danger:true}))act('logout_all',{},gate)};
$('#cpw').onclick=()=>act('password',{old:$('#opw').value,new:$('#npw').value},()=>{$('#opw').value='';$('#npw').value='';loadSet()});
$('#apis').onclick=()=>act('api_devices',{ips:$('#apid').value.split(/[\s,]+/).filter(x=>x)},loadSet);
function load(){if($('#app').style.display!='block')return;return Promise.all([badge(),({ov:loadOv,models:loadModels,gpus:loadGpus,net:loadNet,appr:loadAppr,drives:()=>Promise.all([loadStatus(),loadDrives()]),storage:()=>Promise.all([loadStore(),loadStorage(false)]),logs:loadLogs,addpc:loadAdd,set:loadSet})[TAB]()])}
/* the Network and Approvals tabs only when the network watch is installed */
function applyNet(){document.querySelectorAll('.netonly,#sheet .nb[data-t="net"]').forEach(b=>b.hidden=!INFO.network);if(!INFO.network&&['net','appr'].includes(TAB))go('ov')}
async function start(){try{INFO=Object.assign(INFO,await api('info'))}catch(e){}applyNet();load();fetchOv(30000).then(()=>setTop(OVD.st));pwrGet()}
gate().then(ok=>{if(ok)start()});setInterval(()=>{if($('#app').style.display!='block')return;const f=document.activeElement;
 if(TAB=='set'||TAB=='drives'||TAB=='storage'||(f&&(f.tagName=='INPUT'||f.tagName=='TEXTAREA'||f.tagName=='SELECT')))tick('badge',badge);else tick('load',load)},REFRESH_MS);
</script></body></html>"""
PAGE = PAGE.replace("{{VERSION}}", VERSION)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "aip": return v
        return ""

    def authed(self): return session_ok(self.token())

    def same_origin(self):
        """Extra check for the power buttons: the request must come from this page's own address (Origin, else Referer)."""
        host = self.headers.get("Host") or ""
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return bool(host) and urllib.parse.urlparse(src).netloc == host

    def is_secure(self):
        """HTTPS? Either the panel's own TLS, or a reverse proxy on this machine telling us so."""
        return bool(getattr(self.server, "tls", False)) or (
            self.client_address[0] == "127.0.0.1" and self.headers.get("X-Forwarded-Proto") == "https")

    def cookie(self, s):
        """Add Secure to a cookie when the connection is HTTPS (so browsers never send it in the clear)."""
        return s + "; Secure" if self.is_secure() else s

    def who(self):
        """For the log: address, browser and a short session id (never the token itself)."""
        k = hashlib.sha256(self.token().encode()).hexdigest()
        ua = re.sub(r"[^\w ./();,:-]", "", (_sessions().get(k) or {}).get("ua") or self.headers.get("User-Agent") or "")[:80]
        return f"{self.client_address[0]} (session {k[:8]}, {ua or 'unknown browser'})"
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
            return 200, {"output": "Logged out."}, [self.cookie("aip=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")]
        else:
            return None
        tok = new_session(ip, self.headers.get("User-Agent", ""))
        return 200, {"output": "ok"}, [self.cookie(f"aip={tok}; Max-Age={SESSION_DAYS*86400}; Path=/; HttpOnly; SameSite=Strict")]

    def send(self, code, obj, ctype="application/json", cookies=()):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        for c in cookies: self.send_header("Set-Cookie", c)
        self.send_header("X-Frame-Options", "DENY"); self.send_header("Cache-Control", "no-store")
        if self.is_secure(): self.send_header("Strict-Transport-Security", "max-age=31536000")
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
                                                   "pool_gb": float(c.get("POOL_GB", 13)), "network": network_on()})
            if p == "installed": return self.send(200, installed())
            if p == "progress": return self.send(200, progress())
            if p == "suggestions": return self.send(200, jload(SUG, {"items": []}))
            if p == "gpus": return self.send(200, gpus())
            if p == "workers_extra": return self.send(200, {"items": extra_workers()})
            if p == "pcs": return self.send(200, pcs_info())
            if p == "drives": return self.send(200, drives(q.get("fresh", ["0"])[0] == "1"))
            if p == "status": return self.send(200, server_status())
            if p == "storage": return self.send(200, storage_info(q.get("fresh", ["0"])[0] == "1"))
            if p == "settings":
                d = settings(); cur = hashlib.sha256(self.token().encode()).hexdigest()[:16]
                for s in d["sessions"]: s["current"] = s["id"] == cur
                return self.send(200, d)
            if p == "model_usage":
                try: running = json.loads(get(f"{SWAP}/running")).get("running", [])
                except Exception: running = []
                return self.send(200, {"running": running, "last_used": last_used()})
            if p == "logs": return self.send(200, read_log(q.get("src", ["panel"])[0]))
            if p == "disk_usage": return self.send(200, disk_usage(q.get("fresh", ["0"])[0] == "1"))
            if p == "about": return self.send(200, about())
            if p == "power": return self.send(200, power_info())
            if p == "network": return self.send(200, network())
            if p == "requests": return self.send(200, requests_list())
            if p == "router_info":
                try: return self.send(200, router_admin("show"))
                except ValueError as e: return self.send(200, {"ok": False, "installed": False, "error": str(e)})
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
                try: meta = hf(f"/api/models/{repo}?expand[]=gguf").get("gguf") or {}
                except Exception: meta = {}
                params = meta.get("total")
                if not params:                                   # e.g. "Qwen3-8B-GGUF" -> 8 billion
                    m = re.search(r"(\d+(?:\.\d+)?)[bB](?![a-zA-Z])", repo.split("/")[1])
                    params = float(m.group(1)) * 1e9 if m else None
                return self.send(200, {"files": files, "vision": any("mmproj" in t["path"].lower() for t in g),
                                       "params_b": round(params / 1e9, 1) if params else None, "ctx_max": meta.get("context_length")})
        except Exception as e:
            print(f"GET {p} failed: {e!r}", flush=True)       # detail to the log, not to the client
            return self.send(500, {"error": "server error"})
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
                                 cookies=[self.cookie(f"aip={tok}; Max-Age={SESSION_DAYS*86400}; Path=/; HttpOnly; SameSite=Strict")])
            if p == "logout_all":
                end_sessions(); return self.send(200, {"output": "All devices logged out."}, cookies=[self.cookie("aip=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")])
            if p == "add":
                r = subprocess.run([ADD, b.get("name", ""), b.get("repo", ""), b.get("quant", ""), b.get("mode", "pool")], capture_output=True, text=True, timeout=120)
                out = (r.stdout + r.stderr).strip()
            elif p in ("dl_pause", "dl_resume"):
                n = str(b.get("name", ""))
                if not RE_NAME.match(n): return self.send(400, {"error": "bad name"})
                r = subprocess.run([ADD, "--pause" if p == "dl_pause" else "--resume", n], capture_output=True, text=True, timeout=60)
                out = (r.stdout + r.stderr).strip()
            elif p == "dl_cancel": out = cancel_download(str(b.get("name", "")))
            elif p == "model_load": out = load_model(str(b.get("name", "")))
            elif p == "model_unload": out = unload_model(str(b.get("name", "")))
            elif p == "model_test": return self.send(200, test_model(str(b.get("name", ""))))
            elif p == "remove":
                if not RE_NAME.match(str(b.get("name", ""))): return self.send(400, {"error": "bad name"})
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
            elif p == "pc_set": out = pc_set(b.get("ip", ""), b.get("name", ""), b.get("api"), b.get("share"), b.get("mac", ""))
            elif p == "pc_remove": out = pc_remove(b.get("ip", ""))
            elif p == "pc_reserve": out = pc_reserve(b.get("ip", ""))
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
            elif p == "telegram_test": out = telegram_test()
            elif p == "session_end":
                sid = str(b.get("id", "")); out = end_session_id(sid)
                if sid == hashlib.sha256(self.token().encode()).hexdigest()[:16]:      # this device: drop its cookie too
                    return self.send(200, {"output": out}, cookies=["aip=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"])
            elif p == "service_restart":
                u = str(b.get("unit", ""))
                if u not in RESTARTABLE: return self.send(400, {"error": "This service can't be restarted from the panel."})
                r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "--no-block", f"{u}.service"], capture_output=True, text=True, timeout=20)
                if r.returncode: return self.send(400, {"error": r.stderr.strip()[-300:] or "could not restart"})
                out = f"Restarting {dict(SERVICES).get(u, u)}. It is back in a few seconds."
            elif p == "power":
                if not self.same_origin(): return self.send(403, {"error": "Changes are only allowed from this page."})
                try: out = power(str(b.get("action", "")), b.get("confirm"), self.who())
                except Busy as e: return self.send(409, {"error": str(e), "power": power_info()})
                except ValueError as e: return self.send(400, {"error": str(e), "power": power_info()})
                return self.send(200, {"output": out, "power": power_info()})
            elif p == "backup":
                r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "ai-backup.service"], capture_output=True, text=True, timeout=20)
                if r.returncode: return self.send(400, {"error": r.stderr.strip()[-300:] or "could not start the backup"})
                out = "Backup started. It runs in the background and takes a few minutes."
            elif p == "test": out = test(str(b.get("what", "")))
            elif p == "label": out = set_label(b.get("mac", ""), b.get("known"), b.get("label", ""))
            elif p == "scan":
                r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "ai-net-scan.service"], capture_output=True, text=True, timeout=20)
                out = "Scan started - results in 1-2 minutes." if r.returncode == 0 else (r.stderr.strip() or "could not start the scan")
            elif p == "decide": out = decide(b.get("id", ""), b.get("decision", ""))
            elif p == "net_request": out = net_request(str(b.get("action", "")), b.get("mac", ""), b.get("name", ""))
            elif p == "router_save":
                d = router_admin("save", {k: str(b[k]) for k in ROUTER_KEYS if b.get(k) not in (None, "")})
                if not d.get("ok"): raise ValueError(d.get("error") or "could not save")
                out = d.get("output", "Saved.")
            elif p == "router_test":
                d = router_admin("test"); return self.send(200, {**d, "output": router_summary(d)})
            elif p == "router_detect":
                d = router_admin("detect", {"host": str(b["host"])} if b.get("host") else {})
                if not d.get("ok", True): raise ValueError(d.get("error") or "could not detect")
                return self.send(200, {**d, "output": f"Looks like: {d['label']}" if d.get("label") else "Could not tell the router type. Pick it yourself, or use Generic."})
            elif p == "router_forget_pin":
                d = router_admin("forget-pin")
                if not d.get("ok"): raise ValueError(d.get("error") or "failed")
                out = d.get("output", "Done.")
            else: return self.send(404, {"error": "not found"})
            self.send(200, {"output": out})
        except ValueError as e:
            self.send(400, {"error": str(e)})                 # intentional, user-facing validation messages
        except Exception as e:
            print(f"POST {p} failed: {e!r}", flush=True)       # unexpected: detail to the log, generic to the client
            self.send(500, {"error": "server error"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8091); ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    threading.Thread(target=guard_loop, daemon=True, name="gpu-auto-off").start()
    httpd = ThreadingHTTPServer((a.host, a.port), H)
    # Optional HTTPS: set PANEL_TLS_CERT and PANEL_TLS_KEY. Default is plain HTTP (put HTTPS on a reverse proxy instead).
    cert, key = os.environ.get("PANEL_TLS_CERT"), os.environ.get("PANEL_TLS_KEY")
    if cert and key:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True); httpd.tls = True
        print(f"control panel: HTTPS on :{a.port}", flush=True)
    httpd.serve_forever()

"""Router drivers for the home network watch (stdlib only). Used by netctl, net-exec and router-admin.

Every driver offers the same small interface:
    caps()                       -> set of "status", "clients", "block", "wifi", "updates"
    test()                       -> {"ok", "caps", "status", "clients", "error"}   (read-only)
    status()                     -> dict (model, version, uptime ...)
    clients()                    -> [{"mac", "ip", "name", "online"}]
    block_mac(mac) / unblock_mac(mac)   -> text   (only called by net-exec for APPROVED requests)

Safety rules (also enforced by router-admin, which runs as root):
  * the router address must be on the home network: a private IPv4 address or a .lan / .local name that resolves
    to one; each brand may only use its fixed ports (PORTS);
  * every MAC / IP / name is checked with a strict pattern before use; SSH runs fixed command strings only and never
    a shell built from user text;
  * HTTPS to the router accepts a self-signed certificate but pins its SHA-256 fingerprint on first use and refuses
    when it changes (PIN_FILE); redirects and proxies are never followed;
  * block rules carry the tag "ai_block" and unblock only removes rules with that exact tag.
"""
import base64, hashlib, ipaddress, json, os, re, socket, ssl, subprocess, time, urllib.error, urllib.parse, urllib.request
import http.client
import xml.etree.ElementTree as ET

ENV_FILE = "/var/lib/home-ai/net/router.env"    # root, 600 (written by router-admin)
LEGACY_ENV = "/opt/ai/secrets/router.env"     # old OpenWrt-only setting (HOST, USER)
PIN_FILE = "/var/lib/home-ai/net/router-pins.json"   # root, 600: {"ip:port": "sha256 of the certificate"}
STATE_FILE = "/var/lib/home-ai/net/router-blocks.json"   # generic driver: MAC -> IP it blocked in AdGuard
INVENTORY = "/var/lib/home-ai/net/inventory.json"   # devices found by net-scan
SSH_KEY = "/opt/ai/secrets/ssh_key"
KNOWN_HOSTS = "/opt/ai/secrets/known_hosts"
TAG = "ai_block"

RE_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
RE_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
RE_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,256}$")          # API keys, secrets, client ids
RE_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*\.(lan|local)$")
RE_SITE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
HOME_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


class RouterError(Exception):
    """A message that is safe to show to the owner (never contains a secret)."""


# ---------------------------------------------------------------- validation
def norm_mac(mac):
    m = str(mac or "").strip().lower().replace("-", ":")
    if not RE_MAC.match(m): raise RouterError("not a valid MAC address")
    return m


def home_ip(addr):
    """addr as a string if it is a private IPv4 address on a home network, else RouterError."""
    try: a = ipaddress.ip_address(str(addr).strip())
    except ValueError: raise RouterError("not a valid IP address")
    if a.version != 4 or not any(a in n for n in HOME_NETS): raise RouterError(f"{a} is not a home-network address")
    return str(a)


def check_host(host):
    """The router address as typed: a private IPv4 address or a .lan / .local name."""
    h = str(host or "").strip().lower()
    if not h: raise RouterError("router address missing")
    if RE_NAME.match(h): return h
    return home_ip(h)


def resolve_home(host):
    """The IPv4 address to connect to. A name must resolve to a home-network address (checked every time)."""
    h = check_host(host)
    if RE_NAME.match(h):
        try: infos = socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror: raise RouterError(f"{h} could not be found on the network")
        return home_ip(infos[0][4][0])
    return h


def gateway():
    """Default IPv4 gateway from the kernel routing table, or None."""
    try:
        with open("/proc/net/route") as f: lines = f.read().splitlines()[1:]
        for line in lines:
            f = line.split()
            if f[1] == "00000000" and int(f[3], 16) & 2:
                return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
    except Exception: pass
    return None


def _json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default


def _write600(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write(text)
    os.chmod(tmp, 0o600); os.replace(tmp, path)


def load_conf(path=None):
    """Settings for the router. Falls back to the old OpenWrt-only file (BRAND=openwrt) or to generic."""
    for p, legacy in ((path or ENV_FILE, False), (LEGACY_ENV, True)):
        d = {}
        try:
            with open(p) as f: text = f.read()
            for line in text.splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1); d[k.strip()] = v.strip().strip('"')
        except OSError: continue
        if legacy:
            if not d.get("HOST"): continue
            d["BRAND"] = "openwrt"
        d.setdefault("BRAND", "generic")
        return d
    return {"BRAND": "generic"}


# ---------------------------------------------------------------- HTTPS with a pinned certificate
class PinError(RouterError):
    pass


def _pins(): return _json(PIN_FILE, {})


def check_pin(key, fp):
    pins = _pins(); old = pins.get(key)
    if old is None:
        pins[key] = fp; _write600(PIN_FILE, json.dumps(pins, indent=1))
    elif old != fp:
        raise PinError(f"The router's certificate changed ({key}). If you replaced the router or its certificate, "
                       "press 'Trust the new certificate' in the panel; otherwise something may be intercepting the connection.")


def forget_pin(host=None):
    pins = _pins()
    keep = {k: v for k, v in pins.items() if host and not k.startswith(f"{host}:")}
    _write600(PIN_FILE, json.dumps(keep, indent=1))
    return len(pins) - len(keep)


class _PinnedConn(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        check_pin(f"{self.host}:{self.port}", hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest())


class _PinnedHandler(urllib.request.HTTPSHandler):
    def https_open(self, req): return self.do_open(_PinnedConn, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): raise RouterError("the router answered with a redirect; check the address and port")


def http_request(scheme, ip, port, method, path, body=None, headers=None, digest=None, timeout=15):
    """One request to the router (ip already checked). Returns (status, bytes). TLS: self-signed OK, but pinned."""
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.ProxyHandler({}), _NoRedirect(), _PinnedHandler(context=ctx)]
    url = f"{scheme}://{ip}:{port}{path}"
    if digest:
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None, f"{scheme}://{ip}:{port}/", *digest)
        handlers.append(urllib.request.HTTPDigestAuthHandler(pm))
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r: return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except PinError: raise
    except RouterError: raise
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, PinError): raise reason
        raise RouterError(f"could not reach the router at {ip}:{port} ({type(reason).__name__})")


# ---------------------------------------------------------------- SSH with fixed commands
def ssh(user, ip, port, command, timeout=40, with_stderr=False):
    """Run one FIXED command string on the router with the server's SSH key. user/ip/port are validated here.
    with_stderr=True returns stdout+stderr, stripped - exactly what netctl always printed."""
    if not RE_USER.match(user or ""): raise RouterError("invalid user name")
    home_ip(ip)
    if not os.path.exists(SSH_KEY): raise RouterError("the server's SSH key is missing (run the network setup step)")
    r = subprocess.run(["ssh", "-i", SSH_KEY, "-p", str(int(port)), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                        "--", f"{user}@{ip}", command], capture_output=True, text=True, timeout=timeout)
    if with_stderr: return (r.stdout + r.stderr).strip()
    if r.returncode == 255: raise RouterError("SSH login to the router failed (is the server's public key added on the router?)")
    return r.stdout


# ---------------------------------------------------------------- parsers shared by Linux-based routers
def parse_leases(text):
    """dnsmasq lease file: '<expiry> <mac> <ip> <name> <client-id>'."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4:
            try: mac = norm_mac(f[1]); ip = home_ip(f[2])
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": "" if f[3] == "*" else f[3][:64], "online": False}
    return out


def parse_proc_arp(text):
    """Linux /proc/net/arp. Flags 0x2 = complete (the device answered recently)."""
    out = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 4:
            try: mac = norm_mac(f[3]); ip = home_ip(f[0])
            except RouterError: continue
            if mac != "00:00:00:00:00:00": out[mac] = {"mac": mac, "ip": ip, "name": "", "online": f[2] == "0x2"}
    return out


def parse_ip_neigh(text):
    """'ip neigh show': '<ip> dev <if> lladdr <mac> REACHABLE|STALE|...'."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+) .*lladdr (\S+) (\S+)", line)
        if m:
            try: mac = norm_mac(m.group(2)); ip = home_ip(m.group(1))
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": "", "online": m.group(3) in ("REACHABLE", "DELAY", "PROBE", "STALE")}
    return out


def merge(*tables):
    """Combine client tables by MAC: keep a name and IP from any source, online if any source says so."""
    out = {}
    for t in tables:
        for mac, c in t.items():
            o = out.setdefault(mac, {"mac": mac, "ip": "", "name": "", "online": False})
            o["ip"] = o["ip"] or c.get("ip", ""); o["name"] = o["name"] or c.get("name", ""); o["online"] = o["online"] or c.get("online", False)
    return sorted(out.values(), key=lambda c: tuple(int(x) for x in (c["ip"] or "999.0.0.0").split(".")))


# ---------------------------------------------------------------- drivers
class Driver:
    NAME = "base"; LABEL = ""; TESTED = False
    CAPS = {"status", "clients"}
    PORTS = ()                    # allowed ports, first = default
    FIELDS = ("HOST",)            # settings this brand needs (see FIELD_INFO)
    DOC = ""                      # where the driver comes from
    NOTE = ""                     # shown in the panel

    def __init__(self, conf, adguard=None):
        self.c = conf; self.adguard = adguard

    def caps(self): return set(self.CAPS)

    def ip(self): return resolve_home(self.c.get("HOST") or gateway() or "")

    def port(self):
        p = str(self.c.get("PORT") or (self.PORTS[0] if self.PORTS else ""))
        if self.PORTS and (not p.isdigit() or int(p) not in self.PORTS):
            raise RouterError(f"port {p} is not allowed for {self.LABEL} (allowed: {', '.join(map(str, self.PORTS))})")
        return int(p) if p else None

    def user(self, default="root"):
        u = self.c.get("USER") or default
        if not RE_USER.match(u): raise RouterError("invalid user name")
        return u

    def secret(self, key):
        v = self.c.get(key, "")
        if not v: raise RouterError(f"{FIELD_INFO.get(key, (key,))[0]} is not saved yet")
        if not RE_TOKEN.match(v) and key not in ("PASS",): raise RouterError(f"the saved {FIELD_INFO.get(key, (key,))[0]} has invalid characters")
        return v

    def block_mac(self, mac): raise RouterError(f"{self.LABEL} support is read-only here: it can't block devices")
    unblock_mac = block_mac

    def reserve_dhcp(self, mac, ip, name=""):
        raise RouterError(f"{self.LABEL} can't set a DHCP reservation from here - do it in the router's own settings")

    def test(self):
        res = {"ok": False, "brand": self.NAME, "caps": sorted(self.caps()), "status": None, "clients": None, "error": None}
        try:
            res["status"] = self.status()
            res["clients"] = len(self.clients())
            res["ok"] = True
        except RouterError as e: res["error"] = str(e)
        except Exception as e: res["error"] = f"unexpected error ({type(e).__name__})"
        return res


class Generic(Driver):
    NAME = "generic"; LABEL = "Generic (any router)"; TESTED = True
    PORTS = (); FIELDS = ()
    DOC = "No router login. Devices come from the server's own network scan; blocking uses AdGuard Home."
    NOTE = ("Works with any router. Devices come from the server's own network scan. 'Block' stops the device's DNS in "
            "AdGuard Home (by IP address), so a device that uses its own DNS server is not stopped.")

    def caps(self): return {"status", "clients"} | ({"block"} if self.adguard else set())

    def status(self):
        gw = gateway()
        ok_gw = bool(gw) and subprocess.run(["ping", "-n", "-c1", "-W2", home_ip(gw)], capture_output=True).returncode == 0
        try: socket.getaddrinfo("example.com", 443); dns = True
        except OSError: dns = False
        try: socket.create_connection(("1.1.1.1", 443), 5).close(); net = True
        except OSError: net = False
        return {"model": "generic", "gateway": gw, "gateway_answers": ok_gw, "dns_works": dns, "internet": net}

    def clients(self):
        now = time.time(); out = {}
        for mac, d in _json(INVENTORY, {}).items():
            try: m = norm_mac(mac); ip = home_ip(d.get("ip", ""))
            except RouterError: continue
            try: seen = time.mktime(time.strptime(d.get("last_seen", ""), "%Y-%m-%d %H:%M:%S"))
            except ValueError: seen = 0
            out[m] = {"mac": m, "ip": ip, "name": (d.get("label") or d.get("name") or "")[:64], "online": now - seen < 1800}
        return merge(out)

    def block_mac(self, mac):
        mac = norm_mac(mac)
        if not self.adguard: raise RouterError("AdGuard Home is not connected, so devices can't be blocked")
        c = next((c for c in self.clients() if c["mac"] == mac), None)
        if not c or not c["ip"]: raise RouterError(f"{mac} is not in the network scan, so its IP address is unknown")
        st = _json(STATE_FILE, {}); st[mac] = c["ip"]; _write600(STATE_FILE, json.dumps(st, indent=1))
        return self.adguard(block=c["ip"]) + f" (DNS blocked for {c['ip']})"

    def unblock_mac(self, mac):
        mac = norm_mac(mac)
        if not self.adguard: raise RouterError("AdGuard Home is not connected")
        st = _json(STATE_FILE, {}); ip = st.pop(mac, None)
        if not ip: raise RouterError(f"{mac} was not blocked by this system")
        _write600(STATE_FILE, json.dumps(st, indent=1))
        return self.adguard(unblock=home_ip(ip))


class OpenWrt(Driver):
    NAME = "openwrt"; LABEL = "OpenWrt / iStoreOS / ImmortalWrt / GL.iNet / Turris"; TESTED = True
    CAPS = {"status", "clients", "block", "wifi", "updates", "reserve"}
    PORTS = (22,); FIELDS = ("HOST", "USER")
    DOC = "OpenWrt docs: firewall configuration (uci firewall rule src_mac), dnsmasq lease file /tmp/dhcp.leases."
    NOTE = "Add the server's public key on the router (System > Administration > SSH-Keys)."
    # the commands netctl has always used (scripts and agents may read their raw output)
    CMDS = {
        "clients": "cat /tmp/dhcp.leases; echo; ip neigh show | grep -v FAILED",
        "dhcp": "cat /tmp/dhcp.leases",
        "firewall": "uci show firewall | grep -E 'rule|redirect|zone' | head -200",
        "status": "uptime; free -m; df -h /overlay 2>/dev/null; cat /etc/openwrt_release 2>/dev/null",
        "updates": "opkg update >/dev/null 2>&1; opkg list-upgradable 2>/dev/null | head -50",
        "wifi": "iwinfo 2>/dev/null | head -60",
    }

    def run(self, cmd): return ssh(self.user(), self.ip(), self.port(), cmd)

    def raw(self, sub): return ssh(self.user(), self.ip(), self.port(), self.CMDS[sub], 60, with_stderr=True)

    def status(self):
        t = self.run("cat /etc/openwrt_release 2>/dev/null; echo; cat /proc/uptime")
        rel = dict(re.findall(r"^DISTRIB_(\w+)='?([^'\n]*)'?", t, re.M))
        up = re.search(r"^(\d+)\.\d+ ", t, re.M)
        return {"model": rel.get("ID", "OpenWrt"), "version": rel.get("DESCRIPTION") or rel.get("RELEASE", ""),
                "uptime_s": int(up.group(1)) if up else None}

    def clients(self):
        t = self.run("cat /tmp/dhcp.leases; echo ===; ip neigh show")
        leases, _, neigh = t.partition("===")
        return merge(parse_leases(leases), parse_ip_neigh(neigh))

    @staticmethod
    def rule(mac): return f"{TAG}_" + norm_mac(mac).replace(":", "")

    def block_mac(self, mac):
        mac = norm_mac(mac); name = self.rule(mac)          # mac + name are [0-9a-f:_] only
        return self.run(f"uci -q delete firewall.{name}; uci set firewall.{name}=rule; uci set firewall.{name}.name='AI block {mac}'; "
                        f"uci set firewall.{name}.src='lan'; uci set firewall.{name}.dest='*'; uci set firewall.{name}.src_mac='{mac}'; "
                        f"uci set firewall.{name}.target='REJECT'; uci commit firewall; /etc/init.d/firewall reload; echo blocked {mac}")

    def unblock_mac(self, mac):
        mac = norm_mac(mac); name = self.rule(mac)
        return self.run(f"uci -q delete firewall.{name}; uci commit firewall; /etc/init.d/firewall reload; echo unblocked {mac}")

    def reserve_dhcp(self, mac, ip, name=""):
        """Give this PC a fixed address in the router's DHCP. Backs up the config, refuses an IP that belongs to
        another device, reuses the PC's existing reservation if it has one. mac/ip/name are validated (shell-safe)."""
        mac = norm_mac(mac); ip = home_ip(ip)
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:32] or "aipc"
        out = self.run(
            "set -e; cp /etc/config/dhcp /etc/config/dhcp.bak-homeai 2>/dev/null || true; "
            f"L=$(awk '$3==\"{ip}\"{{print $2}}' /tmp/dhcp.leases | head -1); "
            f"if [ -n \"$L\" ] && [ \"$L\" != \"{mac}\" ]; then echo \"CONFLICT lease $L\"; exit 0; fi; "
            f"while read -r s; do m=$(uci -q get \"dhcp.$s.mac\"); i=$(uci -q get \"dhcp.$s.ip\"); "
            f"[ \"$i\" = \"{ip}\" ] && [ -n \"$m\" ] && [ \"$m\" != \"{mac}\" ] && {{ echo \"CONFLICT reserved $m\"; exit 0; }}; "
            f"done <<EOF\n$(uci show dhcp | sed -n \"s/^dhcp\\.\\([^.]*\\)=host$/\\1/p\")\nEOF\n"
            f"E=$(uci show dhcp | sed -n \"s/^dhcp\\.\\([^.]*\\)\\.mac='{mac}'$/\\1/p\" | head -1); "
            f"if [ -n \"$E\" ]; then H=$E; else H=$(uci add dhcp host); uci set \"dhcp.$H.mac={mac}\"; fi; "
            f"uci set \"dhcp.$H.ip={ip}\"; uci set \"dhcp.$H.name={name}\"; uci -q set \"dhcp.$H.dns=1\"; "
            "uci commit dhcp; /etc/init.d/dnsmasq reload >/dev/null 2>&1 || /etc/init.d/dnsmasq restart >/dev/null 2>&1; "
            f"echo \"RESERVED {ip} {mac} {name}\"")
        line = (out or "").strip().splitlines()[-1] if out.strip() else ""
        if "CONFLICT lease" in out: raise RouterError(f"{ip} is already in use by another device ({out.split('CONFLICT lease',1)[1].strip()})")
        if "CONFLICT reserved" in out: raise RouterError(f"{ip} is already reserved for another device ({out.split('CONFLICT reserved',1)[1].strip()})")
        if not line.startswith("RESERVED"): raise RouterError(f"the router did not confirm the reservation: {line or 'no output'}")
        return f"Reserved {ip} for {name} ({mac}). A backup of the router's DHCP config was saved as /etc/config/dhcp.bak-homeai."


class MikroTik(Driver):
    NAME = "mikrotik"; LABEL = "MikroTik (RouterOS 7)"
    CAPS = {"status", "clients", "block"}
    PORTS = (443,); FIELDS = ("HOST", "PORT", "USER", "PASS")
    DOC = ("help.mikrotik.com: REST API (RouterOS v7.1+, HTTPS www-ssl, HTTP Basic, GET/PUT/DELETE, '.id', ?field=value "
           "filters), DHCP (lease: mac-address, address, host-name, status=bound), Filter + Common Firewall Matchers "
           "(chain=forward, action=drop, src-mac-address, comment).")
    NOTE = ("Enable the www-ssl service. Make a user with a group that has the 'read', 'write' and 'rest-api' policies "
            "(least rights) and use it here.")

    def call(self, method, path, body=None):
        tok = base64.b64encode(f"{self.user('admin')}:{self.secret('PASS')}".encode()).decode()
        st, data = http_request("https", self.ip(), self.port(), method, "/rest" + path,
                                json.dumps(body).encode() if body is not None else None,
                                {"Authorization": "Basic " + tok, "Content-Type": "application/json"})
        if st in (401, 403): raise RouterError("the router refused the login (check user, password and the user's policies)")
        if st >= 400: raise RouterError(f"the router answered {st}")
        return json.loads(data or b"null")

    def status(self):
        r = self.call("GET", "/system/resource")
        return {"model": r.get("board-name", ""), "version": r.get("version", ""), "uptime": r.get("uptime", "")}

    def clients(self):
        out = {}
        for l in self.call("GET", "/ip/dhcp-server/lease"):
            try: mac = norm_mac(l.get("mac-address")); ip = home_ip(l.get("active-address") or l.get("address", ""))
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": (l.get("host-name") or l.get("comment") or "")[:64], "online": l.get("status") == "bound"}
        return merge(out)

    def _ours(self, mac):
        tag = f"{TAG} {mac}"
        rules = self.call("GET", "/ip/firewall/filter?comment=" + urllib.parse.quote(tag))
        return [r for r in rules if r.get("comment") == tag and re.match(r"^\*[0-9A-Fa-f]+$", r.get(".id", ""))]

    def block_mac(self, mac):
        mac = norm_mac(mac)
        if self._ours(mac): return f"{mac} is already blocked"
        # appended at the end of the forward chain (place-before is not in the docs we used)
        self.call("PUT", "/ip/firewall/filter", {"chain": "forward", "action": "drop", "src-mac-address": mac.upper(), "comment": f"{TAG} {mac}"})
        return f"blocked {mac}"

    def unblock_mac(self, mac):
        mac = norm_mac(mac); n = 0
        for r in self._ours(mac): self.call("DELETE", "/ip/firewall/filter/" + r[".id"]); n += 1
        return f"unblocked {mac} ({n} rule(s) removed)" if n else f"{mac} was not blocked by this system"


def _rows(d):
    """OPNsense lists: {"rows": [...]} or a plain list."""
    if isinstance(d, dict): d = d.get("rows", d.get("data", []))
    return d if isinstance(d, list) else []


def _pick(row, *keys):
    for k in keys:
        if row.get(k): return str(row[k])
    return ""


class OPNsense(Driver):
    NAME = "opnsense"; LABEL = "OPNsense"
    PORTS = (443,); FIELDS = ("HOST", "PORT", "KEY", "SECRET")
    DOC = ("docs.opnsense.org: API (key/secret as HTTP Basic, /api/<module>/<controller>/<command>), diagnostics "
           "interface get_arp, dhcpv4/kea/dnsmasq lease search, diagnostics system system_information. Field names are "
           "not documented, so several spellings are accepted.")
    NOTE = ("Read-only: OPNsense firewall rules match IP addresses, not MAC addresses. Make an API key for a user that "
            "only has the 'Diagnostics: ARP Table', 'Status: DHCP leases' and 'System: Status' privileges.")

    def call(self, path):
        tok = base64.b64encode(f"{self.secret('KEY')}:{self.secret('SECRET')}".encode()).decode()
        st, data = http_request("https", self.ip(), self.port(), "GET", "/api/" + path, headers={"Authorization": "Basic " + tok})
        if st in (401, 403): raise RouterError("OPNsense refused the API key (check key, secret and privileges)")
        if st >= 400: return None
        return json.loads(data or b"null")

    def status(self):
        s = self.call("diagnostics/system/system_information") or {}
        return {"model": "OPNsense", "version": _pick(s, "versions", "product_version", "name") if isinstance(s, dict) else ""}

    def clients(self):
        arp, names = {}, {}
        for r in _rows(self.call("diagnostics/interface/get_arp")):
            try: mac = norm_mac(_pick(r, "mac", "hwaddr")); ip = home_ip(_pick(r, "ip", "address"))
            except RouterError: continue
            arp[mac] = {"mac": mac, "ip": ip, "name": _pick(r, "hostname", "host")[:64], "online": True}
        for path in ("dhcpv4/leases/search_lease", "kea/leases/search", "dnsmasq/leases/search"):
            for r in _rows(self.call(path)):
                try: mac = norm_mac(_pick(r, "mac", "hwaddr", "hw_address")); ip = home_ip(_pick(r, "address", "ip", "ip_address"))
                except RouterError: continue
                names[mac] = {"mac": mac, "ip": ip, "name": _pick(r, "hostname", "client-hostname", "host")[:64], "online": False}
        return merge(arp, names)


class SSHReadOnly(Driver):
    """Base for routers we only read over SSH (fixed commands, ARP table + lease file)."""
    PORTS = (22,); FIELDS = ("HOST", "USER")
    STATUS_CMD = "uptime"; CLIENT_CMD = "cat /proc/net/arp; echo ===; cat /tmp/dnsmasq.leases 2>/dev/null"
    DEFAULT_USER = "root"

    def run(self, cmd): return ssh(self.user(self.DEFAULT_USER), self.ip(), self.port(), cmd)

    def status(self): return {"model": self.LABEL, "info": self.run(self.STATUS_CMD).strip()[:600]}

    def clients(self):
        arp, _, leases = self.run(self.CLIENT_CMD).partition("===")
        return merge(parse_proc_arp(arp), parse_leases(leases))


class PfSense(SSHReadOnly):
    NAME = "pfsense"; LABEL = "pfSense"; DEFAULT_USER = "admin"
    DOC = ("docs.netgate.com: SSH access (System > Advanced > Admin Access; admin or users with 'User - System - Shell "
           "account access'). ARP table via FreeBSD 'arp -an'; ISC DHCP leases file if present.")
    NOTE = ("Read-only: pfSense firewall rules match IP addresses, not MAC addresses. Add the server's public key to "
            "the user under System > User Manager.")
    STATUS_CMD = "uname -sr; uptime; cat /etc/version 2>/dev/null"
    CLIENT_CMD = "arp -an; echo ===; cat /var/dhcpd/var/db/dhcpd.leases 2>/dev/null"

    def clients(self):
        arp_t, _, lease_t = self.run(self.CLIENT_CMD).partition("===")
        arp, leases = {}, {}
        for m in re.finditer(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]{17})", arp_t):
            try: mac = norm_mac(m.group(2)); arp[mac] = {"mac": mac, "ip": home_ip(m.group(1)), "name": "", "online": True}
            except RouterError: continue
        for blk in re.finditer(r"lease (\d+\.\d+\.\d+\.\d+) \{(.*?)\}", lease_t, re.S):
            mac = re.search(r"hardware ethernet ([0-9a-fA-F:]{17});", blk.group(2))
            name = re.search(r'client-hostname "([^"]{0,64})";', blk.group(2))
            if not mac: continue
            try: m2 = norm_mac(mac.group(1)); leases[m2] = {"mac": m2, "ip": home_ip(blk.group(1)), "name": name.group(1) if name else "", "online": False}
            except RouterError: continue
        return merge(arp, leases)


class AsusWrt(SSHReadOnly):
    NAME = "asuswrt"; LABEL = "ASUS (ASUSWRT / Asuswrt-Merlin)"; DEFAULT_USER = "admin"
    DOC = ("Asuswrt-Merlin wiki: SSH (Administration > System > Enable SSH, Dropbear, key login). Clients from the "
           "Linux ARP table (/proc/net/arp) and the dnsmasq lease file if present.")
    NOTE = "Read-only: blocking on ASUS firmware is not safely reversible from outside. Enable SSH with key login."
    STATUS_CMD = "uptime; nvram get productid; nvram get firmver; nvram get buildno; nvram get extendno"
    CLIENT_CMD = "cat /proc/net/arp; echo ===; cat /var/lib/misc/dnsmasq.leases 2>/dev/null"


class DDWrt(SSHReadOnly):
    NAME = "ddwrt"; LABEL = "DD-WRT"
    DOC = ("DD-WRT: SSH (Services > Secure Shell, key login). The official wiki could not be read; clients come from "
           "the Linux ARP table (/proc/net/arp) and the dnsmasq lease file if present.")
    NOTE = "Read-only. Enable SSH with key login (Services > Secure Shell)."
    STATUS_CMD = "uptime; nvram get DD_BOARD; nvram get os_version"


class FritzBox(Driver):
    NAME = "fritzbox"; LABEL = "AVM FRITZ!Box"
    PORTS = (49443,); FIELDS = ("HOST", "PORT", "USER", "PASS")
    DOC = ("AVM TR-064 documents 'Hosts' (urn:LanDeviceHosts-com:serviceId:Hosts1: GetHostNumberOfEntries, "
           "GetGenericHostEntry NewIndex -> NewIPAddress, NewMACAddress, NewHostName, NewActive) and 'DeviceInfo' "
           "(GetInfo -> NewModelName, NewSoftwareVersion, NewUpTime). Service type and control URL are read from the "
           "box's own description (/tr64desc.xml). HTTP digest login.")
    NOTE = ("Read-only. Turn on 'Allow access for applications' (Home Network > Network > Network Settings) and use a "
            "FRITZ!Box user with only the rights it needs.")

    def _services(self):
        st, data = http_request("https", self.ip(), self.port(), "GET", "/tr64desc.xml")
        if st != 200: raise RouterError("this does not look like a FRITZ!Box (no TR-064 description)")
        local = lambda el: el.tag.split("}")[-1]
        out = {}
        for s in ET.fromstring(data).iter():
            if local(s) != "service": continue
            f = {local(c): (c.text or "").strip() for c in s}
            if f.get("serviceId"): out[f["serviceId"].split(":")[-1]] = (f.get("serviceType", ""), f.get("controlURL", ""))
        return out

    def soap(self, svc, action, args=None):
        stype, url = self._svc[svc]
        if not url.startswith("/") or ".." in url or not re.match(r"^urn:[\w.:-]+$", stype): raise RouterError("unexpected TR-064 description")
        a = "".join(f"<{k}>{int(v)}</{k}>" for k, v in (args or {}).items())       # only integer arguments are ever sent
        body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{stype}">{a}'
                f'</u:{action}></s:Body></s:Envelope>').encode()
        st, data = http_request("https", self.ip(), self.port(), "POST", url, body,
                                {"Content-Type": 'text/xml; charset="utf-8"', "SoapAction": f"{stype}#{action}"},
                                digest=(self.user("admin"), self.secret("PASS")))
        if st == 401: raise RouterError("the FRITZ!Box refused the login (user / password)")
        if st != 200: raise RouterError(f"the FRITZ!Box answered {st} to {action}")
        return {el.tag.split("}")[-1]: (el.text or "") for el in ET.fromstring(data).iter() if el.tag.split("}")[-1].startswith("New")}

    @property
    def _svc(self):
        if not hasattr(self, "_svc_cache"): self._svc_cache = self._services()
        return self._svc_cache

    def status(self):
        r = self.soap("DeviceInfo1", "GetInfo")
        return {"model": r.get("NewModelName", ""), "version": r.get("NewSoftwareVersion", ""), "uptime_s": int(r.get("NewUpTime") or 0)}

    def clients(self):
        n = min(int(self.soap("Hosts1", "GetHostNumberOfEntries").get("NewHostNumberOfEntries") or 0), 300); out = {}
        for i in range(n):
            r = self.soap("Hosts1", "GetGenericHostEntry", {"NewIndex": i})
            try: mac = norm_mac(r.get("NewMACAddress")); ip = home_ip(r.get("NewIPAddress", ""))
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": r.get("NewHostName", "")[:64], "online": r.get("NewActive") == "1"}
        return merge(out)


class UniFi(Driver):
    NAME = "unifi"; LABEL = "UniFi (Network application)"
    PORTS = (443,); FIELDS = ("HOST", "PORT", "KEY", "SITE")
    DOC = ("UniFi Network official API (integration v1: /proxy/network/integration/v1, X-API-KEY). Ubiquiti's doc site "
           "could not be read by our tools, so paths and field names are NOT verified against the docs.")
    NOTE = ("Read-only (the official API documents no block action). Create an API key in UniFi (Settings > Control "
            "Plane > Integrations). Not verified against the official docs yet - please report results.")
    BASE = "/proxy/network/integration/v1"

    def call(self, path):
        st, data = http_request("https", self.ip(), self.port(), "GET", self.BASE + path,
                                headers={"X-API-KEY": self.secret("KEY"), "Accept": "application/json"})
        if st in (401, 403): raise RouterError("UniFi refused the API key")
        if st >= 400: raise RouterError(f"UniFi answered {st}")
        return json.loads(data or b"null")

    def site_id(self):
        want = (self.c.get("SITE") or "").lower()
        sites = (self.call("/sites") or {}).get("data", [])
        s = next((x for x in sites if want and want in (str(x.get("name", "")).lower(), str(x.get("internalReference", "")).lower())), None) or (sites[0] if sites else None)
        if not s or not re.match(r"^[A-Za-z0-9-]{1,64}$", str(s.get("id", ""))): raise RouterError("no UniFi site found")
        return s["id"]

    def status(self):
        i = self.call("/info") or {}
        return {"model": "UniFi Network", "version": i.get("applicationVersion", "")}

    def clients(self):
        out = {}
        for c in (self.call(f"/sites/{self.site_id()}/clients?limit=200") or {}).get("data", []):
            try: mac = norm_mac(c.get("macAddress")); ip = home_ip(c.get("ipAddress", ""))
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": str(c.get("name", ""))[:64], "online": True}
        return merge(out)


class Omada(Driver):
    NAME = "omada"; LABEL = "TP-Link Omada (controller)"
    CAPS = {"status", "clients", "block"}
    PORTS = (8043, 443); FIELDS = ("HOST", "PORT", "OMADAC", "CLIENT_ID", "CLIENT_SECRET", "SITE")
    DOC = ("TP-Link Omada Open API (official spec /v3/api-docs, 'Omada Open API'): POST /openapi/authorize/token"
           "?grant_type=client_credentials {omadacId, client_id, client_secret}; header 'Authorization: AccessToken=...'; "
           "GET /openapi/v1/{omadacId}/sites; GET .../sites/{siteId}/clients (page, pageSize; mac, ip, name, hostName); "
           "POST .../clients/{clientMac}/block and /unblock.")
    NOTE = ("Create an Open API app in the controller (Settings > Platform Integration > Open API) in 'Client' mode with "
            "only the site and client permissions it needs.")

    def _api(self, method, path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token: h["Authorization"] = "AccessToken=" + token
        st, data = http_request("https", self.ip(), self.port(), method, path, json.dumps(body).encode() if body is not None else None, h)
        if st >= 400: raise RouterError(f"the Omada controller answered {st}")
        d = json.loads(data or b"{}")
        if d.get("errorCode", 0) != 0: raise RouterError(f"Omada error {d.get('errorCode')}: {str(d.get('msg', ''))[:120]}")
        return d.get("result")

    def token(self):
        if not hasattr(self, "_tok"):
            r = self._api("POST", "/openapi/authorize/token?grant_type=client_credentials",
                          {"omadacId": self.secret("OMADAC"), "client_id": self.secret("CLIENT_ID"), "client_secret": self.secret("CLIENT_SECRET")})
            self._tok = (r or {}).get("accessToken", "")
            if not RE_TOKEN.match(self._tok): raise RouterError("Omada did not return an access token")
        return self._tok

    def site(self):
        want = (self.c.get("SITE") or "").lower()
        sites = (self._api("GET", f"/openapi/v1/{self.secret('OMADAC')}/sites?page=1&pageSize=100", token=self.token()) or {}).get("data", [])
        s = next((x for x in sites if want and str(x.get("name", "")).lower() == want), None) or (sites[0] if sites else None)
        if not s or not re.match(r"^[A-Za-z0-9]{1,64}$", str(s.get("siteId", ""))): raise RouterError("no Omada site found")
        return s["siteId"]

    def status(self):
        return {"model": "Omada controller", "site": self.site()}

    def _list(self):
        return (self._api("GET", f"/openapi/v1/{self.secret('OMADAC')}/sites/{self.site()}/clients?page=1&pageSize=1000", token=self.token()) or {}).get("data", [])

    def clients(self):
        out = {}
        for c in self._list():
            try: mac = norm_mac(c.get("mac")); ip = home_ip(c.get("ip", ""))
            except RouterError: continue
            out[mac] = {"mac": mac, "ip": ip, "name": str(c.get("name") or c.get("hostName") or "")[:64], "online": True}
        return merge(out)

    def _act(self, mac, what):
        mac = norm_mac(mac)
        # use the MAC exactly as the controller writes it (format not specified in the spec), else AA-BB-CC-DD-EE-FF
        raw = next((c.get("mac") for c in self._list() if str(c.get("mac", "")).lower().replace("-", ":") == mac), mac.upper().replace(":", "-"))
        if not re.match(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$", raw): raise RouterError("unexpected MAC format from Omada")
        self._api("POST", f"/openapi/v1/{self.secret('OMADAC')}/sites/{self.site()}/clients/{raw}/{what}", token=self.token())
        return f"{what}ed {mac}"

    def block_mac(self, mac): return self._act(mac, "block")

    def unblock_mac(self, mac): return self._act(mac, "unblock")


DRIVERS = {d.NAME: d for d in (Generic, OpenWrt, MikroTik, OPNsense, PfSense, UniFi, AsusWrt, FritzBox, DDWrt, Omada)}

# settings shown in the panel: key -> (label, kind). kind "secret" is never shown again once saved.
FIELD_INFO = {"HOST": ("Router address", "text"), "PORT": ("Port", "port"), "USER": ("User name", "text"),
              "PASS": ("Password", "secret"), "KEY": ("API key", "secret"), "SECRET": ("API secret", "secret"),
              "SITE": ("Site name (empty = first site)", "text"), "OMADAC": ("Omada ID (omadacId)", "secret"),
              "CLIENT_ID": ("Client ID", "secret"), "CLIENT_SECRET": ("Client secret", "secret")}
SECRET_KEYS = {k for k, (_, kind) in FIELD_INFO.items() if kind == "secret"}


def get(conf=None, adguard=None):
    conf = conf if conf is not None else load_conf()
    cls = DRIVERS.get(conf.get("BRAND", "generic"))
    if not cls: raise RouterError("unknown router type")
    return cls(conf, adguard)


def catalog():
    """Brands for the panel: fields, ports, capabilities, tested flag, notes and sources."""
    return [{"brand": d.NAME, "label": d.LABEL, "tested": d.TESTED, "caps": sorted(d.CAPS if d is not Generic else {"status", "clients", "block"}),
             "ports": list(d.PORTS), "fields": [{"key": k, "label": FIELD_INFO[k][0], "kind": FIELD_INFO[k][1]} for k in d.FIELDS],
             "note": d.NOTE, "doc": d.DOC} for d in DRIVERS.values()]


# ---------------------------------------------------------------- auto-detect (read-only, no login)
SIGNS = [("openwrt", r"openwrt|istoreos|immortalwrt|luci|gl\.inet|turris"), ("mikrotik", r"routeros|mikrotik"),
         ("opnsense", r"opnsense"), ("pfsense", r"pfsense"), ("unifi", r"unifi|ubiquiti"), ("asuswrt", r"asuswrt|asus"),
         ("fritzbox", r"fritz!box|avm"), ("ddwrt", r"dd-wrt"), ("omada", r"omada")]


def detect(host=None):
    """Look at the router's SSH banner and web pages (no login, nothing sent but a GET) and suggest a brand."""
    ip = resolve_home(host or gateway() or "")
    seen = []
    try:
        with socket.create_connection((ip, 22), 4) as s:
            banner = s.recv(200).decode("latin1", "replace").split("\n")[0]
            seen.append("ssh: " + re.sub(r"[^\x20-\x7e]", "", banner)[:100])
    except OSError: pass
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    for scheme, port, path in (("http", 80, "/"), ("https", 443, "/"), ("http", 49000, "/tr64desc.xml"), ("https", 8043, "/")):
        try:
            c = (http.client.HTTPSConnection(ip, port, timeout=4, context=ctx) if scheme == "https" else http.client.HTTPConnection(ip, port, timeout=4))
            c.request("GET", path); r = c.getresponse(); body = r.read(20000).decode("latin1", "replace")
            title = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
            seen.append(f"{scheme}:{port}: {r.status} server={r.getheader('Server', '')[:60]} title={(title.group(1).strip() if title else '')[:80]} "
                        + " ".join(sorted(set(m.lower() for m in re.findall(r"openwrt|istoreos|luci|routeros|mikrotik|opnsense|pfsense|unifi|asuswrt|fritz!box|dd-wrt|omada", body, re.I)))))
            c.close()
        except (OSError, http.client.HTTPException): pass
    text = " ".join(seen).lower()
    guess = next((b for b, rx in SIGNS if re.search(rx, text)), None)
    if not guess and "dropbear" in text: guess = "openwrt"
    return {"host": ip, "suggested": guess, "label": DRIVERS[guess].LABEL if guess else None, "evidence": seen}

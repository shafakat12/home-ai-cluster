"""Mocked tests for server/files/routers.py: every driver gets fake router answers shaped like its official docs.
Run:  python3 -I tests/test_routers.py  (stdlib only, no network, no router needed; NET_DIR=<dir> to test another copy)."""
import importlib.util, json, os, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NETDIR = os.environ.get("NET_DIR") or os.path.join(HERE, "..", "server", "files")
spec = importlib.util.spec_from_file_location("routers", os.path.join(NETDIR, "routers.py"))
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)

MAC = "aa:bb:cc:dd:ee:01"


class Fake:
    """Answers http_request / ssh from a table and records every call."""
    def __init__(self, http=None, ssh=None):
        self.http, self.sshmap, self.calls = http or {}, ssh or {}, []

    def http_request(self, scheme, ip, port, method, path, body=None, headers=None, digest=None, timeout=15):
        self.calls.append((method, path, json.loads(body) if body and body[:1] in b"{[" else body, headers, digest))
        for (m, p), ans in self.http.items():
            if m == method and (path == p or (p.endswith("*") and path.startswith(p[:-1]))):
                ans = ans(body) if callable(ans) else ans
                return (200, ans) if isinstance(ans, bytes) else (200, json.dumps(ans).encode())
        return 404, b'{"error":404}'

    def ssh(self, user, ip, port, command, timeout=40, **kw):
        self.calls.append(("ssh", user, ip, port, command))
        for k, v in self.sshmap.items():
            if command.startswith(k): return v
        return ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        R.PIN_FILE = f"{self.tmp}/pins.json"; R.STATE_FILE = f"{self.tmp}/blocks.json"; R.INVENTORY = f"{self.tmp}/inv.json"
        self._orig = (R.http_request, R.ssh)

    def tearDown(self): R.http_request, R.ssh = self._orig

    def use(self, fake): R.http_request, R.ssh = fake.http_request, fake.ssh; return fake


class Validation(Base):
    def test_mac(self):
        self.assertEqual(R.norm_mac("AA-BB-CC-DD-EE-01"), MAC)
        for bad in ("aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:0g", "aa:bb:cc:dd:ee:01; reboot", ""):
            with self.assertRaises(R.RouterError): R.norm_mac(bad)

    def test_home_addresses_only(self):
        for ok in ("192.168.1.1", "10.0.0.1", "172.16.5.4", "router.lan", "fritz-box.local"):
            self.assertTrue(R.check_host(ok))
        for bad in ("8.8.8.8", "172.32.0.1", "127.0.0.1", "169.254.1.1", "fd00::1", "example.com", "router.lan.evil.com",
                    "a b.lan", "x.lan;id", "http://192.168.1.1", ""):
            with self.assertRaises(R.RouterError, msg=bad): R.check_host(bad)

    def test_fixed_ports(self):
        d = R.MikroTik({"BRAND": "mikrotik", "HOST": "192.168.1.1", "PORT": "8080"})
        with self.assertRaises(R.RouterError): d.port()
        self.assertEqual(R.MikroTik({"HOST": "192.168.1.1"}).port(), 443)

    def test_pin(self):
        R.check_pin("192.168.1.1:443", "aa")
        R.check_pin("192.168.1.1:443", "aa")                     # same certificate: fine
        with self.assertRaises(R.PinError): R.check_pin("192.168.1.1:443", "bb")
        self.assertEqual(oct(os.stat(R.PIN_FILE).st_mode & 0o777), "0o600")
        self.assertEqual(R.forget_pin("192.168.1.1"), 1)
        R.check_pin("192.168.1.1:443", "bb")                     # trusted again after forgetting

    def test_catalog(self):
        cat = R.catalog()
        self.assertEqual({c["brand"] for c in cat}, set(R.DRIVERS))
        self.assertTrue(all(f["key"] in R.FIELD_INFO for c in cat for f in c["fields"]))
        self.assertEqual([c["brand"] for c in cat if c["tested"]], ["generic", "openwrt"])


class GenericT(Base):
    def test_clients_and_adguard_block(self):
        json.dump({MAC: {"mac": MAC, "ip": "192.168.1.50", "label": "TV", "last_seen": "2000-01-01 00:00:00"},
                   "zz": {"mac": "zz", "ip": "1.2.3.4"}}, open(R.INVENTORY, "w"))
        calls = []
        def adguard(block=None, unblock=None): calls.append((block, unblock)); return "done"
        d = R.Generic({"BRAND": "generic"}, adguard)
        self.assertEqual(d.clients(), [{"mac": MAC, "ip": "192.168.1.50", "name": "TV", "online": False}])
        self.assertIn("block", d.caps())
        d.block_mac(MAC.upper()); d.unblock_mac(MAC)
        self.assertEqual(calls, [("192.168.1.50", None), (None, "192.168.1.50")])
        with self.assertRaises(R.RouterError): d.unblock_mac(MAC)          # nothing left to undo
        self.assertNotIn("block", R.Generic({"BRAND": "generic"}).caps())  # no AdGuard: no blocking


class OpenWrtT(Base):
    def test_clients_and_block(self):
        f = self.use(Fake(ssh={"cat /tmp/dhcp.leases; echo ===":
                               f"1700000000 {MAC} 192.168.1.50 laptop *\n1700000000 aa:bb:cc:dd:ee:02 192.168.1.51 * 01:aa\n===\n"
                               f"192.168.1.50 dev br-lan lladdr {MAC} REACHABLE\n192.168.1.9 dev br-lan  FAILED\n",
                               "uci": "blocked"}))
        d = R.OpenWrt({"BRAND": "openwrt", "HOST": "192.168.1.1", "USER": "root"})
        c = d.clients()
        self.assertEqual(c[0], {"mac": MAC, "ip": "192.168.1.50", "name": "laptop", "online": True})
        self.assertEqual(c[1]["name"], "")
        d.block_mac(MAC)
        cmd = f.calls[-1][4]
        self.assertIn("firewall.ai_block_aabbccddee01=rule", cmd); self.assertIn(f"src_mac='{MAC}'", cmd)
        self.assertEqual(f.calls[-1][1:4], ("root", "192.168.1.1", 22))
        d.unblock_mac(MAC); self.assertTrue(f.calls[-1][4].startswith("uci -q delete firewall.ai_block_aabbccddee01;"))
        with self.assertRaises(R.RouterError): d.block_mac("aa:bb:cc:dd:ee:01'; rm -rf /")

    def test_legacy_commands_unchanged(self):
        self.assertEqual(R.OpenWrt.CMDS["clients"], "cat /tmp/dhcp.leases; echo; ip neigh show | grep -v FAILED")
        self.assertEqual(set(R.OpenWrt.CMDS), {"clients", "dhcp", "firewall", "status", "updates", "wifi"})

    def test_reserve_dhcp(self):
        d = R.OpenWrt({"BRAND": "openwrt", "HOST": "192.168.1.1", "USER": "root"})
        self.assertIn("reserve", d.caps())
        f = self.use(Fake(ssh={"set -e; cp /etc/config/dhcp": "RESERVED 192.168.1.50 " + MAC + " pc\n"}))
        msg = d.reserve_dhcp(MAC.upper(), "192.168.1.50", "pc")
        self.assertIn("Reserved 192.168.1.50", msg); self.assertIn("bak-homeai", msg)
        cmd = f.calls[-1][4]
        self.assertIn(f"dhcp.$H.mac={MAC}", cmd); self.assertIn("dhcp.$H.ip=192.168.1.50", cmd); self.assertIn("cp /etc/config/dhcp /etc/config/dhcp.bak-homeai", cmd)
        self.use(Fake(ssh={"set -e; cp /etc/config/dhcp": "CONFLICT lease ff:ff:ff:ff:ff:ff\n"}))
        with self.assertRaises(R.RouterError): d.reserve_dhcp(MAC, "192.168.1.50", "pc")
        with self.assertRaises(R.RouterError): d.reserve_dhcp("not-a-mac", "192.168.1.50", "pc")

    def test_reserve_not_supported_on_readonly(self):
        with self.assertRaises(R.RouterError): R.FritzBox({"BRAND": "fritzbox", "HOST": "192.168.1.1"}).reserve_dhcp(MAC, "192.168.1.50")


class MikroTikT(Base):
    def setUp(self):
        super().setUp()
        self.rules = [{".id": "*1", "chain": "forward", "action": "accept", "comment": "defconf: accept established"}]
        def put(body):
            b = json.loads(body); b[".id"] = "*A"; self.rules.append(b); return b
        self.f = self.use(Fake(http={
            ("GET", "/rest/system/resource"): {"board-name": "hAP ax3", "version": "7.16 (stable)", "uptime": "1d2h"},
            ("GET", "/rest/ip/dhcp-server/lease"): [{".id": "*5", "address": "192.168.88.10", "mac-address": "AA:BB:CC:DD:EE:01",
                                                     "host-name": "phone", "status": "bound"},
                                                    {".id": "*6", "address": "8.8.8.8", "mac-address": "AA:BB:CC:DD:EE:02", "status": "bound"}],
            ("GET", "/rest/ip/firewall/filter?comment=*"): lambda _: [r for r in self.rules if r.get("comment") == f"ai_block {MAC}"],
            ("PUT", "/rest/ip/firewall/filter"): put,
            ("DELETE", "/rest/ip/firewall/filter/*A"): b"",
        }))
        self.d = R.MikroTik({"BRAND": "mikrotik", "HOST": "192.168.88.1", "USER": "ai", "PASS": "pw"})

    def test_status_clients(self):
        self.assertEqual(self.d.status()["model"], "hAP ax3")
        self.assertEqual(self.d.clients(), [{"mac": MAC, "ip": "192.168.88.10", "name": "phone", "online": True}])  # public IP dropped
        self.assertTrue(self.f.calls[0][3]["Authorization"].startswith("Basic "))

    def test_block_unblock_only_our_rule(self):
        self.d.block_mac(MAC)
        put = [c for c in self.f.calls if c[0] == "PUT"][0][2]
        self.assertEqual(put, {"chain": "forward", "action": "drop", "src-mac-address": "AA:BB:CC:DD:EE:01", "comment": f"ai_block {MAC}"})
        self.assertIn("already", self.d.block_mac(MAC))
        self.d.unblock_mac(MAC)
        self.assertEqual([c[1] for c in self.f.calls if c[0] == "DELETE"], ["/rest/ip/firewall/filter/*A"])   # never *1


class OPNsenseT(Base):
    def test_read_only(self):
        self.use(Fake(http={("GET", "/api/diagnostics/system/system_information"): {"name": "fw", "versions": ["OPNsense 25.1"]},
                            ("GET", "/api/diagnostics/interface/get_arp"): [{"mac": MAC, "ip": "10.0.0.5", "hostname": "nas", "intf": "igb1"}],
                            ("GET", "/api/kea/leases/search"): {"rows": [{"hwaddr": "aa:bb:cc:dd:ee:02", "address": "10.0.0.6", "hostname": "tv"}]}}))
        d = R.OPNsense({"BRAND": "opnsense", "HOST": "10.0.0.1", "KEY": "k", "SECRET": "s"})
        self.assertEqual([(c["mac"], c["name"], c["online"]) for c in d.clients()], [(MAC, "nas", True), ("aa:bb:cc:dd:ee:02", "tv", False)])
        self.assertNotIn("block", d.caps())
        with self.assertRaises(R.RouterError): d.block_mac(MAC)


class PfSenseT(Base):
    def test_read_only(self):
        self.use(Fake(ssh={"arp -an": f"? (192.168.1.20) at {MAC} on igb1 expires in 1180 seconds [ethernet]\n===\n"
                                       'lease 192.168.1.21 {\n  binding state active;\n  hardware ethernet aa:bb:cc:dd:ee:02;\n'
                                       '  client-hostname "printer";\n}\n'}))
        d = R.PfSense({"BRAND": "pfsense", "HOST": "192.168.1.1"})
        self.assertEqual([(c["ip"], c["name"]) for c in d.clients()], [("192.168.1.20", ""), ("192.168.1.21", "printer")])
        with self.assertRaises(R.RouterError): d.block_mac(MAC)


class LinuxSSHT(Base):
    def test_asus_and_ddwrt(self):
        out = (f"IP address HW type Flags HW address Mask Device\n192.168.50.10 0x1 0x2 {MAC} * br0\n"
               "192.168.50.11 0x1 0x0 00:00:00:00:00:00 * br0\n===\n1700000000 aa:bb:cc:dd:ee:03 192.168.50.12 cam *\n")
        for cls in (R.AsusWrt, R.DDWrt):
            f = self.use(Fake(ssh={"cat /proc/net/arp": out}))
            c = cls({"BRAND": cls.NAME, "HOST": "192.168.50.1"}).clients()
            self.assertEqual([(x["mac"], x["online"], x["name"]) for x in c], [(MAC, True, ""), ("aa:bb:cc:dd:ee:03", False, "cam")])
            self.assertEqual(f.calls[0][1], "admin" if cls is R.AsusWrt else "root")


class FritzT(Base):
    DESC = (b'<?xml version="1.0"?><root xmlns="urn:dslforum-org:device-1-0"><device><serviceList>'
            b'<service><serviceType>urn:dslforum-org:service:DeviceInfo:1</serviceType><serviceId>urn:DeviceInfo-com:serviceId:DeviceInfo1</serviceId>'
            b'<controlURL>/upnp/control/deviceinfo</controlURL></service>'
            b'<service><serviceType>urn:dslforum-org:service:Hosts:1</serviceType><serviceId>urn:LanDeviceHosts-com:serviceId:Hosts1</serviceId>'
            b'<controlURL>/upnp/control/hosts</controlURL></service></serviceList></device></root>')

    @staticmethod
    def soap(action, **out):
        a = "".join(f"<{k}>{v}</{k}>" for k, v in out.items())
        return (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action}Response xmlns:u="x">{a}'
                f'</u:{action}Response></s:Body></s:Envelope>').encode()

    def test_read_only(self):
        def hosts(body):
            if b"GetHostNumberOfEntries" in body: return self.soap("GetHostNumberOfEntries", NewHostNumberOfEntries=2)
            i = int(body.split(b"<NewIndex>")[1].split(b"<")[0])
            return [self.soap("GetGenericHostEntry", NewIPAddress="192.168.178.20", NewMACAddress="AA:BB:CC:DD:EE:01", NewHostName="laptop", NewActive=1),
                    self.soap("GetGenericHostEntry", NewIPAddress="", NewMACAddress="", NewHostName="old", NewActive=0)][i]
        f = self.use(Fake(http={("GET", "/tr64desc.xml"): self.DESC,
                                ("POST", "/upnp/control/deviceinfo"): self.soap("GetInfo", NewModelName="FRITZ!Box 7590", NewSoftwareVersion="154.07.57", NewUpTime=3600),
                                ("POST", "/upnp/control/hosts"): hosts}))
        d = R.FritzBox({"BRAND": "fritzbox", "HOST": "192.168.178.1", "USER": "ai", "PASS": "pw"})
        self.assertEqual(d.status(), {"model": "FRITZ!Box 7590", "version": "154.07.57", "uptime_s": 3600})
        self.assertEqual(d.clients(), [{"mac": MAC, "ip": "192.168.178.20", "name": "laptop", "online": True}])
        post = [c for c in f.calls if c[0] == "POST"][0]
        self.assertEqual(post[3]["SoapAction"], "urn:dslforum-org:service:DeviceInfo:1#GetInfo")
        self.assertEqual(post[4], ("ai", "pw"))                                          # digest login
        with self.assertRaises(R.RouterError): d.block_mac(MAC)


class UniFiT(Base):
    def test_read_only(self):
        f = self.use(Fake(http={("GET", "/proxy/network/integration/v1/sites"): {"data": [{"id": "88f7af54-98f8-306a-a1c7-c9349722b1f6", "name": "Default"}]},
                                ("GET", "/proxy/network/integration/v1/sites/88f7af54-98f8-306a-a1c7-c9349722b1f6/clients?limit=200"):
                                    {"data": [{"macAddress": MAC, "ipAddress": "192.168.1.30", "name": "Pixel", "type": "WIRELESS"}]}}))
        d = R.UniFi({"BRAND": "unifi", "HOST": "192.168.1.1", "KEY": "abc"})
        self.assertEqual(d.clients()[0]["name"], "Pixel")
        self.assertEqual(f.calls[0][3]["X-API-KEY"], "abc")
        with self.assertRaises(R.RouterError): d.block_mac(MAC)


class OmadaT(Base):
    def test_clients_block(self):
        ok = lambda r: {"errorCode": 0, "msg": "Success.", "result": r}
        f = self.use(Fake(http={
            ("POST", "/openapi/authorize/token?grant_type=client_credentials"): ok({"accessToken": "AT-1", "tokenType": "bearer", "expiresIn": 7200}),
            ("GET", "/openapi/v1/cid123/sites?page=1&pageSize=100"): ok({"data": [{"siteId": "63a1b2c3", "name": "Home"}]}),
            ("GET", "/openapi/v1/cid123/sites/63a1b2c3/clients?page=1&pageSize=1000"): ok({"data": [{"mac": "AA-BB-CC-DD-EE-01", "ip": "192.168.1.40", "name": "tablet"}]}),
            ("POST", "/openapi/v1/cid123/sites/63a1b2c3/clients/AA-BB-CC-DD-EE-01/block"): ok(None),
            ("POST", "/openapi/v1/cid123/sites/63a1b2c3/clients/AA-BB-CC-DD-EE-01/unblock"): ok(None)}))
        d = R.Omada({"BRAND": "omada", "HOST": "192.168.1.2", "OMADAC": "cid123", "CLIENT_ID": "id", "CLIENT_SECRET": "sec"})
        self.assertEqual(d.clients(), [{"mac": MAC, "ip": "192.168.1.40", "name": "tablet", "online": True}])
        d.block_mac(MAC); d.unblock_mac(MAC)
        posts = [c for c in f.calls if c[0] == "POST"]
        self.assertEqual(posts[0][2], {"omadacId": "cid123", "client_id": "id", "client_secret": "sec"})
        self.assertTrue(all(c[3].get("Authorization") == "AccessToken=AT-1" for c in f.calls[1:]))
        self.assertTrue(posts[-1][1].endswith("/clients/AA-BB-CC-DD-EE-01/unblock"))

    def test_error_code(self):
        self.use(Fake(http={("POST", "/openapi/authorize/token?grant_type=client_credentials"): {"errorCode": -44112, "msg": "Invalid client"}}))
        with self.assertRaises(R.RouterError): R.Omada({"HOST": "192.168.1.2", "OMADAC": "c", "CLIENT_ID": "i", "CLIENT_SECRET": "s"}).clients()


class RouterAdminT(unittest.TestCase):
    """router-admin save/show with temp files (main() itself refuses to run unless root)."""
    def setUp(self):
        import importlib.machinery
        ld = importlib.machinery.SourceFileLoader("router_admin", os.path.join(NETDIR, "router-admin"))
        sp = importlib.util.spec_from_loader("router_admin", ld); self.A = importlib.util.module_from_spec(sp); ld.exec_module(self.A)
        t = tempfile.mkdtemp(); RR = self.A.R
        RR.ENV_FILE, RR.LEGACY_ENV, RR.PIN_FILE = f"{t}/router.env", f"{t}/legacy.env", f"{t}/pins.json"

    def test_rejects_bad_input(self):
        S = self.A.save
        for bad in ({"brand": "nope", "host": "192.168.1.1"}, {"brand": "mikrotik", "host": "8.8.8.8"},
                    {"brand": "mikrotik", "host": "evil.com"}, {"brand": "mikrotik", "host": "192.168.1.1", "port": "22"},
                    {"brand": "openwrt", "host": "192.168.1.1", "user": "root; reboot"},
                    {"brand": "mikrotik", "host": "192.168.1.1", "pass": 'a"b'}, {"brand": "mikrotik", "host": "192.168.1.1", "pass": "a\nBRAND=x"},
                    {"brand": "opnsense", "host": "192.168.1.1", "key": "k$(id)"}, {"brand": "mikrotik"}):
            with self.assertRaises(self.A.R.RouterError, msg=str(bad)): S(bad)
        self.assertFalse(os.path.exists(self.A.R.ENV_FILE))

    def test_save_keeps_secret_and_show_hides_it(self):
        self.A.save({"brand": "mikrotik", "host": "192.168.88.1", "user": "ai", "pass": "S3cret pw"})
        self.A.save({"brand": "mikrotik", "host": "192.168.88.1", "user": "ai2", "pass": ""})       # empty = keep
        txt = open(self.A.R.ENV_FILE).read()
        self.assertIn("PASS=S3cret pw\n", txt); self.assertIn("USER=ai2\n", txt)
        self.assertEqual(oct(os.stat(self.A.R.ENV_FILE).st_mode & 0o777), "0o600")
        shown = json.dumps(self.A.show())
        self.assertNotIn("S3cret", shown); self.assertIn('"PASS": true', shown)
        self.A.save({"brand": "openwrt", "host": "router.lan", "user": "root"})                    # other type: secret dropped
        self.assertNotIn("PASS", open(self.A.R.ENV_FILE).read())


class TestAll(Base):
    def test_test_never_raises(self):
        self.use(Fake())        # every request fails
        for b, cls in R.DRIVERS.items():
            res = cls({"BRAND": b, "HOST": "192.168.1.1", "USER": "u", "PASS": "p", "KEY": "k", "SECRET": "s",
                       "OMADAC": "o", "CLIENT_ID": "i", "CLIENT_SECRET": "c"}).test()
            self.assertIn("ok", res, b)


if __name__ == "__main__":
    unittest.main(verbosity=1)

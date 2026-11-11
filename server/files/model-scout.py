#!/usr/bin/python3 -I
"""Weekly 'better model finder'. Looks at what's trending on Hugging Face (GGUF), keeps the ones that fit
your GPUs and that you don't have yet, saves them for the AI Control Panel (Models tab) and tells you on Telegram.
Nothing is downloaded automatically - you choose in the panel. Runs as the owner from a systemd timer."""
import json, os, re, time, urllib.request, urllib.parse

OUT = "/opt/ai/suggestions.json"
CFG = "/opt/ai/llama-swap.yaml"
TG = "/opt/ai/secrets/telegram.env"
def _conf(k, d):
    try:
        for l in open("/opt/ai/ai.conf"):
            if l.startswith(k + "="): return float(l.split("=", 1)[1])
    except Exception: pass
    return d
FIT_SOLO, FIT_POOL = _conf("SOLO_GB", 4.5), _conf("POOL_GB", 13)   # GB - server GPU alone / all GPUs together
SKIP = re.compile(r"(abliterat|uncensor|nsfw|erotic|roleplay|rp-|merge|test|tiny-random|draft|embed|rerank|vae|lora|mmproj)", re.I)
PREFERRED = ["Q4_K_M", "Q4_K_XL", "IQ4_XS", "MXFP4", "Q4_0", "Q5_K_M", "Q3_K_M", "Q3_K_XL"]


def hf(path):
    req = urllib.request.Request("https://huggingface.co" + path, headers={"User-Agent": "home-ai-model-scout"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def have():
    try: txt = open(CFG).read().lower()
    except Exception: return set()
    return set(os.path.basename(m) for m in re.findall(r"-m (\S+\.gguf)", txt))


def candidates():
    seen, out = set(), []
    for q in ("sort=trendingScore", "sort=likes7d", "sort=downloads"):
        try:
            rows = hf(f"/api/models?filter=gguf&{q}&direction=-1&limit=60")
        except Exception:
            continue
        for r in rows:
            if r["id"] not in seen and not SKIP.search(r["id"]):
                seen.add(r["id"]); out.append(r)
        if len(out) >= 40: break
    return out


def pick(repo):
    tree = hf(f"/api/models/{urllib.parse.quote(repo)}/tree/main")
    files = [t for t in tree if t.get("path", "").endswith(".gguf") and "/" not in t["path"]]
    vision = any("mmproj" in t["path"].lower() for t in files)
    files = [t for t in files if "mmproj" not in t["path"].lower() and not re.search(r"-0000[2-9]-of-", t["path"])]
    for qn in PREFERRED:
        for t in files:
            if qn.lower() in t["path"].lower() and "-of-" not in t["path"]:
                gb = round(t.get("size", 0) / 2**30, 1)
                if 0.3 < gb <= FIT_POOL:
                    return {"file": t["path"], "gb": gb, "mode": "solo" if gb <= FIT_SOLO else "pool", "vision": vision}
    return None


def notify(text):
    c = {}
    try:
        for l in open(TG):
            if "=" in l: k, v = l.strip().split("=", 1); c[k] = v.strip('"')
    except FileNotFoundError: return
    if c.get("TOKEN") and c.get("CHAT_ID"):
        try:
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{c['TOKEN']}/sendMessage",
                data=json.dumps({"chat_id": c["CHAT_ID"], "text": text[:4000]}).encode(), headers={"Content-Type": "application/json"}), timeout=15)
        except Exception: pass


def main():
    mine = have()
    old = {}
    try: old = {s["repo"]: s for s in json.load(open(OUT)).get("items", [])}
    except Exception: pass
    items = []
    for r in candidates():
        if len(items) >= 10: break
        try: f = pick(r["id"])
        except Exception: continue
        if not f or f["file"].lower() in mine: continue
        items.append({"repo": r["id"], **f, "downloads": r.get("downloads"), "likes": r.get("likes"),
                      "updated": (r.get("lastModified") or r.get("createdAt") or "")[:10],
                      "fits": "server GPU alone" if f["mode"] == "solo" else "all GPUs together"})
        time.sleep(0.3)
    json.dump({"checked": time.strftime("%Y-%m-%d %H:%M"), "items": items}, open(OUT, "w"), indent=1)
    new = [i for i in items if i["repo"] not in old]
    if new:
        notify("🧠 New models that fit your GPUs:\n" + "\n".join(f"• {i['repo']} ({i['gb']} GB, {i['fits']})" for i in new[:6]) +
               "\n\nAdd any of them in the control panel -> Models (one click).")
    print(f"{len(items)} suggestions saved, {len(new)} new.")


if __name__ == "__main__":
    main()

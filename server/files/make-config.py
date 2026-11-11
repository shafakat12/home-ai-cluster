#!/usr/bin/env python3
"""Creates /opt/ai/llama-swap.yaml (model menu), /opt/ai/download-list.txt (files to fetch) and
/opt/ai/router.json (which models the virtual 'team' and 'auto' models use) for a starter preset.
Usage: make-config.py <small|standard|full|none> <models_dir> [--force]
An existing llama-swap.yaml is never overwritten without --force (it may contain models you added)."""
import json, os, sys

preset, md = sys.argv[1], sys.argv[2].rstrip("/")
force = "--force" in sys.argv
CFG, DL, RJ = "/opt/ai/llama-swap.yaml", "/opt/ai/download-list.txt", "/opt/ai/router.json"

MODELS = {
    "qwen3.5-4b": dict(mode="solo", repo="unsloth/Qwen3.5-4B-GGUF", file="Qwen3.5-4B-UD-Q4_K_XL.gguf",
                       mmproj=("mmproj-F16.gguf", "Qwen3.5-4B-mmproj-F16.gguf"),
                       args="-c 32768 -ctk q8_0 -ctv q8_0 --jinja", aliases=["fast", "small"], ttl=3600),
    "gpt-oss-20b": dict(mode="pool", repo="ggml-org/gpt-oss-20b-GGUF", file="gpt-oss-20b-MXFP4.gguf",
                        args="-c 65536 --jinja --temp 1.0 --top-p 1.0", aliases=["coder", "agent"], ttl=1800),
    "gemma-4-12b": dict(mode="remote", repo="unsloth/gemma-4-12B-it-qat-GGUF", file="gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
                        mmproj=("mmproj-F16.gguf", "gemma-4-12B-mmproj-F16.gguf"),
                        args="-c 32768 -ctk q8_0 -ctv q8_0 --jinja", aliases=["chat", "vision"], ttl=3600),
}
PRESETS = {"none": [], "small": ["qwen3.5-4b"], "standard": ["qwen3.5-4b", "gpt-oss-20b"],
           "full": ["qwen3.5-4b", "gpt-oss-20b", "gemma-4-12b"]}
names = PRESETS[preset]

# ---- router (team = several models answer, a judge merges; auto = picks a model per message)
if "gemma-4-12b" in names:
    router = {"team": ["qwen3.5-4b", "gemma-4-12b"], "judge": "gemma-4-12b", "classifier": "qwen3.5-4b",
              "auto": {"vision": "gemma-4-12b", "code": "gpt-oss-20b", "deep": "gemma-4-12b", "quick": "qwen3.5-4b"}}
elif "gpt-oss-20b" in names:
    router = {"team": ["qwen3.5-4b", "gpt-oss-20b"], "judge": "gpt-oss-20b", "classifier": "qwen3.5-4b",
              "auto": {"vision": "qwen3.5-4b", "code": "gpt-oss-20b", "deep": "gpt-oss-20b", "quick": "qwen3.5-4b"}}
elif names:
    router = {"team": [names[0]], "judge": names[0], "classifier": names[0],
              "auto": {k: names[0] for k in ("vision", "code", "deep", "quick")}}
else:
    router = None

if os.path.exists(CFG) and not force:
    print(f"{CFG} already exists - kept as it is (use --force to rebuild it).")
else:
    L = ["# llama-swap: one API address for all models; starts/stops the right model on demand.",
         "#   solo = server GPU only   pool = all GPUs together   remote = GPU worker PCs only",
         "#   team / auto = virtual models from router.py.  Add models in the control panel (Models tab).",
         "healthCheckTimeout: 1800", "logLevel: info", "startPort: 10001", "", "groups:"]
    small = [n for n in names if MODELS[n]["mode"] != "pool"]
    big = [n for n in names if MODELS[n]["mode"] == "pool"]
    if small:
        L += ['  # small models stay loaded together', '  "duo":', "    swap: false", "    exclusive: true",
              "    members: [" + ", ".join(f'"{n}"' for n in small) + "]"]
    if big:
        L += ['  # big models use ALL GPUs, so they pause the small ones while they run', '  "big":', "    swap: true",
              "    exclusive: true", "    members: [" + ", ".join(f'"{n}"' for n in big) + "]"]
    if router:
        L += ['  # the router is always available and never unloads other models', '  "router":', "    persistent: true",
              "    swap: false", "    exclusive: false", '    members: ["team", "auto"]']
    L += ["", "models:"]
    for n in names:
        m = MODELS[n]
        mm = f"\n      --mmproj {md}/{m['mmproj'][1]}" if m.get("mmproj") else ""
        L += [f'  "{n}":', "    cmd: >", f"      /opt/ai/bin/llama-run.sh {m['mode']} ${{PORT}}", f"      -m {md}/{m['file']}{mm}",
              f"      {m['args']}", "    aliases: [" + ", ".join(f'"{a}"' for a in m["aliases"]) + "]", f"    ttl: {m['ttl']}", ""]
    if router:
        for v in ("team", "auto"):
            L += [f'  "{v}":', "    cmd: python3 /opt/ai/bin/router.py --port ${PORT}", "    checkEndpoint: /health", ""]
    if not names:
        L += ['  # no starter models - add some in the control panel', '  "placeholder":', "    cmd: sleep infinity",
              "    proxy: http://127.0.0.1:9", "    unlisted: true"]
    open(CFG, "w").write("\n".join(L) + "\n")
    print(f"wrote {CFG} with: {', '.join(names) or 'no models'}")
    if router: json.dump(router, open(RJ, "w"), indent=1); print(f"wrote {RJ}")

with open(DL, "w") as f:
    for n in names:
        m = MODELS[n]
        f.write(f"{m['repo']} {m['file']} {m['file']}\n")
        if m.get("mmproj"): f.write(f"{m['repo']} {m['mmproj'][0]} {m['mmproj'][1]}\n")
print(f"download list: {sum(1 for _ in open(DL))} files")

#!/usr/bin/env bash
# add-model.sh <name> <hf-repo> <quant> <solo|pool> [context]   -> downloads a GGUF model and adds it to the menu
# add-model.sh --remove <name>                                  -> removes it (and its files)
# add-model.sh --list                                           -> shows installed models
set -euo pipefail
CFG=/opt/ai/llama-swap.yaml; LOGD=/opt/ai/logs
conf(){ grep -oP "^$1=\K.*" /opt/ai/ai.conf 2>/dev/null || true; }
MD=$(conf MODELS_DIR); MD=${MD:-/opt/ai/models}
SOLO_GB=$(conf SOLO_GB); SOLO_GB=${SOLO_GB:-4.5}; POOL_GB=$(conf POOL_GB); POOL_GB=${POOL_GB:-13}
if [ "${1:-}" = "--list" ]; then
  awk '/^[^ ]/{m=($0 ~ /^models:/)} m && /^  "[^"]+":/' "$CFG" | tr -d ' ":'; echo; ls -lh "$MD" | awk 'NR>1{print "  "$5"  "$9}'; exit 0
fi
if [ "${1:-}" = "--remove" ]; then
  N=${2:?name}
  case "$N" in team|auto) echo "$N is built in and cannot be removed"; exit 1;; esac
  awk -v n="  \"$N\":" '/^models:/{m=1} m && $0==n{f=1} END{exit !f}' "$CFG" || { echo "No model named $N"; exit 1; }
  cp "$CFG" "$CFG.bak"
  # the model's own block (to find its files), then the config without it
  BLOCK=$(awk -v n="  \"$N\":" '/^[^ ]/{m=($0 ~ /^models:/); on=0} m && $0==n{on=1;next} on && /^  "/{on=0} on' "$CFG.bak")
  awk -v n="  \"$N\":" '/^[^ ]/{m=($0 ~ /^models:/); skip=0} m && $0==n{skip=1;next} skip && /^  "/{skip=0} !skip' "$CFG.bak" > "$CFG"
  # take it out of model groups (and drop groups left empty) and out of team/auto (router.json)
  python3 - "$CFG" "$N" "${ROUTER_JSON:-/opt/ai/router.json}" <<'PYEOF'
import json, os, re, sys
cfg, n, rj = sys.argv[1:4]
s = open(cfg).read()
head, sep, body = s.partition("\nmodels:\n")
def drop(m):
    items = [x.strip() for x in m.group(2).split(",") if x.strip() and x.strip().strip('"') != n]
    return m.group(1) + "[" + ", ".join(items) + "]"
head = re.sub(r"(members:\s*)\[([^\]]*)\]", drop, head)
head = re.sub(r'(?m)^  "[^"]+":\n(?:    .*\n)*?    members: \[\]\n', "", head)
open(cfg, "w").write(head + sep + body)
left = [x for x in re.findall(r'(?m)^  "([^"]+)":', body) if x not in ("team", "auto", "placeholder")]
if os.path.exists(rj) and left:
    r = json.load(open(rj)); fb = left[0]
    r["team"] = [x for x in r.get("team", []) if x != n] or [fb]
    for k in ("judge", "classifier"):
        if r.get(k) == n: r[k] = fb
    r["auto"] = {k: (fb if v == n else v) for k, v in r.get("auto", {}).items()}
    json.dump(r, open(rj, "w"), indent=1)
PYEOF
  # delete its model files - only inside the models folder and only if no other model still uses them
  for f in $(echo "$BLOCK" | grep -oE -- '(-m|--mmproj) +[^ ]+\.gguf' | awk '{print $2}'); do
    case "$f" in "$MD"/*) ;; *) continue;; esac
    if grep -qF -- "$f" "$CFG"; then echo "kept $(basename "$f") (another model uses it)"; else rm -f "$f" && echo "deleted $(basename "$f")"; fi
  done
  echo "Removed $N"; exit 0
fi
NAME=${1:?name}; REPO=${2:?repo}; QUANT=${3:?quant}; MODE=${4:-pool}; CTX=${5:-32768}
[[ "$NAME" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || { echo "Name must be lowercase letters, numbers, - . _ (no spaces)"; exit 1; }
[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || { echo "Repo must look like owner/name"; exit 1; }
[[ "$QUANT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Bad quant"; exit 1; }
[[ "$MODE" == solo || "$MODE" == pool ]] || { echo "Mode must be solo or pool"; exit 1; }
[[ "$CTX" =~ ^[0-9]+$ ]] || CTX=32768
awk -v n="  \"$NAME\":" '/^models:/{m=1} m && $0==n{f=1} END{exit !f}' "$CFG" && { echo "A model called $NAME already exists"; exit 1; }
case "$NAME" in team|auto|duo|big|router|placeholder) echo "That name is reserved, pick another"; exit 1;; esac

FILES=$(curl -fsSL "https://huggingface.co/api/models/$REPO/tree/main" | jq -r '.[] | select(.path|test("\\.gguf$")) | "\(.size) \(.path)"') \
  || { echo "Could not find repo $REPO on Hugging Face"; exit 1; }
MAIN=$(echo "$FILES" | grep -vi mmproj | grep -iF -- "$QUANT" | sort -n | head -1)
if [ -z "$MAIN" ]; then
  echo "No single-file $QUANT model in $REPO. Files available:"
  echo "$FILES" | awk '{printf "  %5.1f GB  %s\n",$1/1073741824,$2}'; exit 1
fi
MM=$(echo "$FILES" | grep -i mmproj | grep -iE 'f16|bf16' | head -1); [ -n "$MM" ] || MM=$(echo "$FILES" | grep -i mmproj | head -1 || true)
SIZE=$(echo "$MAIN" | awk '{printf "%.1f",$1/1073741824}')
LIMIT=$([ "$MODE" = solo ] && echo "$SOLO_GB" || echo "$POOL_GB")
echo "Model file: ${MAIN#* }  ($SIZE GB)   vision: $([ -n "$MM" ] && echo yes || echo no)"
awk -v s="$SIZE" -v l="$LIMIT" 'BEGIN{exit !(s>l)}' && echo "WARNING: $SIZE GB is big for '$MODE' mode (about $LIMIT GB fits). It will still run, but slower (part goes to system RAM)."
FREE=$(df --output=avail -BG "$MD" | tail -1 | tr -dc 0-9); echo "Free disk: ${FREE} GB"

LOG=$LOGD/add-$NAME.log
setsid nohup bash -c "
  set -e
  echo 'Downloading $NAME ...'
  curl -fL --retry 10 -C - -o '$MD/$NAME.gguf.part' 'https://huggingface.co/$REPO/resolve/main/${MAIN#* }?download=true'
  mv '$MD/$NAME.gguf.part' '$MD/$NAME.gguf'
  MMARG=''
  if [ -n '$MM' ]; then
    curl -fL --retry 10 -C - -o '$MD/$NAME-mmproj.gguf' 'https://huggingface.co/$REPO/resolve/main/${MM#* }?download=true'
    MMARG='--mmproj $MD/$NAME-mmproj.gguf'
  fi
  printf '\n  \"%s\":\n    cmd: >\n      /opt/ai/bin/llama-run.sh %s \${PORT}\n      -m %s %s\n      -c %s --jinja\n    ttl: 1800\n' '$NAME' '$MODE' '$MD/$NAME.gguf' \"\$MMARG\" '$CTX' >> '$CFG'
  echo 'READY: $NAME is now in the model list (refresh Open WebUI).'
" > "$LOG" 2>&1 < /dev/null &
echo "Download started in the background. The model appears in Open WebUI when finished."
echo "Progress: control panel (Models tab), or on the server: tail -f $LOG"

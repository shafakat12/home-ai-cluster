#!/usr/bin/env bash
# model-download.sh <name> - downloads (or continues) a model that add-model.sh started, then adds it to the menu.
# Started in the background by add-model.sh; "add-model.sh --pause/--resume <name>" stops and continues it.
set -uo pipefail
LOGD=/opt/ai/logs; CFG=/opt/ai/llama-swap.yaml
N=${1:?name}; [[ "$N" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || { echo "FAILED: bad name"; exit 1; }
J=$LOGD/add-$N.job
[ -f "$J" ] || { echo "FAILED: no download job for $N"; exit 1; }
REPO=""; MAINF=""; MMF=""; MODE=pool; CTX=32768; MD=""
# shellcheck source=/dev/null
. "$J"
echo $$ > "$LOGD/add-$N.pid"
dl() {   # dl <target file> <file in the repo>; -C - continues a .part file from where it stopped
  curl -fL --retry 10 --retry-delay 5 -C - -o "$1.part" "https://huggingface.co/$REPO/resolve/main/$2?download=true" && mv "$1.part" "$1"
}
stop() { rm -f "$LOGD/add-$N.pid"; printf '\nFAILED: %s (press Continue to try again)\n' "$1"; exit 1; }
[ -f "$MD/$N.gguf" ] || dl "$MD/$N.gguf" "$MAINF" || stop "the download stopped"
MMARG=""
if [ -n "$MMF" ]; then
  [ -f "$MD/$N-mmproj.gguf" ] || dl "$MD/$N-mmproj.gguf" "$MMF" || stop "the picture-support file did not download"
  MMARG="--mmproj $MD/$N-mmproj.gguf"
fi
if ! grep -qF "  \"$N\":" "$CFG"; then
  # shellcheck disable=SC2016
  printf '\n  "%s":\n    cmd: >\n      /opt/ai/bin/llama-run.sh %s ${PORT}\n      -m %s %s\n      -c %s --jinja\n    ttl: 1800\n' \
    "$N" "$MODE" "$MD/$N.gguf" "$MMARG" "$CTX" >> "$CFG"
fi
rm -f "$J" "$LOGD/add-$N.pid"
printf '\nREADY: %s is now in the model list (refresh Open WebUI).\n' "$N"

#!/usr/bin/env bash
# Downloads the starter model files listed in /opt/ai/download-list.txt (resumable, safe to re-run).
# Line format: <huggingface-repo> <file-in-repo> <local-name>
MD=$(grep -oP '^MODELS_DIR=\K.*' /opt/ai/ai.conf 2>/dev/null || echo /opt/ai/models)
cd "$MD" || exit 1
while read -r repo file out; do
  [ -z "$repo" ] && continue
  if [ -f "$out" ] && [ ! -f "$out.part" ]; then echo "[have] $out"; continue; fi
  echo "[download] $out  from $repo"
  touch "$out.part"
  if curl -fL --retry 10 --retry-delay 5 -C - -o "$out" "https://huggingface.co/$repo/resolve/main/$file?download=true"; then
    rm -f "$out.part"; echo "[done] $out"
  else
    echo "[FAILED] $out (run again to resume)"
  fi
done < /opt/ai/download-list.txt
echo "ALL-DOWNLOADS-FINISHED $(date)"
ls -lh "$MD"

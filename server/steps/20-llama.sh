#!/usr/bin/env bash
# llama.cpp: picks the newest release that has a build for this server AND Windows worker builds,
# so every PC runs exactly the same version (required for GPU pooling).
source "$REPO/server/lib.sh"; set -uo pipefail
API=https://api.github.com/repos/ggml-org/llama.cpp/releases
if [ "$HAS_NVIDIA" = yes ] && nvidia-smi >/dev/null 2>&1; then FLAV=cuda; else FLAV=vulkan; fi
info "server build: $FLAV"

SEL=$(curl -fsSL "$API?per_page=20" | jq -r --arg f "$FLAV" '
  [ .[] | {tag: .tag_name, pre: .prerelease,
      lin: (if $f=="cuda" then ([.assets[]|select(.name|test("^llama-.*-bin-ubuntu-cuda-12[0-9.]*-x64\\.tar\\.gz$"))|.browser_download_url][0])
            else ([.assets[]|select(.name|test("^llama-.*-bin-ubuntu-vulkan-x64\\.tar\\.gz$"))|.browser_download_url][0]) end),
      rt:  (if $f=="cuda" then ([.assets[]|select(.name|test("^cudart-llama-.*-bin-ubuntu-cuda-12[0-9.]*-x64\\.tar\\.gz$"))|.browser_download_url][0]) else "none" end),
      win: ([.assets[]|select(.name|test("bin-win-(cuda-1[23][0-9.]*|vulkan)-x64\\.zip$"))|.name][0]) }
    | select(.lin != null and .rt != null and .win != null) ]
  | (map(select(.pre|not)) + .)[0] | "\(.tag) \(.lin) \(.rt)"')
read -r TAG URL RT <<<"$SEL"
[ -n "${TAG:-}" ] && [ "$TAG" != null ] || die "no suitable llama.cpp release found (GitHub reachable?)"
info "llama.cpp $TAG"

D=/opt/ai/llama.cpp/$TAG
if [ ! -x "$D/llama-server" ]; then
  mkdir -p "$D"
  for u in "$URL" "$RT"; do
    [ "$u" = none ] && continue
    info "downloading $(basename "$u")..."
    curl -fL --retry 5 -s -o /tmp/llama-dl.tgz "$u" || die "download failed: $u"
    FIRST=$(tar -tzf /tmp/llama-dl.tgz 2>/dev/null | head -1 || true)
    if [[ "$FIRST" == */* ]]; then tar -xzf /tmp/llama-dl.tgz -C "$D" --strip-components=1; else tar -xzf /tmp/llama-dl.tgz -C "$D"; fi
    rm -f /tmp/llama-dl.tgz
  done
fi
ln -sfn "$D" /opt/ai/llama.cpp/current
chown -R "$ADMIN:$ADMIN" /opt/ai/llama.cpp
echo "$TAG" > /opt/ai/llama-version.txt; cp /opt/ai/llama-version.txt /opt/ai/worker-kit/
ldd "$D/llama-server" | grep "not found" && warn "missing libraries (see above)" || info "libraries ok"
info "devices seen: $("$D/llama-server" --list-devices 2>&1 | grep -E '^ +[A-Za-z]+[0-9]+:' | sed 's/^ *//' | paste -sd ';' -)"

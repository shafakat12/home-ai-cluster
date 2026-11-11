#!/usr/bin/env bash
# Private web search (SearXNG) on 127.0.0.1:8888 - only reachable from the server itself (Open WebUI uses it).
# It forwards searches to several engines and merges the results; no account, no tracking.
source "$REPO/server/lib.sh"; set -uo pipefail
[ "${INSTALL_WEB_SEARCH:-yes}" = yes ] || { info "skipped (INSTALL_WEB_SEARCH=no)"; exit 0; }
install -d -m 755 /opt/ai/searxng
F=/opt/ai/searxng/settings.yml
KEY=$(grep -oP 'secret_key:\s*"\K[^"]+' "$F" 2>/dev/null || openssl rand -hex 32)
# Google often blocks automated searches from home servers, so several engines are used;
# each result shows which engine it came from.
cat > "$F" <<YML
use_default_settings:
  engines:
    keep_only:
      - brave
      - startpage
      - google
      - bing
      - duckduckgo
      - wikipedia
      - google news
      - bing news
      - google images
      - bing images
server:
  secret_key: "$KEY"
  limiter: false
  image_proxy: false
search:
  safe_search: 0
  formats:
    - html
    - json
engines:
  - name: brave
    weight: 2
    disabled: false
  - name: startpage
    weight: 2
    disabled: false
  - name: google
    weight: 2
YML
chmod 644 "$F"
docker rm -f searxng >/dev/null 2>&1
docker run -d --name searxng --restart unless-stopped -p 127.0.0.1:8888:8080 \
  -v /opt/ai/searxng:/etc/searxng:rw searxng/searxng:latest >/dev/null || die "could not start SearXNG"
for _ in $(seq 1 40); do curl -sf -m 5 -o /dev/null http://127.0.0.1:8888/healthz && break; sleep 3; done
sleep 3
R=$(curl -s -m 40 "http://127.0.0.1:8888/search?q=open+source+ai&format=json")
info "test search: $(echo "$R" | jq -r '"\(.results|length) results; engines not answering: \(.unresponsive_engines|map(.[0])|join(", "))"' 2>/dev/null || echo 'no answer yet')"
info "Open WebUI: web search is preset on a NEW install. If it is off: Admin Panel > Settings > Web Search >"
info "  engine searxng, query URL  http://127.0.0.1:8888/search?q=<query>"

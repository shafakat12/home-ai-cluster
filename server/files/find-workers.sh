#!/usr/bin/env bash
# Prints a comma-separated list of reachable GPU workers (ip:50052) for llama-server --rpc.
# 1) every PC on the local /24 network that has the worker port open (auto-discovery)
# 2) plus any extra entries in /opt/ai/workers.txt (one ip[:port] per line, # comments)
PORT=50052
SELF=$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
BASE=${SELF%.*}
tmp=$(mktemp)
for i in $(seq 1 254); do
  ip="$BASE.$i"; [ "$ip" = "$SELF" ] && continue
  ( timeout 0.5 bash -c "</dev/tcp/$ip/$PORT" 2>/dev/null && echo "$ip:$PORT" >> "$tmp" ) &
done
wait
if [ -f /opt/ai/workers.txt ]; then
  grep -vE '^\s*(#|$)' /opt/ai/workers.txt | while read -r w; do
    [[ "$w" == *:* ]] || w="$w:$PORT"
    h=${w%:*}; p=${w##*:}
    timeout 1 bash -c "</dev/tcp/$h/$p" 2>/dev/null && echo "$w" >> "$tmp"
  done
fi
sort -u -t. -k4,4n "$tmp" | paste -sd, -
rm -f "$tmp"

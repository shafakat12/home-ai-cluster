#!/usr/bin/env bash
# Starts llama-server for llama-swap.
#   llama-run.sh solo   <port> <args...>  -> server GPU only (small models)
#   llama-run.sh pool   <port> <args...>  -> server GPU + every online GPU worker (big models)
#   llama-run.sh remote <port> <args...>  -> ONLY the GPU worker PCs (leaves the server GPU free for another model)
#
# Respects the GPU choices made in the AI Control Panel (/opt/ai/gpu-policy.json):
#   use     = used by every model that wants it
#   standby = kept ready, only used by big pooled models (or when no "use" GPU is left)
#   off     = never used
MODE=$1; PORT=$2; shift 2
BIN=/opt/ai/llama.cpp/current
POL=/opt/ai/gpu-policy.json
# keep compiled GPU code between runs (first run of a model compiles it once)
export CUDA_CACHE_MAXSIZE=4294967296
ARGS=(--host 127.0.0.1 --port "$PORT" --threads 4 --metrics)
log(){ echo "[llama-run] $*" >&2; }

pol_server(){ jq -r '.server // "use"' "$POL" 2>/dev/null || echo use; }
pol_worker(){ jq -r --arg k "$1" '.workers[$k] // "use"' "$POL" 2>/dev/null || echo use; }

SPOL=$(pol_server); [ -n "$SPOL" ] || SPOL=use
USE=(); STBY=()
for w in $(/opt/ai/bin/find-workers.sh | tr ',' ' '); do
  case "$(pol_worker "$w")" in
    off) log "worker $w is OFF in the control panel - not used" ;;
    standby) STBY+=("$w") ;;
    *) USE+=("$w") ;;
  esac
done
join(){ local IFS=,; echo "$*"; }

# run only on the given workers (no server GPU)
remote_on(){
  local W; W=$(join "$@")
  ARGS+=(--rpc "$W")
  local DEVS; DEVS=$("$BIN/llama-server" --rpc "$W" --list-devices 2>/dev/null | awk -F: '/^ *RPC/{gsub(/ /,"",$1); print $1}' | paste -sd, -)
  if [ -n "$DEVS" ]; then ARGS+=(--device "$DEVS"); else ARGS+=(--device none); fi
  log "GPU workers only: $W (devices: ${DEVS:-none})"
}
server_only(){ log "server GPU only"; }
cpu_only(){ ARGS+=(--device none); log "no GPU allowed/online for this model - running on the server CPU (slow)"; }

case "$MODE" in
  solo)
    if [ "$SPOL" = use ]; then server_only
    elif [ ${#USE[@]} -gt 0 ]; then log "server GPU is $SPOL - moving this model to a worker"; remote_on "${USE[@]}"
    elif [ "$SPOL" = standby ]; then server_only
    elif [ ${#STBY[@]} -gt 0 ]; then remote_on "${STBY[@]}"
    else cpu_only; fi ;;
  remote)
    if [ ${#USE[@]} -gt 0 ]; then remote_on "${USE[@]}"
    elif [ ${#STBY[@]} -gt 0 ]; then remote_on "${STBY[@]}"
    elif [ "$SPOL" != off ]; then log "no worker available - using the server GPU instead"; server_only
    else cpu_only; fi ;;
  pool)
    ALLW=("${USE[@]}" "${STBY[@]}")
    if [ "$SPOL" = off ]; then
      if [ ${#ALLW[@]} -gt 0 ]; then remote_on "${ALLW[@]}"; else cpu_only; fi
    elif [ ${#ALLW[@]} -gt 0 ]; then
      ARGS+=(--rpc "$(join "${ALLW[@]}")"); log "pooled: server GPU + workers $(join "${ALLW[@]}")"
    else
      log "pooled mode but NO workers online/allowed - using server GPU + RAM only (slower)"
    fi ;;
esac
exec "$BIN/llama-server" "${ARGS[@]}" "$@"

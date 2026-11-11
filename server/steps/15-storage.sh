#!/usr/bin/env bash
# Storage: where the models (and backups) are kept - the system disk, a folder, one whole drive, or a RAID.
# Asks once during the first install (or uses STORAGE= from config.env). Change it later:  sudo ai-storage
source "$REPO/server/lib.sh"; set -uo pipefail
F=$REPO/server/files
install -d -m 755 -o root -g root /opt/ai/sbin
install -m 755 -o root -g root "$F/ai-storage" /opt/ai/sbin/
ln -sf /opt/ai/sbin/ai-storage /usr/local/sbin/ai-storage

OPTS=()
[ "${STORAGE_ERASE_OK:-no}" = yes ] && OPTS+=(--yes)
case "${STORAGE_BACKUPS:-auto}" in yes) OPTS+=(--backups);; no) OPTS+=(--no-backups);; esac
same(){ [ "$(realpath -m "$1")" = "$(realpath -m "$2")" ]; }

# config.env values are applied once, and again only when you edit them - so a later  sudo ai-storage  change sticks
if [ -n "${STORAGE:-}" ]; then
  [ -n "$CFG_MODELS_DIR" ] && warn "config.env has both STORAGE and MODELS_DIR - using STORAGE"
  /opt/ai/sbin/ai-storage apply "$STORAGE" "${OPTS[@]}" || exit 1
elif [ -n "$CFG_MODELS_DIR" ] && [ "$CFG_MODELS_DIR" != "$(aiconf MODELS_DIR_FROM_CONFIG)" ]; then
  same "$CFG_MODELS_DIR" "$MODELS_DIR" || /opt/ai/sbin/ai-storage folder "$CFG_MODELS_DIR" "${OPTS[@]}" || exit 1
  conf_set MODELS_DIR_FROM_CONFIG "$CFG_MODELS_DIR"
elif [ -t 0 ] && [ -z "$(aiconf STORAGE_ASKED)" ]; then
  /opt/ai/sbin/ai-storage menu --first-run "${OPTS[@]}" || warn "storage not changed - you can do it later:  sudo ai-storage"
fi
if [ -n "$CFG_BACKUP_DIR" ] && [ "$CFG_BACKUP_DIR" != "$(aiconf BACKUP_DIR_FROM_CONFIG)" ]; then
  same "$CFG_BACKUP_DIR" "$BACKUP_DIR" || /opt/ai/sbin/ai-storage backups "$CFG_BACKUP_DIR" || exit 1
  conf_set BACKUP_DIR_FROM_CONFIG "$CFG_BACKUP_DIR"
fi
conf_set STORAGE_ASKED 1
info "models are stored in $(aiconf MODELS_DIR)   (another drive or a RAID:  sudo ai-storage)"

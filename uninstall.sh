#!/usr/bin/env bash
# Removes the Home AI services from this server.
#   sudo bash uninstall.sh            stop + remove services (keeps /opt/ai with models, settings and backups)
#   sudo bash uninstall.sh --purge    also delete /opt/ai and the Open WebUI data (chats, accounts) - cannot be undone
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
UNITS="llama-swap open-webui ai-control-panel ai-worker-kit ai-model-download ai-model-scout.timer ai-model-scout ai-health-check.timer ai-health-check ai-backup.timer ai-backup"
for u in $UNITS; do systemctl disable --now "$u" >/dev/null 2>&1; rm -f "/etc/systemd/system/$u" "/etc/systemd/system/$u.service"; done
systemctl daemon-reload
docker rm -f searxng open-webui >/dev/null 2>&1
rm -f /etc/sudoers.d/home-ai
for p in 3000 8090 8091 8080; do
  for _ in 1 2 3 4 5 6 7 8 9 10; do        # remove every firewall rule for this port (max 10)
    n=$(ufw status numbered | grep " $p/tcp" | head -1 | sed 's/^\[ *\([0-9]*\)\].*/\1/')
    [ -n "$n" ] || break
    yes | ufw delete "$n" >/dev/null 2>&1 || break
  done
done
echo "Services removed."
MD=$(grep -oP '^MODELS_DIR=\K.*' /opt/ai/ai.conf 2>/dev/null); BD=$(grep -oP '^BACKUP_DIR=\K.*' /opt/ai/ai.conf 2>/dev/null)
if [ "${1:-}" = "--purge" ]; then
  echo "This deletes /opt/ai (settings) ${MD:+, the models in $MD}${BD:+, the backups in $BD} and all Open WebUI chats/accounts."
  read -r -p "Type DELETE to continue: " a
  if [ "$a" = DELETE ]; then
    for d in "$MD" "$BD"; do case "$d" in ""|/|/opt/ai/*) ;; *) rm -rf -- "$d";; esac; done
    rm -rf /opt/ai /var/lib/home-ai /usr/local/sbin/ai-storage; docker volume rm open-webui >/dev/null 2>&1; echo "Deleted."
  else echo "Kept."; fi
else
  echo "Kept /opt/ai (settings) and the models${MD:+ in $MD}. Run with --purge to delete them."
fi
grep -q '/mnt/ai-storage' /etc/fstab 2>/dev/null &&
  echo "Your storage drive/RAID stays mounted at /mnt/ai-storage* (in /etc/fstab) - remove that line yourself if you no longer want it."
true

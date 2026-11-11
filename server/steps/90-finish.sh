#!/usr/bin/env bash
# Summary: addresses, setup code, what to do next.
source "$REPO/server/lib.sh"; set -uo pipefail
systemctl is-active llama-swap open-webui ai-control-panel ai-worker-kit 2>/dev/null | paste -sd' ' - | sed 's/^/  services: /'
CODE=$(cat /opt/ai/secrets/setup-code 2>/dev/null)
cat <<EOF

  ============================================================================
   Home AI Cluster is installed.

   Chat (everyone at home):   http://$SERVER_IP:3000
       The FIRST account you create there becomes the Open WebUI admin.
   Control panel (admin):     http://$SERVER_IP:8091
EOF
[ -n "$CODE" ] && cat <<EOF
       First visit: create the admin password with this SETUP CODE:  $CODE
       (also saved in /opt/ai/secrets/setup-code; it is deleted once the password exists)
EOF
cat <<EOF

   Models are downloading in the background:  tail -f /opt/ai/logs/model-download.log
   Models are stored in $MODELS_DIR - another drive or a RAID: control panel > Storage
   Add a GPU PC:  open  http://$SERVER_IP:8090/add-this-pc.bat  on a Windows PC, or run on Linux:
                  curl -fsSL http://$SERVER_IP:8090/add-worker-linux.sh | bash
   Tip: reserve $SERVER_IP for this server in your router (DHCP reservation).
  ============================================================================
EOF

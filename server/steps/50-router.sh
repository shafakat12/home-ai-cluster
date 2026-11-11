#!/usr/bin/env bash
# Virtual models "team" (several models answer, one merges) and "auto" (picks a model per message).
# router.py is installed in step 30 and started by llama-swap on demand; this step just checks it.
source "$REPO/server/lib.sh"; set -uo pipefail
if [ -f /opt/ai/router.json ]; then
  info "router models: $(jq -c '{team, auto}' /opt/ai/router.json)"
  python3 -c "import ast;ast.parse(open('/opt/ai/bin/router.py').read())" && info "router ok"
else
  info "no router.json (no starter models) - 'team' and 'auto' are not in the menu."
fi

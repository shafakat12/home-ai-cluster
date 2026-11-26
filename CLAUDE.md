# Home AI Cluster - notes for Claude Code

Scripts that turn an Ubuntu server into a private home AI server: llama.cpp with GPU pooling over the
network (RPC), llama-swap, Open WebUI, SearXNG, a control panel, health checks and backups.
User-facing description: README.md and docs/.

## Layout
- `install.sh` runs the steps in `server/steps/NN-name.sh` in order (as root via sudo). Every step sources
  `server/lib.sh` (settings from config.example.env + config.env, auto-detected IP/network, helpers).
  Steps must stay **idempotent** (safe to re-run) and must not overwrite user data
  (`/opt/ai/llama-swap.yaml`, `/opt/ai/secrets`, models). Exit code 100 = "reboot needed".
- `server/files/` is installed to `/opt/ai/bin` (runs as the admin user) or `/usr/local/lib/home-ai` (root-owned
  helpers the control panel may run through `/etc/sudoers.d/home-ai` and `home-ai-net`; exact command lines only,
  keep that list minimal). `/opt/ai` belongs to the admin user, so nothing root runs or writes may live there
  (the old `/opt/ai/sbin` is removed by 90-finish). Root-run code must not follow links in folders other users own.
- Optional network watch (`server/steps/45-network.sh`, `INSTALL_NETWORK_WATCH`): `netctl` (requests that wait
  for approval), `net-scan` (timer), `net-exec` (carries out approved requests, Telegram buttons), `router-admin`
  (root helper for router settings) and `routers.py` (one driver class per router brand). Data in
  `/var/lib/home-ai/net`. Only OpenWrt/iStoreOS is tested on real hardware; mark untested brands as such.
  Validate every MAC/IP/host with strict regexes, only connect to home-network addresses on fixed ports, never
  build a shell command from user text, and only remove router rules tagged `ai_block`. Tests: `tests/test_routers.py`.
- Core preset models go to `CORE_DIR` (system disk, `/opt/ai/models-core` on new installs); models added later go
  to `MODELS_DIR`, which `ai-storage` can move to a data disk or RAID. `model-download.sh` does the downloading
  (pause/resume through a job file).
- `server/files/control-panel.py` is a single-file stdlib web app (port 8091): HTML/JS in `PAGE`, JSON API
  under `/api/`. POST requests need the `X-Panel: 1` header + login cookie. Escape every value shown in
  the page with `esc()`. The page refreshes every 0.5 s and only redraws changed HTML.
- `server/files/ai-storage` (installed as `sudo ai-storage`) moves models/backups and can ERASE drives to make
  a single drive or an mdadm RAID. Never run its `drive`, `raid` or `apply` commands (or `--yes`) on someone's
  machine unless they explicitly asked for exactly those drives. Use `--dry-run` to show what would happen.
  Test it with loop devices (`AI_STORAGE_TEST_LOOP=1`), never with real disks.
- `MODELS_DIR` / `BACKUP_DIR` live in `/opt/ai/ai.conf` after the first install; `server/lib.sh` reads them from there.
- `worker/` = GPU-PC installers. The server serves them on port 8090 with `__SERVER_IP__` replaced.
- No personal data in this repository: no real IPs, names, e-mails, serial numbers or tokens.

## Checks before committing
```bash
for f in install.sh uninstall.sh update-open-webui.sh server/lib.sh server/steps/*.sh server/files/*.sh server/files/ai-backup server/files/ai-storage worker/linux/*.sh; do bash -n "$f"; done
shellcheck -S error install.sh server/lib.sh server/steps/*.sh server/files/*.sh server/files/ai-storage worker/linux/*.sh   # if installed
python3 -m pyflakes server/files/*.py server/files/disk-health server/files/api-allow-sync server/files/netctl server/files/net-exec server/files/net-scan server/files/router-admin   # if installed
python3 -I tests/test_routers.py                                                            # router driver tests
# panel JavaScript: extract the <script> parts of PAGE and run  node --check  on them
```
Linux scripts use LF line endings, Windows scripts CRLF (`.gitattributes`). Keep shell scripts executable in git
(`git update-index --chmod=+x`), but docs always say `sudo bash install.sh` so it works without the bit.

## Publishing to GitHub
When asked to upload / publish / push this project to GitHub, run in the VS Code terminal (PowerShell):
```powershell
powershell -ExecutionPolicy Bypass -File tools\publish-to-github.ps1            # public repo "home-ai-cluster"
powershell -ExecutionPolicy Bypass -File tools\publish-to-github.ps1 -Private   # or a private repo
```
It installs Git + GitHub CLI with winget if missing, uses the GitHub no-reply e-mail as author (keeps the
real e-mail private), fills the GitHub username into the README, marks the Linux scripts executable,
commits, creates the repository and pushes. Running it again pushes new changes.
If it says "not logged in", the user must run `gh auth login` themselves (browser login) - never ask for
or handle their GitHub password or tokens.

#!/usr/bin/env bash
# Deploy the latest code. Run on the server as the coach user:
#   ~/study-coach/deploy/update.sh
# Tests run BEFORE the restart, so a broken commit never replaces a working bot.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m unittest -q
sudo systemctl restart study-coach
echo "Updated to $(git log -1 --format='%h %s')"

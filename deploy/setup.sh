#!/usr/bin/env bash
# One-off server setup on a fresh Ubuntu 24.04 VPS. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/ZakHustlin/study-coach/main/deploy/setup.sh | bash
# Safe to run again: every step checks before it acts.
set -euo pipefail
REPO="${REPO:-https://github.com/ZakHustlin/study-coach.git}"
APP=/home/coach/study-coach

echo "== packages and automatic security updates"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv git sqlite3 ufw unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== firewall: SSH only (the bot polls Telegram, so nothing else needs to come in)"
ufw allow OpenSSH
ufw --force enable

echo "== app user (no password, no sudo)"
id coach >/dev/null 2>&1 || adduser --disabled-password --gecos "" coach
sudo -u coach mkdir -p /home/coach/backups

echo "== let coach restart its own service (for deploy/update.sh), nothing else"
echo "coach ALL=(root) NOPASSWD: /usr/bin/systemctl restart study-coach, /usr/bin/systemctl status study-coach" \
  > /etc/sudoers.d/study-coach
chmod 440 /etc/sudoers.d/study-coach
visudo -cf /etc/sudoers.d/study-coach

echo "== code + virtualenv"
[ -d "$APP/.git" ] || sudo -u coach git clone "$REPO" "$APP"
sudo -u coach python3 -m venv "$APP/.venv"
sudo -u coach "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chmod +x "$APP/deploy/"*.sh

echo "== systemd units"
cp "$APP/deploy/study-coach.service" "$APP/deploy/study-coach-backup.service" \
   "$APP/deploy/study-coach-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now study-coach-backup.timer
systemctl enable study-coach        # started once the secrets are copied (DEPLOY.md step 4)

echo
echo "Done. Next: copy .env, credentials.json, token.json, gcal_state.json,"
echo "bot_state.json and coach.db into $APP (DEPLOY.md step 4)."

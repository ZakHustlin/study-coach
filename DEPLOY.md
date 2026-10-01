# Deploying to a VPS (step 6)

Target: a small Ubuntu 24.04 VPS (e.g. Hetzner CX22, about €4/month). The bot uses
long polling, so the server needs **no open ports except SSH**: it calls out to
Telegram, Google, DeepSeek and Anthropic; nothing calls in.

## 1. Create the server
- Ubuntu 24.04, smallest size, any EU location. Add your SSH public key when creating
  it (then the provider disables root password login).
- Note its IP address. Below it's `IP`.

## 2. Set it up (once)
```bash
ssh root@IP
curl -fsSL https://raw.githubusercontent.com/ZakHustlin/study-coach/main/deploy/setup.sh | bash
```
This installs Python, turns on automatic security updates and the firewall, creates a
`coach` user with no password or sudo (apart from restarting its own service), clones
the repo, builds the virtualenv and installs the systemd units.

## 3. Stop the Codespaces bot
Only one program may poll a bot token. Two at once gives
`telegram.error.Conflict` and each steals the other's messages. Stop `python bot.py`
in Codespaces before starting the server.

## 4. Copy secrets and data (from the Codespace terminal)
```bash
python db.py backup transfer.db          # consistent copy, even if something has it open
scp .env credentials.json token.json gcal_state.json bot_state.json root@IP:/home/coach/study-coach/
scp transfer.db root@IP:/home/coach/study-coach/coach.db
ssh root@IP 'chown coach:coach /home/coach/study-coach/{.env,*.json,coach.db} && chmod 600 /home/coach/study-coach/.env'
rm transfer.db
```
(The Codespace needs an SSH key the server accepts: `ssh-keygen -t ed25519`, then add
`~/.ssh/id_ed25519.pub` to `/root/.ssh/authorized_keys` on the server.)

Add `ANTHROPIC_API_KEY=...` to the server's `.env` for the weekly review, or
`REVIEWER_PROVIDER=deepseek` until you have Anthropic credit. See `.env.example`.

## 5. Google sign-in that doesn't expire
While the OAuth app's publishing status is **Testing**, Google's refresh tokens die
after 7 days, so the calendar sync would break every week.
1. Google Cloud Console → Google Auth Platform → Audience → **Publish app**
   (status becomes "In production").
2. You don't need Google's verification for a personal app with under 100 users. You'll
   see a "Google hasn't verified this app" screen at sign-in: Advanced → continue.
3. The old token was issued in Testing mode, so sign in again on the server:
   ```bash
   sudo -iu coach && cd study-coach
   .venv/bin/python gcal.py auth     # copy-paste flow, works over SSH
   .venv/bin/python gcal.py sync
   ```

## 6. Start it
```bash
sudo systemctl start study-coach
sudo journalctl -u study-coach -f       # live log; Ctrl+C to stop watching
```
On your phone: `/help`, then `/note testing the knowledge base`, then `/review`.
systemd restarts the bot if it crashes and starts it again after a reboot.

## 7. Backups
`study-coach-backup.timer` runs `deploy/backup.sh` at 03:30 UK time: a consistent copy
of `coach.db`, integrity-checked, gzipped into `/home/coach/backups`, kept 30 days.
```bash
sudo systemctl start study-coach-backup && ls -l /home/coach/backups   # test it now
systemctl list-timers study-coach-backup                               # next run
```
Those backups live on the same server, so they don't survive losing the server. Turn
on the provider's backups (Hetzner: +20%), or pull a copy now and then:
`scp root@IP:/home/coach/backups/coach-*.db.gz .`

Restore drill (do it once, so you know it works):
```bash
sudo systemctl stop study-coach
gunzip -c ~/backups/coach-YYYY-MM-DD.db.gz > coach.db && .venv/bin/python db.py version
sudo systemctl start study-coach
```

## 8. Updating
Push to GitHub, then on the server as `coach`:
```bash
~/study-coach/deploy/update.sh      # pull, install, run tests, restart only if they pass
```

## Day to day
| | |
|---|---|
| Logs | `sudo journalctl -u study-coach --since today` |
| Planner / review logs | `~/study-coach/logs/` |
| Status | `sudo systemctl status study-coach` |
| Dry runs on real data | `.venv/bin/python planner.py --dry-run`, `.venv/bin/python reviewer.py --dry-run` |

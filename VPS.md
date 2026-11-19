# Deploy on a Linux VPS

The dashboard listens on the VPS loopback address at port 18768. Open it from your computer through an SSH tunnel. Choose native systemd deployment on Ubuntu or the Docker Compose instructions below.

## Native systemd deployment

Use an Ubuntu VPS with Python 3.10 or later and Git. Run these commands as root on a fresh server:

```sh
git clone https://github.com/lakshyagambhirlaksh-boop/ema-cross-vwap.git /opt/delta-strategy
install -d -m 700 /etc/delta-strategy
test -f /etc/delta-strategy/credentials.env || install -m 600 /opt/delta-strategy/.env.example /etc/delta-strategy/credentials.env
nano /etc/delta-strategy/credentials.env
install -m 644 /opt/delta-strategy/deploy/delta-strategy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now delta-strategy.service
systemctl status delta-strategy.service --no-pager
```

Set the Delta key and secret in `/etc/delta-strategy/credentials.env`. Run `systemctl restart delta-strategy.service` after changing that file. Systemd keeps the position journal in the persistent service state directory. View logs with `journalctl -u delta-strategy.service -n 50 --no-pager`.

Continue with **Access and start trading** below.

## Docker Compose deployment

Use a Linux VPS with Docker Engine and the Docker Compose plugin.

On the VPS, run:

```sh
git clone https://github.com/lakshyagambhirlaksh-boop/ema-cross-vwap.git
cd ema-cross-vwap
test -f .env || install -m 600 .env.example .env
nano .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 dashboard
```

Fill in `DELTA_API_KEY` and `DELTA_API_SECRET` in the VPS `.env`.

## Access and start trading

Use a Delta Exchange India key with trading permission. On your computer, start the tunnel:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18768:127.0.0.1:18768 YOUR_VPS_USER@YOUR_VPS_IP
```

Leave the SSH terminal open and visit [the VPS dashboard through the tunnel](http://127.0.0.1:18768/). In the dashboard sidebar, find the public IPv4 and allowlist it for that API key in Delta. Verify that the sidebar shows **Connected**.

If your VPS also runs the read-only `delta-history.service` while the main trading process awaits a scheduled restart, add `-L 127.0.0.1:18769:127.0.0.1:18769` to the SSH command to load the updated history without interrupting trading.

The strategy starts with entries stopped on a fresh install. Before starting this engine, pause any other copy running on the same Delta account and contracts. Wait for positions tracked by that copy to close, or manage them there until they close. Select contracts, submit the timeframe, set lots, and confirm **Start strategy** when you are ready to allow real orders.

## Update and inspect a Docker deployment

```sh
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 dashboard
```

The named volume `strategy-state` retains the position journal and the Start/Stop choice when the container is recreated. Keep this volume for restart recovery. A started strategy resumes on new candles after a restart. **Stop strategy** persists across restarts and pauses new entries while the container keeps managing tracked positions. Stopping the container also stops the software-managed exits.

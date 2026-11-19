# Deploy on a Linux VPS

Use a Linux VPS with Docker Engine and the Docker Compose plugin. The dashboard listens on the VPS loopback address at port 18768. Open it from your computer through an SSH tunnel.

## Install and start

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

Fill in `DELTA_API_KEY` and `DELTA_API_SECRET` in the VPS `.env`. Use a Delta Exchange India key with trading permission. On your computer, start the tunnel:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18768:127.0.0.1:18768 YOUR_VPS_USER@YOUR_VPS_IP
```

Leave the SSH terminal open and visit [the VPS dashboard through the tunnel](http://127.0.0.1:18768/). In the dashboard sidebar, find the public IPv4 and allowlist it for that API key in Delta. Verify that the sidebar shows **Connected**.

The strategy starts with entries stopped. Before starting this engine, pause any other copy running on the same Delta account and contracts. Wait for positions tracked by that copy to close, or manage them there until they close. Select contracts, submit the timeframe, set lots, and confirm **Start strategy** when you are ready to allow real orders.

## Update and inspect

```sh
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 dashboard
```

The named volume `strategy-state` retains the local position journal when the container is recreated. Keep this volume for restart recovery. After a restart, the dashboard continues managing positions from its journal with new entries stopped until you confirm Start again. **Stop strategy** pauses new entries while the container keeps managing tracked positions. Stopping the container also stops the software-managed exits.

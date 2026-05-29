# FRP for Simple Servermanager

This branch uses FRP TCP tunnels only.

- `start.sh` can expose the node API to the VPS with `SIMPLE_API_FRP_ENABLED=true`.
- `main.py` renders and restarts a standalone `frpc` process for user-created TCP tunnels.
- Docker, GPU containers, stcp visitors, and per-container systemd units are not used here.

Install the `frpc` binary:

```bash
cd frp
bash install.sh
```

Then configure `.env` from `.env.copy` and start the node:

```bash
./start.sh
```

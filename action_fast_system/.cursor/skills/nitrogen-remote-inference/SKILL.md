---
name: nitrogen-remote-inference
description: Call the NitroGen action-change inference service that lives on the remote autodl machine from a different machine. Covers SSH connection, starting/stopping the long-running server (with conda env activation), opening the project directory, and invoking the per-frame remote_infer.py client. Use when the user wants to predict gamepad actions or detect action changes from images using the NitroGen model hosted on the remote box, or mentions remote_infer, action_change_server, NitroGen inference, or the autodl GPU machine.
---

# NitroGen Remote Inference

This project lives on a remote autodl GPU box. The inference service is a long-running FastAPI server (`scripts/action_change_server.py`) that loads `ng.pt` once and serves per-frame predictions over HTTP. Other machines call it via `scripts/remote_infer.py`.

## Remote machine coordinates

| Item | Value |
|---|---|
| SSH host | `connect.bjb1.seetacloud.com` |
| SSH port | `18037` |
| SSH user | `root` |
| Project dir | `/root/autodl-tmp/NitroGen` |
| Conda env | `nitrogen` (REQUIRED for every python invocation) |
| Server port | `8000` (HTTP) |
| Checkpoint | `/root/autodl-tmp/NitroGen/ng.pt` |

Base SSH command (use as prefix for one-shot commands):

```bash
ssh -p 18037 root@connect.bjb1.seetacloud.com
```

## Always activate conda first

Every Python command on the remote box MUST run inside the `nitrogen` env. The Shell tool's sessions are not persistent, so activate in the same command line:

```bash
source activate nitrogen && python ...
```

This rule is enforced by `.cursor/rules/conda-env.mdc` on the remote machine.

## Workflow

### Step 1 — Open the project directory

When working ON the remote machine, always start by cd-ing to the project:

```bash
cd /root/autodl-tmp/NitroGen
```

When using Cursor remotely with this folder open, the working directory is already correct; just confirm with `pwd`.

### Step 2 — Make sure the server is running

The server is started via a detached script that survives terminal disconnects (uses `setsid + nohup`). PID file: `.server.pid`, log: `server.log`.

Check status:

```bash
ssh -p 18037 root@connect.bjb1.seetacloud.com '
  cd /root/autodl-tmp/NitroGen && \
  if [ -f .server.pid ] && kill -0 $(cat .server.pid) 2>/dev/null; then
    echo "Server running, PID=$(cat .server.pid)"
  else
    echo "Server NOT running"
  fi'
```

Start (only if not running):

```bash
ssh -p 18037 root@connect.bjb1.seetacloud.com '
  cd /root/autodl-tmp/NitroGen && bash scripts/start_server.sh'
```

The start script activates `nitrogen` automatically. Wait ~15-30 s for `Application startup complete` to appear in `server.log` before the first call.

Verify ready:

```bash
ssh -p 18037 root@connect.bjb1.seetacloud.com '
  cd /root/autodl-tmp/NitroGen && grep -q "Application startup complete" server.log && echo READY'
```

Stop (rare, only when explicitly asked):

```bash
ssh -p 18037 root@connect.bjb1.seetacloud.com '
  cd /root/autodl-tmp/NitroGen && bash scripts/stop_server.sh'
```

### Step 3 — Reach the HTTP endpoint from the local machine

The server binds `0.0.0.0:8000` on the remote box but is usually not directly reachable over the public internet. Use an **SSH tunnel** so `localhost:8000` on the local machine forwards to the server:

```bash
ssh -p 18037 -L 8000:localhost:8000 -N -f root@connect.bjb1.seetacloud.com
```

`-N -f` runs the tunnel in the background. To stop:

```bash
pkill -f 'ssh.*18037.*-L 8000'
```

If the box exposes 8000 directly, you can also just use `http://connect.bjb1.seetacloud.com:8000` and skip the tunnel.

### Step 4 — Call per-frame inference

`scripts/remote_infer.py` accepts one image per invocation and manages per-video case folders automatically. Copy it to the local machine once:

```bash
scp -P 18037 root@connect.bjb1.seetacloud.com:/root/autodl-tmp/NitroGen/scripts/remote_infer.py .
pip install requests   # only dependency
```

Per-frame call (script runs ON the local machine, hits the tunneled server):

```bash
python remote_infer.py path/to/frame.png \
    --server http://localhost:8000 \
    --output-dir ./remote_runs
```

Behavior:

- Gap to previous call > 1.0 s ⇒ **cold start**: creates `remote_runs/<YYYYMMDD_HHMMSS_case>/`, POSTs `/reset` to clear server history.
- Gap ≤ 1.0 s ⇒ **continuous**: reuses the existing case folder; server keeps history.
- Saves `<case_dir>/<frame_stem>.json` AND prints JSON to stdout.

Useful flags: `--idle-sec 1.0`, `--tag <video_name>`, `--no-save`, `--output-dir`, `--timeout`.

### Step 5 — Inspect results

Output JSON schema is documented in `docs/output_schema.md` on the remote box. Top-level fields: `frame_idx`, `session_idx`, `auto_reset`, `action_summary`, `is_change`, `change_info`, plus client-added `source_image`, `case_dir`, `cold_start`.

## End-to-end one-liner (local machine)

```bash
ssh -p 18037 -L 8000:localhost:8000 -N -f root@connect.bjb1.seetacloud.com && \
  python remote_infer.py frame.png --server http://localhost:8000
```

## Common operations on the remote box (always with conda)

```bash
# Tail server log
tail -f /root/autodl-tmp/NitroGen/server.log

# Inspect server config / state
curl -s http://localhost:8000/info | python -m json.tool

# Manually reset session
curl -X POST http://localhost:8000/reset

# Batch a folder of images (uses local action_change_client.py)
source activate nitrogen && \
  cd /root/autodl-tmp/NitroGen && \
  python scripts/action_change_client.py --batch \
    --input-dir inputs --output-dir outputs --reset
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Connection refused` on `:8000` from local | SSH tunnel not up; rerun the `-L 8000:...` command. |
| Server start fails with checkpoint not found | `ng.pt` missing; expected at `/root/autodl-tmp/NitroGen/ng.pt`. |
| `ImportError: torch` etc. when starting | Forgot `source activate nitrogen` before invoking python. The start script handles this; manual runs must do it. |
| First request times out (60 s) | Model still loading; wait for `Application startup complete` in `server.log`. |
| Every frame marked `cold_start=true` | Local state file deleted, or `--output-dir` changed between calls; ensure `remote_infer.py` keeps writing to the same `remote_runs/.state.json`. |
| Stale `.server.pid` blocks restart | Already handled by `start_server.sh` (auto-cleans), but you can `rm .server.pid` manually if needed. |

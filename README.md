# Cassandra's Hoard

Observability for the whole local AI stack on your PC. It watches every local service you run — the Faustus workspace and its test instances, llama-server, Ollama, ComfyUI, the Hoard Hub launcher and every Hoard app — and remembers **what is up, what went down and when, what else changed at that same moment, what the logs said and what the GPUs were doing**. When something stops at 04:00 and nobody knows why, Cassandra has the answer (or the most probable one). An assistant reaches the same data through MCP, so you can simply ask "why did Borges stop last night?".

Everything stays on the machine: one SQLite file, no accounts, no telemetry, no network beyond loopback health checks of your own services.

Part of the Hoard family (see `faustus-plugin.json`).

## What it does

- **Registry of services**, merged from three sources:
  - **discovered apps**: the Hoard Hub's own list (`GET http://127.0.0.1:8810/api/apps`, one source of truth for the whole family; `CASSANDRA_HUB_REGISTRY=0` disables it, `/api/status` shows `registry_source`) and, when the hub is not there, every `<root>/*/faustus-plugin.json` (the same manifest the launcher reads). The manifest's `app.health` gives the health path and the expected `service`; `data/url` in the app folder wins over the manifest port;
  - **built-in externals**: Faustus (`7000` main, `7001-7003` test, `/api/health`), llama-server (`8081`, helper `8082`, `/health`), Ollama (`11434`, `/api/tags`), ComfyUI (`8188-8191`, `/system_stats`) and Hoard Hub (`8810`). An external that never answered shows as **never seen**, never as an incident;
  - **your own services** in `data/services.json` (id, name, url, health path, expected JSON, log paths, restart policy). An entry with the id of a discovered app or an external edits it.
- **Poller**: every `CASSANDRA_POLL_S` seconds (20 by default) all services are checked in parallel with short timeouts. The listening ports are read first (psutil), so a closed port is `down` at once (on Windows connecting to a closed loopback port takes ~1.5 s to fail). States: `up`, `degraded` (HTTP error or slower than `CASSANDRA_SLOW_MS`), `foreign` (another program answers on that port), `down`. The pid behind each port, its start time and a hash of its command line are kept: a new pid on a service that stayed up is a **restart event**.
- **Incidents**: opened when a service goes from up to down/foreign (or its process is replaced), closed when it answers again. At open time a **context** is captured: the other services that changed within ±3 min (filled in by later polls), GPU memory just before and the peak of the last 3 min, the last lines of that service's logs, the process (pid, command line, alive or gone) and the machine's boot time. A **probable cause** is written in plain words from heuristics, for example:
  - "The machine restarted (boot at 03:58…): a reboot stops every service."
  - "Cassandra itself was not running between 01:10 and 07:30 (sleep, hibernation or shutdown?)…"
  - "3 services fell in the same minute (…) → machine-wide event (sleep, shutdown, GPU driver reset?)."
  - "GPU 1 memory jumped to 98% 40 s before → VRAM contention (another model loading?)."
  - "The log ends with a Traceback: “RuntimeError: …”." / "The log reports running out of memory: …"
  - "The process (pid 1234 python.exe) is still alive but does not answer: hung, overloaded or still loading."
- **System pseudo-service**: boot time and uptime; a boot-time change is recorded as a `reboot` event, and a long pause of Cassandra's own loop (sleep, hibernation, closed app) as a `gap`, shown striped on the lanes.
- **GPU**: `nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu` every poll (skipped silently when there is no NVIDIA driver).
- **Logs**: incremental tailing of each app's `data/logs/*.log`, the launcher's `data/logs/<app>.log` (the output of the apps it started), `%LOCALAPPDATA%\Hoards\*.log`, Cassandra's own `data/logs/<id>.log` (apps it restarted), each user service's `log_paths` and `CASSANDRA_LOG_GLOBS`. A file seen for the first time is read from its last 64 KB; rotation (a file that shrinks) is handled. Each line gets a time (parsed from the line when it has one) and a level (error/warning/info/debug heuristic). Retention: `CASSANDRA_RETENTION_DAYS` (14) and `CASSANDRA_LOG_MAX_LINES`.
- **Audit trail** (Hoard Link 0.4): the Hoard Hub keeps a rolling event bus every app posts to — one `agent.call` per tool the assistant ran, app milestones (`scribe.transcript.done`, `links.watch.new`…), hub actions (`hub.backup.done`, `hub.rule.ran`, `hub.app.started`). Cassandra mirrors it for good into `bus_events` (`GET <hub>/api/events?since_id=`, every 10 s, `CASSANDRA_BUS=0` to disable, `CASSANDRA_HUB_URL` for the hub), so `svc_why_down` also shows what the assistant did in the three minutes around an incident, and `audit_search` / `audit_stats` answer "who called what, when, and what failed". Cassandra posts its own `cassandra.incident.opened` / `closed` events to the bus, so a hub rule can react to a service going down.
- **Secrets audit**: `secrets_audit` walks every app folder Cassandra discovered: the agent token file (present, permissions), whether `data/` is git-ignored, secret-looking files tracked by git (`mcp-token`, `.env`, `*.key`, databases), `.env` files. Read-only, no network.
- **Restart policies** (opt-in, per service): the policy's own command; or, for a discovered app while the launcher is up, `POST http://127.0.0.1:8810/api/apps/<id>/start` (`/restart` if it still runs); or the app's manifest `launch_hint` (`{X_DIR}`, `{FAUSTUS_PYTHON}` resolved like the launcher does). Never more than `max_per_hour` automatic restarts per service; never over a port held by another program; every attempt is recorded in the incident. A manual restart (UI button or `svc_restart`) is always allowed for a service that has a way to start.
- **UI** (Spanish or English, automatic, switchable; dark theme): **Panel** (status grid grouped by kind with a 24 h lane per service, machine and GPU summary, check now, restart), **Incidents** (filters by service, window or "around 04:00", open only; each incident expands to the explanation, what else changed, GPUs, process, log tail and actions), **GPU** (plain SVG chart of memory and load, peaks, free memory), **Logs** (search by words, service, level and time), **Services** (automatic-restart switch, max/hour and command per service, add or remove your own services, effective configuration). Installable as a PWA.

## Requirements

- Windows 10/11 (also Linux/macOS), Python 3.11+ (3.13 fine), Node 22 only to build the client.
- `psutil` (in requirements) for ports, pids and boot time. On macOS listing other processes' sockets may need extra permissions; Cassandra then falls back to HTTP-only checks.
- An NVIDIA driver (`nvidia-smi`) is optional: without it the GPU parts are simply empty.

## Install and run

Windows:

```bat
git clone <this repo> CassandraHoard
cd CassandraHoard
python -m venv venv
venv\Scripts\pip install -r requirements.txt
npm.cmd install --include=dev
npx.cmd vite build
venv\Scripts\python -m cassandra_hoard
```

Linux / macOS:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm install --include=dev && npx vite build
.venv/bin/python -m cassandra_hoard
```

Open http://127.0.0.1:5190. Put the repository next to the other Hoard apps (their parent folder is the default discovery root) or set `CASSANDRA_ROOTS`.

- `python scripts/launch.py` starts the app on a free port and opens the browser.
- `python scripts/dev.py` runs the API with reload plus the Vite dev server (proxying `/api`).
- `python scripts/make_icon.py --src <Icons>/dragon-src.png` regenerates the icon from the family dragon (needs `requirements-icon.txt`).

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `CASSANDRA_PORT` / `PORT` | `5190` | Port (the next free one unless `PORT_STRICT=1`). |
| `PORT_STRICT` | — | `1` = fail instead of moving to another port. |
| `CASSANDRA_DATA_DIR` | `./data` | Database, token, `url`, `services.json`, restart logs. |
| `CASSANDRA_POLL_S` | `20` | Seconds between checks. |
| `CASSANDRA_ROOTS` | parent folder of the repo | Folders whose sub-folders are scanned for `faustus-plugin.json` (`;`-separated). |
| `CASSANDRA_EXTERNALS` | `1` | `0` = do not watch the built-in externals. |
| `CASSANDRA_LOG_GLOBS` | — | Extra logs, `;`-separated: `glob` (tagged by file name) or `service=glob`. |
| `CASSANDRA_DEFAULT_LOGS` | `1` | `0` = do not tail Cassandra's own logs folder and `%LOCALAPPDATA%\Hoards`. |
| `CASSANDRA_RETENTION_DAYS` | `14` | History kept (samples, events, incidents, GPU, logs). |
| `CASSANDRA_LOG_MAX_LINES` | `500000` | Cap of stored log lines. |
| `CASSANDRA_SAMPLE_EVERY_S` | `60` | An unchanged up state is stored at most this often (latency history). |
| `CASSANDRA_SLOW_MS` | `3000` | Slower health answers count as `degraded`. |
| `CASSANDRA_AUTO_RESTART` | `1` | `0` = master switch off for automatic restarts (manual ones still work). |
| `CASSANDRA_AGENT_COMMANDS` | `0` | `1` = the assistant may set restart commands through `svc_watch`. |
| `CASSANDRA_HUB_URL` | `http://127.0.0.1:8810` | The launcher used to start discovered apps. |
| `CASSANDRA_FAUSTUS_PYTHON` | this interpreter | Fills `{FAUSTUS_PYTHON}` in launch hints. |
| `CASSANDRA_GPU` | `1` | `0` = do not run `nvidia-smi`. |
| `CASSANDRA_PORT_CHECK` | `1` | `0` = skip the listening-port shortcut (HTTP only). |
| `CASSANDRA_AUTOSTART` | `1` | `0` = do not start the poller with the app. |
| `CASSANDRA_ALLOWED_HOSTS` | — | Extra Host names (exact or `*.suffix`) for access through a tunnel. |

`data/services.json` example:

```json
{
  "services": [
    {"id": "whisper", "name": "Whisper server", "url": "http://127.0.0.1:9000", "health_path": "/health",
     "expect": {"status": "ok"}, "log_paths": ["C:/tools/whisper/logs/*.log"],
     "restart": {"enabled": true, "cmd": ["C:/tools/whisper/run.bat"], "cwd": "C:/tools/whisper", "max_per_hour": 3}}
  ],
  "policies": {"ollama": {"enabled": true, "cmd": "ollama serve", "max_per_hour": 2}}
}
```

## API

`GET /api/health` (`{"service": "cassandra-hoard", ...}`), `/api/status`, `/api/services`, `/api/services/{id}`, `/api/services/{id}/history`, `/api/lanes?hours=24`, `/api/incidents` (`since`, `until`, `at`, `window_min`, `service`, `open_only`), `/api/incidents/{id}`, `/api/logs` (`q`, `service`, `level`, time window), `/api/logs/sources`, `/api/gpu`, `/api/settings`; `POST /api/poll`, `POST /api/services` (add/edit), `DELETE /api/services/{id}`, `PUT /api/services/{id}/policy`, `POST /api/services/{id}/restart`; `GET /api/agent/tools`, `POST /api/agent/call` (Bearer token from `data/mcp-token`). Times accept ISO (`2026-09-24T04:00`), a clock time (`04:00` = the last 04:00) or an age (`2h`, `30m`, `1d`).

## MCP tools

`mcp_server.py` is a stdio bridge: it fetches the catalogue from the app, proxies every call with the token, never opens the database, and starts the app itself when it is not answering (`CASSANDRA_BRIDGE_AUTOSTART=0` turns that off).

| Tool | What it answers | Writes |
|---|---|---|
| `svc_status` | Is X down? State of every service (or one) now, since when, latency, pid, open incident, restart policy, boot time, GPUs now. | no |
| `svc_incidents` | What happened at 04:00? Incidents in a window (`at` ± `window_min`), with probable cause, reboots and Cassandra's own gaps. | no |
| `svc_why_down` | Why did Y stop? The latest incident in plain sentences: cause, GPUs, log tail, restarts. | no |
| `logs_search` | What did the logs say? Words, service, level, time window. | no |
| `gpu_timeline` | Which GPU is free? Memory/load series, peaks, free memory now, freest GPU. | no |
| `svc_history` | The up/down timeline of one service, with uptime %. | no |
| `audit_search` | What did the assistant do? The family bus mirrored from the hub: every agent tool call (app, tool, ok, ms, caller), app milestones, hub actions — by words, type, app, tool, failures, time. | no |
| `audit_stats` | Agent calls per app and tool, failures, slowest, busiest callers, over a window. | no |
| `secrets_audit` | Leaked secrets in app folders: token present, `data/` git-ignored, secret-looking files tracked by git, `.env` files. | no |
| `svc_restart` | Restart or start a service (only when the user asks). | yes |
| `svc_watch` | Add or edit a watched service, its logs and restart policy (only when the user asks). | yes |

## Tests

```sh
python -m pytest -q      # fake services (ASGI apps flipping up/down), fake clock, GPU and process table
npx vite build
```

## Privacy

Everything is local: health checks go only to the URLs in the registry (loopback by default), logs are read from your own disk and stored in `data/cassandra.db`, nothing is sent anywhere and there is no telemetry. The API accepts only local origins (plus `CASSANDRA_ALLOWED_HOSTS`), and the agent routes need the token in `data/mcp-token`.

## Limits (v1)

- Probable causes are heuristics over what Cassandra saw; when it was not running, it can only say the event happened inside that gap.
- The exit code of a process that Cassandra did not start is not available; it reports whether the process is gone or still alive.
- GPU sampling is NVIDIA-only (`nvidia-smi`).

## License

MIT — see `LICENSE`.

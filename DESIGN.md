# Cassandra's Hoard — design notes

## Architecture

```
faustus-plugin.json (siblings) ─┐
built-in externals ─────────────┼─> registry.py ──> poller.py ──> SQLite (data/cassandra.db)
data/services.json ─────────────┘        │            │  ├─ samples / events (state, pid, restart, gap, reboot)
                                         │            │  ├─ gpu_samples (nvidia-smi)
                                         │            │  ├─ log_lines / log_files (logs.py, incremental)
                                         │            │  └─ incidents (incidents.py, context + causes)
                                         │            └─> restart.py (opt-in, rate-limited)
                                         └─> api/* (REST + /api/agent/*) <── mcp_server.py (stdio, proxy only)
```

- **One writer**: the app process. The MCP bridge only talks HTTP to `/api/agent/*` with the token from `data/mcp-token`; it starts the app when it does not answer.
- **One catalogue**: `agent_tools.py` defines the tools once; `/api/agent/tools` serves it and the bridge re-exposes it, so they never disagree.
- **Poll tick** (`Poller.tick`): system check (gap since the previous tick, boot-time change) → registry rescan every 5 min → listening ports (psutil) → parallel health probes (`httpx.AsyncClient`, trust_env off) → pid info → GPU sample → log tail → apply transitions (samples, events, incidents, auto-restart) → refresh the context of incidents younger than 3 min → hourly prune.
- **Storage policy**: a sample is written on every state or pid change and, while up/degraded, at most every `CASSANDRA_SAMPLE_EVERY_S`; a down service is written only when it changes. Lanes are rebuilt from consecutive samples; gaps come from `events(kind='gap')` with `until_ts`.
- **Incidents** open only on up/degraded → down/foreign or a pid change of a live service; never for services that were never up. The context is recomputed on each tick until 3 minutes after opening (`context_final`), so "what else changed" also covers what fell after.
- **Heuristic order** (weight): reboot 100, Cassandra gap 90, ≥3 services in the same minute 80, out-of-memory in the log 75, GPU ≥95 % in the 3 min before 70, another program on the port 65, Traceback / "Killed" 60, pid replaced 60, process alive but silent 50, last log error 45, GPU jump ≥25 points 40, process gone 30, nearby changes 20. `probable_cause` = the first two.
- **Restarts**: policy command → launcher API (`/api/apps/<id>/start|restart`) → manifest launch hint. Automatic only with the policy on, the master switch on, state `down` (never `foreign`) and under `max_per_hour` in the last hour. Children are detached, output in `data/logs/<id>.log` (which the tailer reads).
- **Safety**: loopback guard shared by the family (`guard.py`); restart commands cannot be set by the assistant unless `CASSANDRA_AGENT_COMMANDS=1`; Cassandra refuses to stop its own pid and checks the pid start time before stopping (recycled pids).

## Design system

### Overview
A night-watch console: dark navy (the icon's flat background) with a magenta-crimson accent from the recoloured dragon and gold for marks (reboots, GPU load) like the glyph. Dense, scannable rows; colour means state and nothing else.

### Colors
- Background `#07101f`, sidebar `#050c18`, surfaces `#0f1a2e` / `#14213a`, lines `#22304b`.
- Ink `#e7eaf3`, muted `#8b96b0`.
- Accent `#d64a8a` (hover/active), strong `#a3245f` (primary buttons, theme colour), gold `#e8b64a`.
- States: up `#3fb27f`, degraded `#e0a43a`, down `#e5534b`, foreign `#9a7cf0`, never seen `#34405a`; Cassandra gaps are white 8 % stripes.

### Typography
Segoe UI / system-ui 14 px; tabular numbers for times and sizes; Cascadia Mono / Consolas 12 px for logs, command lines and configuration.

### Layout
Left sidebar (216 px) with the icon, sections, last check and language switch; on phones it becomes a top bar with horizontally scrolling tabs. Content max width follows the viewport; service rows are a three-column grid (name, state pill, 24 h lane) that wraps the lane under the name on phones.

### Components
- **State pill**: dot + label, never colour alone.
- **Lane**: SVG 1000×10 stretched, one rect per segment with a `<title>` tooltip, gap stripes, gold reboot marks.
- **Incident card**: red border while open; the summary line (service, change, time, duration) and the probable cause are always visible; the context expands.
- **GPU chart**: plain SVG, memory as a filled area (accent), load as a gold line, a red dot on the memory peak; no chart library.
- **Switch**: accessible `role="switch"` button for the automatic-restart policy.

### Do / Don't
- Do quote times and the probable cause as Cassandra states them; say "probable".
- Do keep every write (restart, watch-list edits) behind an explicit click or an explicit user request to the assistant.
- Don't show never-seen services as failures.
- Don't add a chart library for one chart.

## Icon
`scripts/make_icon.py` follows the family recipe: HSV mask of the yellow play button (filled, plus its halo) → flattened luminance map → `cv2.inpaint` (TELEA) → luminance ramp `#5a1140 → #d64a8a`, eye kept light (`#ffe4f0`) → flat navy `(1, 11, 27)` background → gold bell with a pulse line across it (dark outline), ≈400 px centred at (627, 768).

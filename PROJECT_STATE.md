# PigWatch — Project State (handoff doc)

Written as a technical handoff for continuing development, not as a demo
pitch (see `DEMO.md` for that). Covers what exists and how it's wired
together. Does not include next steps — those are being provided separately.

Repo: https://github.com/Come404/pig_monitor (branch `main`, up to date with
what's described here).

## Repository layout

```
pig_tracking_pipeline.py   # detection + tracking + Nano Omni orchestration
pig_stress_monitor.py      # sensor analysis + Ultra call + CLI dashboard (rich)
backend_app.py             # FastAPI wrapper exposing the pipeline over HTTP
requirements.txt           # core deps (opencv/numpy/matplotlib/openai/rich), >= not ==
requirements-server.txt    # requirements.txt + fastapi + uvicorn, used on the VM
sensors_sample.json        # SYNTHETIC 10-record sensor fixture, not real sensor data
pigs_top_down.mp4          # demo video (top-down pig pen, ~5s)
webapp/                    # React (Vite) frontend
DEMO.md                    # human-facing demo narrative + architecture summary
.gitignore                 # excludes .venv/, .claude/, keys, debug artifacts, pig_tracking_results.json
```

Not in git (by design): `.venv/`, `key.txt`/`deploy_key` (SSH keys for the
VM), `webapp/.env` (points at the live API URL), `.claude/` (local tooling
config with an absolute venv path).

## What each script does

### `pig_tracking_pipeline.py`
- `detect_centroids(frame, back_sub, min_area, max_area, debug, learning_rate)`:
  MOG2 background subtraction + morphology + contour filtering →
  list of (x, y) pixel centroids.
- `SimpleCentroidTracker`: nearest-centroid matching across ticks, assigns
  persistent `Porc_N` IDs.
- `render_plane(...)`: renders tracked centroids as a clean synthetic
  top-down scatter plot (matplotlib) — this is what gets sent to the vision
  model, not the raw camera frame.
- `analyze_with_nano_omni(client, image_b64, ...)`: sends the rendered plane
  to Nemotron Nano Omni, asks for strict JSON:
  `{"spatial_distribution": "grouped"|"dispersed"|"highly_dispersed",
  "clustering_notes": str, "possible_concern": "aucune"|"chaleur"|"froid"|"stress"|"incertain"}`.
- `run_pig_tracking(video_path, client, interval=1.0, pen_width_m=6.0,
  pen_height_m=4.0, save_frames_dir=None, log=print)`: the reusable entry
  point. Samples the video every `interval` seconds, keeps the background
  subtractor continuously adaptive across *all* ticks, but only calls Nano
  Omni for **2** of them: the earliest usable tick (tick index 1 — tick 0 is
  always empty, see Known Issues) as a "baseline" snapshot, and the final
  tick as "current state." Returns a list of
  `{tick, timestamp_s, pigs: {id: {x_m, y_m}}, nano_omni_analysis}` dicts.
  Both the CLI (`main()`) and `backend_app.py` call this function directly.

Tuned constants (module-level, top of file) — these came from real
calibration against `pigs_top_down.mp4`, not defaults:
- `MIN_AREA=3000, MAX_AREA=200000, VAR_THRESHOLD=250, BG_HISTORY=200`
- `TRACKER_MAX_DISTANCE=250, TRACKER_MAX_MISSED=1`

### `pig_stress_monitor.py`
- `analyze_sensors(records)`: trends (first→last, mean) for temperature,
  humidity, THI from a list of sensor records.
- `build_summary(sensors, omni_ticks, enclosure_id)`: merges sensor trends +
  the 2 Omni readings into one compact text block — this exact text is what
  gets sent to Ultra, nothing else.
- `call_ultra(summary, api_key)`: sends the summary to **Nemotron Ultra
  550B** via Crusoe, system prompt asks for exactly 4 lines:
  `STATUS / WHAT'S HAPPENING / LIKELY CAUSE / RECOMMENDED ACTION`.
  `max_tokens=600` (256 truncated real responses — verified failure mode).
  System prompt explicitly tells the model that pigs grouping while
  temperature/THI are already high is a **heat-stress aggravating factor**,
  not evidence of cold — without this, the model defaults to reading
  "grouped" as a cold-stress signal even when sensor data says otherwise.
- CLI `main()`: standalone script with a `rich`-based terminal dashboard.
  Not used by the backend (which imports the functions directly instead).

### `backend_app.py`
FastAPI wrapper, no new logic — imports and calls the functions above.
- `GET /health` → `{"ok": true}`
- `POST /run` → runs the full pipeline synchronously (tracking + 2 Omni
  calls + sensor analysis + Ultra call), caches the result in memory, returns
  it. Takes several seconds (LLM calls). Not meant for high-frequency
  polling.
- `GET /report` → returns the last cached result, 404 if nothing has run
  yet in this process's lifetime (cache is in-memory, resets on service
  restart).
- Response shape:
  ```json
  {
    "generated_at": <unix ts>,
    "enclosure_id": "01",
    "sensors": { "n", "t_start", "t_end", "temp_first", "temp_last", "temp_mean",
                 "temp_trend", "hum_first", "hum_last", "hum_mean", "hum_trend",
                 "thi_mean", "thi_last", "thi_trend" },
    "omni_ticks": [ { "tick", "timestamp_s", "n_pigs", "pigs", "nano_omni_analysis" }, ... ],
    "summary_sent_to_ultra": "<the exact text Ultra received>",
    "ultra_report": { "status", "whats_happening", "likely_cause", "recommended_action", "raw" } | null,
    "ultra_error": "<error string>" | null
  }
  ```
- CORS currently `allow_origins=["*"]` — intentionally wide open for now,
  flagged in-code to tighten once the frontend's real deployed origin is
  known.
- Reads `PIGWATCH_VIDEO`, `PIGWATCH_SENSORS`, `PIGWATCH_ENCLOSURE_ID` env
  vars (defaults point at `/opt/pigwatch/...` paths on the VM). Requires
  `CRUSOE_API_KEY`.

### `webapp/` (React + Vite)
- `src/api.js`: fetch wrapper, base URL from `VITE_API_BASE_URL` (Vite env
  var, read from `webapp/.env` — gitignored, currently set to
  `https://api.cwco.tech`; `.env.example` documents the variable).
  `getReport()` (GET /report), `runPipeline()` (POST /run).
- `src/App.jsx`: fetches `/report` on mount, "Run pipeline" button triggers
  `/run`, loading/error states handled.
- `src/components/`: `SensorPanel` (trend table), `OmniPanel` (2 vision
  readings, handles per-tick `nano_omni_analysis.error` gracefully),
  `UltraReportPanel` (status badge + 3 text fields), `StatusBadge`
  (NOMINAL=green, WATCH=yellow, WARNING=orange, CRITICAL=red).
- Tested in-browser against the live backend (both cached-report load and
  live "Run pipeline" click verified working, including a real CRITICAL
  report round-trip).
- **Not yet deployed to Cloudflare Pages** — currently only run locally via
  `npm --prefix webapp run dev` (or the Vite dev server directly).

## Deployed infrastructure

- **VM**: Vultr Cloud Compute, Ubuntu 22.04, IP `45.32.151.161`, root user,
  SSH via a locally-held key (`deploy_key`, gitignored, not in repo).
  Project files live at `/opt/pigwatch/` on the VM (copied via scp, not git
  — the VM does not pull from GitHub, it's a manual file copy + a Python
  venv at `/opt/pigwatch/.venv`).
- **Backend service**: systemd unit `pigwatch.service`, runs
  `.venv/bin/python -m uvicorn backend_app:app --host 127.0.0.1 --port 8000`
  (localhost-only, not exposed directly). `EnvironmentFile=/opt/pigwatch/.env`
  supplies `CRUSOE_API_KEY` (root-only file permissions, value not in git).
  Enabled + auto-restarts on failure.
- **Tunnel**: named Cloudflare Tunnel `pigwatch` (id
  `376bb2d0-017f-48b5-aaad-861c4efa01ff`), config at
  `/root/.cloudflared/config.yml` on the VM, routes `api.cwco.tech` →
  `http://localhost:8000`. Runs as systemd unit `cloudflared.service`
  (installed via `cloudflared service install`), enabled + persistent.
- **Domain**: `cwco.tech` registered at OVHcloud, nameservers delegated to
  Cloudflare (free plan). DNS record for `api.cwco.tech` is a Cloudflare-
  managed CNAME to the tunnel, created via `cloudflared tunnel route dns`.
- **API keys**: only `CRUSOE_API_KEY` is needed anywhere (Nano Omni and
  Ultra are both served through Crusoe's OpenAI-compatible endpoint,
  `CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1/"`, exported
  as a constant from `pig_tracking_pipeline.py`). No NVIDIA-direct key is
  used anymore. Never hardcoded — env var only, both locally and on the VM.

## Known issues / gotchas worth knowing before touching this again

- **MOG2 tick 0 is always empty.** The very first frame a fresh background
  subtractor sees defines its initial background estimate, so it can never
  register as foreground. This is why the "baseline" snapshot uses tick
  index 1, not 0 — not a bug, inherent to the method.
- **Nano Omni's grouped/dispersed classification has real run-to-run
  variance** on the same footage — confirmed by running the identical
  pipeline twice and getting `highly_dispersed→grouped` once and
  `dispersed→dispersed` the next time. Ultra's final CRITICAL/heat verdict
  held both times (sensor data alone is extreme enough to drive it), but
  don't assume the vision wording is deterministic.
- **Tracker identity continuity is weak** with only 2 widely-spaced samples
  (`SimpleCentroidTracker` was designed for dense, roughly-continuous
  tracking). Treat `Porc_N` IDs as best-effort, not authoritative, across
  the baseline→final gap.
- **`pkill -f <pattern>` self-match risk over SSH**: if the pattern also
  appears in the invoking command's own command line (which it will, since
  sshd runs `sh -c "<literal command string>"`), pkill kills its own parent
  shell and the SSH session dies with no output. Kill by port (`fuser -k
  8000/tcp`) or PID instead when scripting remote process management.
- **`source some.env` in bash performs shell parameter expansion** on
  unquoted values — a key containing a literal `$2a$10$`-style substring
  gets silently corrupted (positional parameters `$2`, `$1` etc. expand to
  empty). This caused real, confusing 403s that looked like an expired key.
  Fix: use systemd's `EnvironmentFile=` (no shell involved) rather than
  sourcing env files with bash, whenever the value might contain `$`.
- **Local dev Python is 3.12, the VM's is 3.10** — `requirements.txt` uses
  `>=` constraints specifically so both work; don't tighten these back to
  exact pins without checking the VM's Python version.
- **DNS propagation for `api.cwco.tech`**: as of the last check, global
  public resolvers (1.1.1.1, 8.8.8.8) resolve it correctly, but some local
  networks were still serving stale/negative cached answers. If something
  "can't reach api.cwco.tech," check propagation before assuming the
  server-side setup broke.
- **Windows dev environment quirks** (only relevant if developing on this
  same Windows machine): Node.js had to be installed via `winget`; new
  PowerShell/Bash tool invocations don't pick up freshly-installed-program
  PATH changes without an explicit refresh or a new process; `npm.cmd`
  can't be spawned directly in some contexts (shells out to `node` by name,
  which hits the same stale-PATH issue) — invoking
  `node.exe node_modules/vite/bin/vite.js webapp` directly sidesteps this.

## Testing status

- Full pipeline (tracking → 2 Omni calls → sensor analysis → Ultra call):
  verified working end-to-end, multiple times, with a real (rotated) Crusoe
  API key.
- Backend on the VM: verified via systemd-managed service, both over
  localhost (SSH) and through the public tunnel.
- Named tunnel (`api.cwco.tech`): verified working via direct IP-pinned
  curl bypass (`--resolve`); full public DNS resolution was still
  propagating as of the last check.
- Frontend: verified in-browser against the live backend (cached load +
  live run button), including the error-state path.
- Not yet tested: Cloudflare Pages deployment (not done yet), behavior
  under concurrent/overlapping `/run` calls (no locking — a second request
  while one is in flight would race), real (non-synthetic) sensor data.

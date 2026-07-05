"""
PigWatch backend -- thin FastAPI wrapper around the existing pipeline.

Exposes the pig tracking + Nano Omni + sensor + Ultra pipeline over HTTP so a
separate frontend (React on Cloudflare Pages) can trigger a run and fetch the
latest report, instead of everyone SSHing in and running the CLI scripts by
hand. All the actual logic still lives in pig_tracking_pipeline.py and
pig_stress_monitor.py -- this file only adds an HTTP layer + response caching
+ CORS on top of functions that already existed.

Env vars required: CRUSOE_API_KEY (same key used by the CLI scripts).
"""

import os
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

from pig_tracking_pipeline import run_pig_tracking, CRUSOE_BASE_URL
from pig_stress_monitor import analyze_sensors, build_summary, call_ultra

VIDEO_PATH = os.environ.get("PIGWATCH_VIDEO", "/opt/pigwatch/pigs_top_down.mp4")
SENSORS_PATH = os.environ.get("PIGWATCH_SENSORS", "/opt/pigwatch/sensors_sample.json")
ENCLOSURE_ID = os.environ.get("PIGWATCH_ENCLOSURE_ID", "01")

app = FastAPI(title="PigWatch API")

# Wide open for now -- tighten to the actual Cloudflare Pages origin once the
# frontend is deployed and its URL is known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_last_result: Optional[dict] = None


def parse_ultra_report(report_text: str) -> dict:
    """Splits Ultra's 4-line reply into named fields for easier frontend use.
    Falls back to putting everything in `raw` if the model didn't follow the
    exact format (still returned to the caller either way)."""
    fields = {"status": None, "whats_happening": None, "likely_cause": None,
              "recommended_action": None, "raw": report_text}
    label_map = {
        "STATUS": "status",
        "WHAT'S HAPPENING": "whats_happening",
        "LIKELY CAUSE": "likely_cause",
        "RECOMMENDED ACTION": "recommended_action",
    }
    for line in report_text.strip().split("\n"):
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = label_map.get(label.strip().upper())
        if key:
            fields[key] = value.strip()
    return fields


def run_pipeline() -> dict:
    crusoe_key = os.environ.get("CRUSOE_API_KEY")
    if not crusoe_key:
        raise HTTPException(status_code=500, detail="CRUSOE_API_KEY not set on server")

    if not Path(VIDEO_PATH).exists():
        raise HTTPException(status_code=500, detail=f"Video not found: {VIDEO_PATH}")
    if not Path(SENSORS_PATH).exists():
        raise HTTPException(status_code=500, detail=f"Sensor file not found: {SENSORS_PATH}")

    import json
    with open(SENSORS_PATH) as f:
        records = json.load(f)

    client = OpenAI(base_url=CRUSOE_BASE_URL, api_key=crusoe_key)
    omni_ticks = run_pig_tracking(VIDEO_PATH, client, log=lambda *a, **k: None)

    sensors = analyze_sensors(records)
    summary = build_summary(sensors, omni_ticks, enclosure_id=ENCLOSURE_ID)

    try:
        ultra_report_text = call_ultra(summary, crusoe_key)
        ultra_report = parse_ultra_report(ultra_report_text)
        ultra_error = None
    except Exception as e:
        ultra_report = None
        ultra_error = str(e)

    return {
        "generated_at": time.time(),
        "enclosure_id": ENCLOSURE_ID,
        "sensors": sensors,
        "omni_ticks": [
            {
                "tick": tk["tick"],
                "timestamp_s": tk["timestamp_s"],
                "n_pigs": len(tk["pigs"]),
                "pigs": tk["pigs"],
                "nano_omni_analysis": tk["nano_omni_analysis"],
            }
            for tk in omni_ticks
        ],
        "summary_sent_to_ultra": summary,
        "ultra_report": ultra_report,
        "ultra_error": ultra_error,
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/run")
def run():
    """Runs the full pipeline synchronously (tracking + 2 Omni calls + Ultra
    call) and caches the result. Takes a few seconds -- fine for the demo's
    once-at-start/once-at-end cadence, not meant for high-frequency polling."""
    global _last_result
    _last_result = run_pipeline()
    return _last_result


@app.get("/report")
def report():
    """Returns the most recent /run result without re-running the pipeline."""
    if _last_result is None:
        raise HTTPException(status_code=404, detail="No run yet -- POST /run first")
    return _last_result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""
Pig Tracking Pipeline (top-down video -> OpenCV positions -> synthetic 2D plane -> Nano Omni)

Pipeline:
1. Sample frames every --interval seconds (default 1s) and run them through
   the background subtractor continuously -- this keeps it properly adaptive
   to real motion (confirmed necessary: freezing the model or only reading 2
   raw frames total causes stationary pigs to be absorbed into "background").
2. Detect pig positions at every sampled tick using background subtraction +
   contour centroids, and update the tracker at every tick so identity/motion
   stays coherent.
3. Track pigs across ticks with a simple nearest-centroid tracker so IDs stay
   consistent (Porc_1 stays Porc_1 across ticks).
4. Render the tracked centroids as a clean synthetic "2D plane" image (dots on
   a rectangle), independent of the messy real camera frame.
5. Out of all the internally-tracked ticks, only 2 are sent to Nemotron Nano
   Omni via Crusoe's OpenAI-compatible API: the earliest usable tick (a
   "baseline" snapshot -- tick 0 itself is skipped since it's always empty,
   see calibration notes) and the final tick (the "current state" snapshot).
   This gives a real before/after read (e.g. dispersed early -> grouped by
   the end) instead of two snapshots that both land near the end. It also
   decouples detection cadence (needs to be dense for the background model
   to work) from API-call cadence (expensive, and a qualitative dispersion
   read doesn't change meaningfully faster than this).

Requirements:
    pip install opencv-python-headless numpy matplotlib openai

Usage:
    export CRUSOE_API_KEY="..."          # never hardcode this in the script
    python pig_tracking_pipeline.py --video path/to/video.mp4 --interval 1 \
        --pen-width-m 6 --pen-height-m 4 --save-frames-dir ./frames_debug

Tuning note:
    detect_centroids()'s min_area/max_area and the background subtractor's
    history/varThreshold are the knobs you'll need to adjust to your actual
    footage (lighting, pig size in pixels, floor contrast). Run once with
    --save-frames-dir and eyeball the PNGs before trusting the numbers.
"""

import argparse
import base64
import io
import os
import sys
import json
from dataclasses import dataclass
from typing import List, Dict, Tuple

import cv2
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from openai import OpenAI


CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1/"
NANO_OMNI_MODEL = "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B"


# --------------------------------------------------------------------------
# Detection tuning knobs -- adjust these against --save-frames-dir output.
# --------------------------------------------------------------------------
MIN_AREA = 3000
MAX_AREA = 200000
VAR_THRESHOLD = 250
BG_HISTORY = 200

# Tracker matching tuned for sparse sampling: consecutive ticks are seconds
# apart (not consecutive video frames), so pigs can move 100-400px between
# samples -- measured directly on pigs_top_down.mp4. A tight max_distance
# just mints a new ID every tick instead of matching real movement, and a
# high max_missed leaves stale (un-updated) dots on screen long after the
# animal has moved elsewhere.
TRACKER_MAX_DISTANCE = 250
TRACKER_MAX_MISSED = 1


# --------------------------------------------------------------------------
# 1. Detection: find pig-like blobs in a single frame via background subtraction
# --------------------------------------------------------------------------

def detect_centroids(frame, back_sub, min_area=MIN_AREA, max_area=MAX_AREA, debug=False, learning_rate=-1):
    """Returns a list of (x, y) pixel centroids for detected blobs.

    learning_rate is forwarded to back_sub.apply(): -1 is OpenCV's default
    (auto-adapt), 0 freezes the model and just queries it against the
    already-trained background -- use 0 when snapshots are sparse and you
    don't want this specific call's contents folded into "background".

    If debug=True, also returns (fg_mask, all_contours) so callers can render
    a diagnostic overlay on the real frame instead of just trusting numbers.
    """
    fg_mask = back_sub.apply(frame, learningRate=learning_rate)
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    centroids = []
    for c in contours:
        area = cv2.contourArea(c)
        if min_area <= area <= max_area:
            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            centroids.append((cx, cy))

    if debug:
        return centroids, fg_mask, contours
    return centroids


def render_debug_overlay(frame, centroids, contours, min_area, max_area):
    """Draws all raw contours (with area) and accepted centroids on the real
    frame so detection can be visually checked against actual pig positions."""
    overlay = frame.copy()
    for c in contours:
        area = cv2.contourArea(c)
        accepted = min_area <= area <= max_area
        color = (0, 255, 0) if accepted else (0, 0, 255)
        cv2.drawContours(overlay, [c], -1, color, 2)
        x, y, w, h = cv2.boundingRect(c)
        cv2.putText(overlay, f"{int(area)}", (x, max(0, y - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    for cx, cy in centroids:
        cv2.drawMarker(overlay, (int(cx), int(cy)), (255, 255, 0),
                        markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)
        cv2.circle(overlay, (int(cx), int(cy)), 6, (255, 255, 0), -1)

    return overlay


# --------------------------------------------------------------------------
# 2. Tracking: assign persistent IDs across frames (nearest-centroid match)
# --------------------------------------------------------------------------

@dataclass
class TrackedPig:
    pig_id: str
    x: float
    y: float
    missed_frames: int = 0


class SimpleCentroidTracker:
    """
    Minimal nearest-neighbor tracker. Good enough for a hackathon demo with a
    stable pig count and no heavy occlusion. Not a replacement for
    ByteTrack/DeepSORT if pigs cross paths a lot or the count changes often.
    """

    def __init__(self, max_distance=80, max_missed=3):
        self.next_id = 1
        self.tracks: Dict[str, TrackedPig] = {}
        self.max_distance = max_distance
        self.max_missed = max_missed

    def update(self, centroids: List[Tuple[float, float]]) -> Dict[str, TrackedPig]:
        unmatched = list(centroids)
        matched_ids = set()

        for pig_id, track in self.tracks.items():
            if not unmatched:
                break
            distances = [np.hypot(track.x - cx, track.y - cy) for cx, cy in unmatched]
            min_idx = int(np.argmin(distances))
            if distances[min_idx] <= self.max_distance:
                cx, cy = unmatched.pop(min_idx)
                track.x, track.y = cx, cy
                track.missed_frames = 0
                matched_ids.add(pig_id)

        for pig_id, track in list(self.tracks.items()):
            if pig_id not in matched_ids:
                track.missed_frames += 1
                if track.missed_frames > self.max_missed:
                    del self.tracks[pig_id]

        for cx, cy in unmatched:
            pig_id = f"Porc_{self.next_id}"
            self.next_id += 1
            self.tracks[pig_id] = TrackedPig(pig_id=pig_id, x=cx, y=cy)

        return self.tracks


# --------------------------------------------------------------------------
# 3. Render tracked positions as a clean synthetic "2D plane" frame
# --------------------------------------------------------------------------

def render_plane(tracks: Dict[str, TrackedPig], frame_w, frame_h, pen_width_m, pen_height_m):
    """Converts pixel-space centroids to a clean scatter plot on real pen
    dimensions and returns PNG bytes -- this is the image sent to Nano Omni."""
    fig, ax = plt.subplots(figsize=(6, 4), dpi=100)
    ax.set_xlim(0, pen_width_m)
    ax.set_ylim(0, pen_height_m)
    ax.set_facecolor("#e8dcc8")
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_yticks([])

    for pig_id, t in tracks.items():
        x_m = (t.x / frame_w) * pen_width_m
        y_m = (t.y / frame_h) * pen_height_m
        ax.scatter(x_m, y_m, s=220, c="#8b5a2b", edgecolors="black", zorder=3)
        ax.annotate(pig_id.replace("Porc_", "P"), (x_m, y_m),
                    ha="center", va="center", fontsize=7, color="white", zorder=4)

    ax.set_title(f"Top-down pen plane -- {len(tracks)} pigs detected", fontsize=9)

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def to_base64_png(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("utf-8")


# --------------------------------------------------------------------------
# 4. Send the rendered plane image to Nano Omni for a qualitative read
# --------------------------------------------------------------------------

def analyze_with_nano_omni(client: OpenAI, image_b64: str, tick_index: int, timestamp_s: float, n_pigs: int):
    prompt = f"""
Tu observes le plan 2D d'un enclos de porcs vu du dessus, genere a partir de
coordonnees suivies par tracking video (tick {tick_index}, t={timestamp_s:.1f}s,
{n_pigs} porcs detectes).

Analyse la disposition spatiale et reponds UNIQUEMENT en JSON strict:
{{
  "spatial_distribution": "grouped" | "dispersed" | "highly_dispersed",
  "clustering_notes": "breve observation en une phrase",
  "possible_concern": "aucune" | "chaleur" | "froid" | "stress" | "incertain"
}}
"""
    response = client.chat.completions.create(
        model=NANO_OMNI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_b64}},
                ],
            }
        ],
    )
    return json.loads(response.choices[0].message.content)


# --------------------------------------------------------------------------
# 5. Main loop
# --------------------------------------------------------------------------

def run_pig_tracking(video_path, client, interval=1.0, pen_width_m=6.0, pen_height_m=4.0,
                      save_frames_dir=None, log=print):
    """Runs detection+tracking densely across the clip (so the background
    subtractor stays properly adaptive) but only calls Nano Omni for 2 ticks:
    the earliest usable one (baseline) and the final one (current state).
    Returns the same list structure that main() writes to --output, so
    callers (CLI or another script) get identical data. `log` defaults to
    print but can be swapped (e.g. no-op)
    by callers that want to control their own console output.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        sys.exit(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_s = frame_count / fps
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    back_sub = cv2.createBackgroundSubtractorMOG2(history=BG_HISTORY, varThreshold=VAR_THRESHOLD, detectShadows=False)
    tracker = SimpleCentroidTracker(max_distance=TRACKER_MAX_DISTANCE, max_missed=TRACKER_MAX_MISSED)

    if save_frames_dir:
        os.makedirs(save_frames_dir, exist_ok=True)

    log(f"Video: {video_path} | {duration_s:.1f}s @ {fps:.1f} fps | "
        f"sampling every {interval}s internally, calling Nano Omni for 2 of those ticks")

    # Dense internal sampling keeps the background subtractor continuously
    # adaptive (this is what actually detects pigs well -- see calibration
    # notes above). We still render/save a debug PNG per tick so calibration
    # stays checkable, but only 2 ticks get forwarded to the paid API call.
    ticks = []
    t = 0.0
    tick_index = 0
    while t < duration_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break

        centroids, fg_mask, contours = detect_centroids(frame, back_sub, debug=True)
        tracks = tracker.update(centroids)
        # Snapshot the tracker's state: self.tracks is mutated in place on
        # every update(), so later ticks would otherwise overwrite this one.
        tracks_snapshot = {pid: TrackedPig(pig_id=trk.pig_id, x=trk.x, y=trk.y,
                                            missed_frames=trk.missed_frames)
                            for pid, trk in tracks.items()}

        png_bytes = render_plane(tracks_snapshot, frame_w, frame_h, pen_width_m, pen_height_m)

        if save_frames_dir:
            with open(os.path.join(save_frames_dir, f"tick_{tick_index:03d}.png"), "wb") as f:
                f.write(png_bytes)
            overlay = render_debug_overlay(frame, centroids, contours,
                                            min_area=MIN_AREA, max_area=MAX_AREA)
            cv2.imwrite(os.path.join(save_frames_dir, f"tick_{tick_index:03d}_overlay.png"), overlay)
            cv2.imwrite(os.path.join(save_frames_dir, f"tick_{tick_index:03d}_mask.png"), fg_mask)

        ticks.append({"tick_index": tick_index, "t": t, "tracks": tracks_snapshot, "png_bytes": png_bytes})

        tick_index += 1
        t += interval

    cap.release()

    if not ticks:
        sys.exit("No ticks sampled from video.")

    # Pick the earliest usable tick as the "baseline" snapshot and the final
    # tick as the "current state" snapshot -- tick 0 is always empty (MOG2
    # hasn't seen enough to tell foreground from background yet), so tick 1
    # is the earliest tick that actually reflects pig positions. This is what
    # lets the two Nano Omni calls show a real before/after (e.g. dispersed
    # early in the clip -> grouped by the end), instead of both snapshots
    # landing near the end where the state has already settled.
    first_call_tick = ticks[1] if len(ticks) > 1 else ticks[0]
    last_call_tick = ticks[-1]
    selected_ticks = [first_call_tick, last_call_tick]

    results = []
    for tk in selected_ticks:
        image_b64 = to_base64_png(tk["png_bytes"])
        log(f"  tick {tk['tick_index']} (t={tk['t']:.1f}s): {len(tk['tracks'])} pigs -> calling Nano Omni...")
        try:
            analysis = analyze_with_nano_omni(client, image_b64, tk["tick_index"], tk["t"], len(tk["tracks"]))
        except Exception as e:
            log(f"    [warn] Nano Omni call failed: {e}")
            analysis = {"error": str(e)}

        results.append({
            "tick": tk["tick_index"],
            "timestamp_s": tk["t"],
            "pigs": {
                pig_id: {"x_m": round((trk.x / frame_w) * pen_width_m, 2),
                         "y_m": round((trk.y / frame_h) * pen_height_m, 2)}
                for pig_id, trk in tk["tracks"].items()
            },
            "nano_omni_analysis": analysis,
        })

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="Path to top-down video file")
    parser.add_argument("--interval", type=float, default=1.0,
                         help="Seconds between internally tracked samples (background subtractor "
                              "stays continuously adaptive across these; only 2 are sent to Nano Omni)")
    parser.add_argument("--pen-width-m", type=float, default=6.0)
    parser.add_argument("--pen-height-m", type=float, default=4.0)
    parser.add_argument("--output", default="pig_tracking_results.json")
    parser.add_argument("--save-frames-dir", default=None,
                         help="Optional dir to save each rendered PNG for inspection/demo")
    args = parser.parse_args()

    api_key = os.environ.get("CRUSOE_API_KEY")
    if not api_key:
        sys.exit("Set CRUSOE_API_KEY as an environment variable before running this script.")

    client = OpenAI(base_url=CRUSOE_BASE_URL, api_key=api_key)

    results = run_pig_tracking(args.video, client, interval=args.interval,
                                pen_width_m=args.pen_width_m, pen_height_m=args.pen_height_m,
                                save_frames_dir=args.save_frames_dir)

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(results)} Nano Omni calls written to {args.output}")


if __name__ == "__main__":
    main()

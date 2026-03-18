# Hand Gesture Volume Control

> Control your Mac, Windows, or Linux system volume in real-time using just
> your hand in front of a webcam — no keyboard, no mouse required.

Pinch your **thumb and index finger** together to lower the volume. Spread
them apart to raise it. Hold the pinch for half a second to toggle mute.

---

## Demo

| Gesture | Result |
|---|---|
| Fingers spread wide | 100 % volume |
| Fingers halfway apart | ~50 % volume |
| Fingers almost touching | 0 % volume |
| Hold pinch ~0.5 s | Mute / unmute toggle |
| Press `Q` | Quit |

---

## Features

- Real-time hand tracking via **MediaPipe Tasks API** — detects and tracks a hand at ~30 fps on Apple Silicon
- **Euclidean distance** between thumb tip and index fingertip drives volume (0–100 %)
- **Rolling-average smoothing** (8-frame window) eliminates jitter
- **Mute-hold gesture** — sustained pinch toggles mute without extra hardware
- Live **OpenCV** window showing:
  - Full 21-point hand skeleton overlay
  - Cyan connector line + magenta circles on tracked fingertips
  - Vertical volume bar (green = active, blue = muted)
  - Mute-hold countdown arc
  - FPS counter and hand-detection status
- Cross-platform: **macOS**, **Windows**, **Linux**
- Model auto-downloaded on first run (~29 MB, cached locally)
- Single-file implementation — easy to read, extend, and embed

---

## Requirements

- Python **3.9 – 3.11** — MediaPipe does not yet support 3.12+
  - macOS (Homebrew): `brew install python@3.11`
  - Windows / Linux: download from [python.org](https://python.org)
- A working webcam (built-in or USB)
- macOS 12+, Windows 10/11, or Linux with ALSA/PulseAudio

---

## Setup

### 1 — Clone the repo

```bash
git clone https://github.com/rgcurling/Hand-Gesture-Volume-Control.git
cd Hand-Gesture-Volume-Control
```

### 2 — Create a virtual environment

```bash
# macOS / Linux  (use python3.11 explicitly to stay in the supported range)
python3.11 -m venv .venv
source .venv/bin/activate

# Windows
py -3.11 -m venv .venv
.venv\Scripts\activate
```

### 3 — Install dependencies

```bash
pip install -r requirements.txt
```

**Platform notes**

| Platform | Volume backend | Extra steps |
|---|---|---|
| macOS | `osascript` (built-in) | None |
| Windows | `pycaw` + `comtypes` | Installed automatically by pip |
| Linux | `amixer` (ALSA) | `sudo apt install alsa-utils` |

### 4 — Run

```bash
python src/hand_volume_control.py
```

On **first run** the MediaPipe hand-landmarker model (~29 MB) is downloaded
automatically to `models/hand_landmarker.task` and reused on every subsequent
run — no manual download needed.

A window titled **"Hand Volume Control"** opens. Hold your hand up in front of
the webcam with the palm facing the camera, then move your thumb and index
finger to control the volume.

> **macOS tip:** if the camera doesn't open, go to
> **System Settings → Privacy & Security → Camera** and enable access for
> Terminal (or your terminal app). This is a one-time step.

> **Multiple cameras:** if the wrong camera opens, change `CAMERA_INDEX` at
> the top of `src/hand_volume_control.py` (try `0`, `1`, `2` …).

---

## How the mapping works

```
Finger gap (pixels)
        │
  ≤ 30 ─┼─ 0 %   (pinched / almost touching)
        │
 30–220 ─┼─ linear interpolation
        │
 ≥ 220 ─┼─ 100 % (fully spread)
```

1. **Detect** — MediaPipe's `HandLandmarker` (Tasks API) returns normalised
   (x, y) coordinates for 21 hand landmarks. Landmark 4 = thumb tip,
   landmark 8 = index fingertip.
2. **Measure** — Euclidean distance `√((x₂−x₁)² + (y₂−y₁)²)` is computed in
   pixels for the current frame resolution.
3. **Map** — Distance is linearly interpolated between `MIN_DIST_PX` and
   `MAX_DIST_PX` and clamped to [0, 1].
4. **Smooth** — The last 8 mapped values are rolling-averaged to absorb
   frame-to-frame noise before reaching the OS.
5. **Debounce** — The OS volume API is only called when the smoothed value
   changes by more than 1.5 %, avoiding redundant system calls every frame.

---

## Configuration

All tunable constants are at the top of [src/hand_volume_control.py](src/hand_volume_control.py):

| Constant | Default | Description |
|---|---|---|
| `CAMERA_INDEX` | `1` | Webcam index — `0` is often Continuity Camera on Mac; try `1` for built-in |
| `FRAME_WIDTH / HEIGHT` | `1280 × 720` | Capture resolution |
| `MIN_DIST_PX` | `30` | Finger gap (px) → 0 % volume |
| `MAX_DIST_PX` | `220` | Finger gap (px) → 100 % volume |
| `SMOOTH_WINDOW` | `8` | Rolling-average window (frames) |
| `MUTE_THRESHOLD_PX` | `42` | Gap below which mute countdown starts |
| `MUTE_HOLD_FRAMES` | `20` | Frames to hold pinch to toggle mute (~0.5 s at 30 fps) |
| `VOL_UPDATE_EPSILON` | `0.015` | Minimum delta before calling OS volume API |
| `MIN_DETECTION_CONF` | `0.75` | MediaPipe hand detection confidence |

---

## Project structure

```
src/
  hand_volume_control.py   # complete single-file implementation
models/                    # auto-created; holds the downloaded .task model
requirements.txt           # pip dependencies
.gitignore
README.md
LICENSE
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

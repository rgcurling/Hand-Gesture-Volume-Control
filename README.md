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

- Real-time hand tracking via **MediaPipe** — detects and tracks a hand at ~30 fps
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
- Single-file implementation — easy to read, extend, and embed

---

## Requirements

- Python **3.8 – 3.11** (MediaPipe does not yet support 3.12+)
- A working webcam
- macOS, Windows 10/11, or a Linux desktop with ALSA/PulseAudio

---

## Setup

### 1 — Clone the repo

```bash
git clone https://github.com/rgcurling/Hand-Gesture-Volume-Control.git
cd Hand-Gesture-Volume-Control
```

### 2 — Create a virtual environment

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
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

A window titled **"Hand Volume Control"** opens. Hold your hand up in front of
the webcam with the palm facing the camera, and move your thumb and index
finger to control the volume.

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

1. **Detect** — MediaPipe returns normalised (x, y) for 21 hand landmarks.
   Landmark 4 = thumb tip, landmark 8 = index fingertip.
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
| `CAMERA_INDEX` | `0` | Webcam index (try `1` if `0` doesn't work) |
| `FRAME_WIDTH / HEIGHT` | `1280 × 720` | Capture resolution |
| `MIN_DIST_PX` | `30` | Finger gap → 0 % volume |
| `MAX_DIST_PX` | `220` | Finger gap → 100 % volume |
| `SMOOTH_WINDOW` | `8` | Rolling-average window (frames) |
| `MUTE_THRESHOLD_PX` | `42` | Gap below which mute countdown starts |
| `MUTE_HOLD_FRAMES` | `20` | Frames to hold pinch to toggle mute (~0.5 s) |
| `VOL_UPDATE_EPSILON` | `0.015` | Minimum delta before calling OS API |
| `MIN_DETECTION_CONF` | `0.75` | MediaPipe detection confidence threshold |

---

## Project structure

```
src/
  hand_volume_control.py   # complete single-file implementation
requirements.txt           # pip dependencies
README.md
LICENSE
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

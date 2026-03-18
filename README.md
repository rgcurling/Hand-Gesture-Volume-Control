# Hand Gesture Volume Control

Control your computer's master volume in real-time by moving your hand in
front of a webcam — no keyboard, no mouse. Pinch your thumb and index finger
together to lower the volume; spread them apart to raise it.

## Features

- Real-time hand tracking via MediaPipe (runs at ~30 fps on a modern laptop)
- Euclidean distance between **thumb tip** and **index finger tip** maps to
  0–100 % system volume
- Rolling-average smoothing prevents erratic jumps
- Hold the pinch for ~0.5 s to **toggle mute / unmute**
- Live OpenCV window with:
  - Full 21-point hand skeleton overlay
  - Cyan line + magenta circles on the tracked fingertips
  - Vertical volume bar (green = active, blue = muted)
  - Mute-hold progress arc
  - FPS counter and hand-detection status
- Cross-platform: **macOS**, **Windows**, **Linux**
- Press `Q` to quit cleanly

## Setup

### 1 — Create a virtual environment

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
.venv\Scripts\activate
```

### 2 — Install dependencies

```bash
pip install -r requirements.txt
```

> **Linux only** — `amixer` must also be available:
> ```bash
> sudo apt install alsa-utils      # Debian / Ubuntu
> sudo pacman -S alsa-utils        # Arch
> ```
>
> **macOS** — no extra packages; the script uses the built-in `osascript`.
>
> **Windows** — `pycaw` and `comtypes` are installed automatically by pip.

### 3 — Run

```bash
python src/hand_volume_control.py
```

A window titled **"Hand Volume Control"** will open. Hold your hand up so the
webcam can see it clearly.

## Gestures

| Gesture | Action |
|---|---|
| Spread thumb & index apart | Increase volume |
| Pinch thumb & index together | Decrease volume |
| Hold pinch for ~0.5 s | Toggle mute / unmute |
| Press `Q` | Quit |

## How the gesture-to-volume mapping works

```
Pixel distance between fingertips
                │
    ≤ 30 px ────┼──── 0 % volume   (fingers almost touching)
                │
   30 – 220 px  ┼──── linear interpolation
                │
   ≥ 220 px ────┼──── 100 % volume  (fingers wide open)
```

1. **Measure** — MediaPipe provides normalised (x, y, z) coordinates for 21
   hand landmarks. Landmark 4 is the thumb tip; landmark 8 is the index
   fingertip. Both are converted to pixel coordinates for the current frame
   resolution.

2. **Distance** — The Euclidean distance `√((x₂−x₁)² + (y₂−y₁)²)` gives a
   pixel gap that grows as the fingers spread and shrinks as they pinch.

3. **Map** — The raw distance is linearly interpolated between `MIN_DIST_PX`
   (= 0 %) and `MAX_DIST_PX` (= 100 %) and clamped to [0, 1].

4. **Smooth** — The last 8 mapped values are averaged (rolling window) to
   absorb frame-to-frame jitter before the value is sent to the OS.

5. **Debounce** — The OS volume API is only called when the smoothed value
   changes by more than 1.5 percentage points, preventing unnecessary system
   calls every frame.

## Project structure

```
src/
  hand_volume_control.py   # complete implementation
requirements.txt
README.md
```

## Configuration

All tunable parameters live at the top of `src/hand_volume_control.py`:

| Constant | Default | Effect |
|---|---|---|
| `MIN_DIST_PX` | 30 | Finger gap (px) mapped to 0 % volume |
| `MAX_DIST_PX` | 220 | Finger gap (px) mapped to 100 % volume |
| `SMOOTH_WINDOW` | 8 | Rolling-average window size (frames) |
| `MUTE_THRESHOLD_PX` | 42 | Gap below which mute countdown starts |
| `MUTE_HOLD_FRAMES` | 20 | Frames to hold pinch to toggle mute (~0.5 s) |
| `VOL_UPDATE_EPSILON` | 0.015 | Minimum change before calling OS API |
| `CAMERA_INDEX` | 0 | Webcam index (try 1 if 0 doesn't work) |

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

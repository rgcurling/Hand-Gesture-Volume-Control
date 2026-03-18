#!/usr/bin/env python3
"""
Hand Gesture Volume Control
============================
Control your system's master volume in real-time by pinching and spreading
your thumb and index finger in front of a webcam.

Gestures
--------
  Spread thumb & index apart  →  increase volume
  Pinch thumb & index together →  decrease volume
  Hold pinch for ~0.5 s        →  toggle mute / unmute
  Press Q                      →  quit cleanly

Quick-start
-----------
  python -m venv .venv
  source .venv/bin/activate          # macOS / Linux
  .venv\\Scripts\\activate             # Windows
  pip install -r requirements.txt
  python src/hand_volume_control.py

Platform support
----------------
  macOS   – osascript (built-in, no extra package needed)
  Windows – pycaw + comtypes  (install from requirements.txt)
  Linux   – amixer / PulseAudio  (install alsa-utils or pulseaudio-utils)
"""

import collections
import math
import platform
import subprocess
import sys
import time

import cv2
import mediapipe as mp
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Camera
CAMERA_INDEX  = 0
FRAME_WIDTH   = 1280
FRAME_HEIGHT  = 720
TARGET_FPS    = 30

# MediaPipe confidence thresholds
MIN_DETECTION_CONF = 0.75
MIN_TRACKING_CONF  = 0.75

# Finger-distance → volume mapping (pixels at target resolution)
#   Fingers almost touching  →  MIN_DIST_PX  →  0 % volume
#   Fingers fully spread     →  MAX_DIST_PX  →  100 % volume
MIN_DIST_PX = 30
MAX_DIST_PX = 220

# Smoothing: rolling-average window length (frames)
# Larger = smoother but slightly more lag
SMOOTH_WINDOW = 8

# Mute gesture: distance below this threshold (px) triggers mute countdown
MUTE_THRESHOLD_PX = 42
# Number of consecutive frames the mute gesture must be held to toggle mute
MUTE_HOLD_FRAMES  = 20      # ≈ 0.5 s at 30 fps

# Only call the OS volume API when volume changes by at least this amount
# (avoids hammering the OS API every frame)
VOL_UPDATE_EPSILON = 0.015  # 1.5 %

# ─── UI geometry ─────────────────────────────────────────────────────────────
BAR_X = 50      # left edge of volume bar
BAR_Y = 150     # top edge of volume bar
BAR_W = 32      # bar width
BAR_H = 300     # bar height (full scale)

# ─── Colours (BGR) ───────────────────────────────────────────────────────────
C_TEXT        = (255, 255, 255)
C_DIM         = (160, 160, 160)
C_LINE        = (0,   255, 255)   # cyan  – thumb-to-index connector
C_TIP         = (255,   0, 255)   # magenta – fingertip circles
C_MID         = (0,   255, 255)   # midpoint dot
C_BAR_BG      = (55,   55,  55)
C_BAR_ACTIVE  = (0,   210,   0)   # green  – normal volume bar
C_BAR_MUTED   = (200,  80,   0)   # blue   – muted volume bar
C_MUTE_WARN   = (0,   130, 255)   # orange – mute-hold progress
C_MUTE_LABEL  = (200,  80,   0)   # blue   – "MUTED" text

# ─────────────────────────────────────────────────────────────────────────────
# OS-specific volume control
# ─────────────────────────────────────────────────────────────────────────────

OS = platform.system()  # 'Darwin', 'Windows', 'Linux'

# Windows: initialise pycaw once at module load so every frame doesn't re-open
if OS == "Windows":
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        _win_devices   = AudioUtilities.GetSpeakers()
        _win_iface     = _win_devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None
        )
        _win_vol_ctrl  = cast(_win_iface, POINTER(IAudioEndpointVolume))
    except ImportError:
        print("[ERROR] pycaw is not installed. Run: pip install pycaw comtypes")
        sys.exit(1)


def get_system_volume() -> float:
    """Return the current master volume as a float in [0.0, 1.0]."""
    if OS == "Windows":
        return float(_win_vol_ctrl.GetMasterVolumeLevelScalar())

    if OS == "Darwin":
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=1,
        )
        try:
            return float(result.stdout.strip()) / 100.0
        except ValueError:
            return 0.5

    # Linux – amixer
    result = subprocess.run(
        ["amixer", "get", "Master"], capture_output=True, text=True, timeout=1
    )
    import re
    m = re.search(r"\[(\d+)%\]", result.stdout)
    return float(m.group(1)) / 100.0 if m else 0.5


def set_system_volume(scalar: float) -> None:
    """Set master volume. scalar must be in [0.0, 1.0]."""
    scalar = max(0.0, min(1.0, scalar))

    if OS == "Windows":
        _win_vol_ctrl.SetMasterVolumeLevelScalar(scalar, None)
        return

    if OS == "Darwin":
        # Popen (non-blocking) so the main loop is never stalled
        subprocess.Popen(
            ["osascript", "-e", f"set volume output volume {int(scalar * 100)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    # Linux
    subprocess.Popen(
        ["amixer", "-D", "pulse", "sset", "Master", f"{int(scalar * 100)}%"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def set_system_mute(muted: bool) -> None:
    """Mute or unmute the master output."""
    if OS == "Windows":
        _win_vol_ctrl.SetMute(int(muted), None)
        return

    if OS == "Darwin":
        val = "true" if muted else "false"
        subprocess.Popen(
            ["osascript", "-e", f"set volume output muted {val}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    # Linux
    toggle = "mute" if muted else "unmute"
    subprocess.Popen(
        ["amixer", "-D", "pulse", "sset", "Master", toggle],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Camera
# ─────────────────────────────────────────────────────────────────────────────

def init_camera(index: int = CAMERA_INDEX) -> cv2.VideoCapture:
    """Open the webcam and configure resolution / frame-rate."""
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(
            f"Cannot open camera at index {index}. "
            "Check that your webcam is connected and not in use by another app."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS,          TARGET_FPS)
    # Keep the internal buffer small so we always process the latest frame
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ─────────────────────────────────────────────────────────────────────────────
# Hand detection
# ─────────────────────────────────────────────────────────────────────────────

def init_hand_detector():
    """Return a configured MediaPipe Hands detector."""
    mp_hands = mp.solutions.hands
    detector = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=MIN_DETECTION_CONF,
        min_tracking_confidence=MIN_TRACKING_CONF,
    )
    return detector, mp_hands


def process_frame(frame_rgb: np.ndarray, detector) -> tuple:
    """
    Run hand detection on an RGB frame.

    Returns
    -------
    results   : raw MediaPipe result object (needed for skeleton drawing)
    landmarks : list of (x_px, y_px) for all 21 hand landmarks, or None
    """
    results = detector.process(frame_rgb)
    if not results.multi_hand_landmarks:
        return results, None

    h, w = frame_rgb.shape[:2]
    hand = results.multi_hand_landmarks[0]
    landmarks = [
        (int(lm.x * w), int(lm.y * h))
        for lm in hand.landmark
    ]
    return results, landmarks


# ─────────────────────────────────────────────────────────────────────────────
# Geometry & mapping
# ─────────────────────────────────────────────────────────────────────────────

def euclidean(p1: tuple, p2: tuple) -> float:
    """Euclidean distance between two (x, y) points in pixels."""
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def dist_to_volume(
    dist_px: float,
    min_dist: float = MIN_DIST_PX,
    max_dist: float = MAX_DIST_PX,
) -> float:
    """
    Map pixel distance between thumb tip and index tip to a [0.0, 1.0] scalar.

    Mapping
    -------
      dist ≤ min_dist  →  0.0  (fingers touching / pinched → silent)
      dist ≥ max_dist  →  1.0  (fingers wide open → full volume)
      in-between       →  linear interpolation, clamped

    The caller applies smoothing before this value reaches the OS API.
    """
    if max_dist <= min_dist:
        return 0.0
    raw = (dist_px - min_dist) / (max_dist - min_dist)
    return max(0.0, min(1.0, raw))


# ─────────────────────────────────────────────────────────────────────────────
# UI drawing helpers
# ─────────────────────────────────────────────────────────────────────────────

def draw_hand_skeleton(
    frame: np.ndarray,
    mp_results,
    mp_hands_module,
    draw_utils,
    draw_styles,
) -> None:
    """Draw the full 21-point hand skeleton using MediaPipe's built-in styles."""
    if not mp_results.multi_hand_landmarks:
        return
    for hand_lm in mp_results.multi_hand_landmarks:
        draw_utils.draw_landmarks(
            frame,
            hand_lm,
            mp_hands_module.HAND_CONNECTIONS,
            draw_styles.get_default_hand_landmarks_style(),
            draw_styles.get_default_hand_connections_style(),
        )


def draw_fingertip_overlay(
    frame: np.ndarray,
    thumb_pt: tuple,
    index_pt: tuple,
) -> None:
    """
    Highlight the thumb tip, index tip, and the line between them.
    Drawn on top of the skeleton so it is clearly visible.
    """
    mid_pt = (
        (thumb_pt[0] + index_pt[0]) // 2,
        (thumb_pt[1] + index_pt[1]) // 2,
    )
    cv2.line(frame, thumb_pt, index_pt, C_LINE, 3, cv2.LINE_AA)
    cv2.circle(frame, thumb_pt, 14, C_TIP,  cv2.FILLED)
    cv2.circle(frame, index_pt, 14, C_TIP,  cv2.FILLED)
    cv2.circle(frame, thumb_pt, 14, C_TEXT,  1, cv2.LINE_AA)
    cv2.circle(frame, index_pt, 14, C_TEXT,  1, cv2.LINE_AA)
    cv2.circle(frame, mid_pt,   8,  C_MID,  cv2.FILLED)


def draw_volume_bar(
    frame: np.ndarray,
    volume_scalar: float,
    is_muted: bool,
) -> None:
    """
    Vertical bar on the left side of the frame.
      Green fill   = active volume level
      Blue fill    = muted
    The bar fills from the bottom upwards, matching natural intuition.
    """
    x, y, w, h = BAR_X, BAR_Y, BAR_W, BAR_H

    # Background track
    cv2.rectangle(frame, (x, y), (x + w, y + h), C_BAR_BG, cv2.FILLED)

    # Filled portion proportional to current volume
    fill_h = int(h * volume_scalar)
    bar_color = C_BAR_MUTED if is_muted else C_BAR_ACTIVE
    if fill_h > 0:
        cv2.rectangle(
            frame,
            (x,     y + h - fill_h),
            (x + w, y + h),
            bar_color, cv2.FILLED,
        )

    # Border
    cv2.rectangle(frame, (x, y), (x + w, y + h), C_DIM, 1)

    # Percentage label below the bar
    label = f"{int(volume_scalar * 100)}%"
    cv2.putText(
        frame, label, (x - 2, y + h + 26),
        cv2.FONT_HERSHEY_SIMPLEX, 0.85, C_TEXT, 2, cv2.LINE_AA,
    )

    # Scale markers
    cv2.putText(
        frame, "MAX", (x + w + 8, y + 14),
        cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_DIM, 1, cv2.LINE_AA,
    )
    cv2.putText(
        frame, "MIN", (x + w + 8, y + h),
        cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_DIM, 1, cv2.LINE_AA,
    )

    # Gesture range markers (visual guide)
    min_y = y + h - int(h * 0.0)   # MIN_DIST → bottom
    max_y = y + h - int(h * 1.0)   # MAX_DIST → top
    cv2.line(frame, (x - 6, min_y), (x, min_y), C_DIM, 1)
    cv2.line(frame, (x - 6, max_y), (x, max_y), C_DIM, 1)


def draw_mute_ui(
    frame: np.ndarray,
    is_muted: bool,
    mute_progress: float,
) -> None:
    """
    Show a centred 'MUTED' banner when audio is muted, or an arc progress
    indicator while the mute-hold gesture is being held.
    """
    h, w = frame.shape[:2]
    cx, cy = w // 2, 65

    if is_muted:
        text = "MUTED"
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.5, 3)
        # Faint background pill
        cv2.rectangle(
            frame,
            ((w - tw) // 2 - 16, 30),
            ((w + tw) // 2 + 16, 90),
            (20, 20, 20), cv2.FILLED,
        )
        cv2.putText(
            frame, text, ((w - tw) // 2, 82),
            cv2.FONT_HERSHEY_SIMPLEX, 1.5, C_MUTE_LABEL, 3, cv2.LINE_AA,
        )
    elif mute_progress > 0.0:
        # Arc sweeps clockwise as the user holds the pinch gesture
        angle = int(360 * mute_progress)
        radius = 28
        cv2.ellipse(
            frame, (cx, cy), (radius, radius),
            -90, 0, angle,
            C_MUTE_WARN, 4, cv2.LINE_AA,
        )
        cv2.putText(
            frame, "Hold to mute",
            (cx - 58, cy + radius + 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, C_MUTE_WARN, 1, cv2.LINE_AA,
        )


def draw_hud(
    frame: np.ndarray,
    fps: float,
    dist_px: float,
    hand_detected: bool,
) -> None:
    """Top-right HUD showing FPS, detection status, and raw finger distance."""
    fw = frame.shape[1]
    cv2.putText(
        frame, f"FPS: {fps:.0f}",
        (fw - 115, 34),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, C_TEXT, 2, cv2.LINE_AA,
    )
    status_text  = "Hand: detected" if hand_detected else "Hand: searching…"
    status_color = (0, 210, 0) if hand_detected else (80, 80, 200)
    cv2.putText(
        frame, status_text,
        (fw - 215, 62),
        cv2.FONT_HERSHEY_SIMPLEX, 0.58, status_color, 2, cv2.LINE_AA,
    )
    if hand_detected:
        cv2.putText(
            frame, f"Gap: {dist_px:.0f} px",
            (fw - 175, 86),
            cv2.FONT_HERSHEY_SIMPLEX, 0.52, C_DIM, 1, cv2.LINE_AA,
        )


def draw_instructions(frame: np.ndarray) -> None:
    """Small legend at the bottom-left corner."""
    fh = frame.shape[0]
    lines = [
        "Q  – quit",
        "Hold pinch  – mute toggle",
        "Pinch closer  – quieter",
        "Spread apart  – louder",
    ]
    for i, line in enumerate(lines):
        cv2.putText(
            frame, line,
            (12, fh - 14 - i * 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1, cv2.LINE_AA,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[INFO] Platform: {OS}")
    print("[INFO] Initialising camera…")
    cap = init_camera()

    print("[INFO] Initialising MediaPipe hand detector…")
    detector, mp_hands = init_hand_detector()
    mp_draw        = mp.solutions.drawing_utils
    mp_draw_styles = mp.solutions.drawing_styles

    # ── State ────────────────────────────────────────────────────────────────
    # Rolling-average buffer for volume smoothing
    smooth_buf = collections.deque(maxlen=SMOOTH_WINDOW)

    # Mute-gesture state
    mute_hold_count  = 0    # consecutive frames the pinch has been held
    mute_released    = True  # True when fingers are outside the mute zone
    is_muted         = False

    # Volume tracking (avoid redundant OS calls)
    last_sent_vol = -1.0
    dist_px       = 0.0     # last measured finger gap (displayed in HUD)

    # Seed the smooth buffer from the actual current volume
    try:
        current_vol = get_system_volume()
    except Exception:
        current_vol = 0.5
    smooth_buf.extend([current_vol] * SMOOTH_WINDOW)

    # FPS measurement
    fps_buf   = collections.deque(maxlen=30)
    prev_time = time.perf_counter()

    print("[INFO] Running — press Q in the video window to quit.\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[ERROR] Failed to read camera frame — exiting.")
            break

        # Flip horizontally so the feed acts like a mirror (more intuitive)
        frame = cv2.flip(frame, 1)

        # ── FPS counter ──────────────────────────────────────────────────────
        now = time.perf_counter()
        fps_buf.append(1.0 / max(now - prev_time, 1e-9))
        prev_time = now
        fps = sum(fps_buf) / len(fps_buf)

        # ── Hand detection ───────────────────────────────────────────────────
        # MediaPipe requires an RGB image; frame is BGR from OpenCV
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_results, landmarks = process_frame(frame_rgb, detector)
        hand_detected = landmarks is not None

        # ── Gesture processing ───────────────────────────────────────────────
        mute_progress = 0.0

        if hand_detected:
            # Landmark indices defined by MediaPipe:
            #   4  = THUMB_TIP
            #   8  = INDEX_FINGER_TIP
            thumb_pt = landmarks[mp_hands.HandLandmark.THUMB_TIP]
            index_pt = landmarks[mp_hands.HandLandmark.INDEX_FINGER_TIP]

            dist_px  = euclidean(thumb_pt, index_pt)
            raw_vol  = dist_to_volume(dist_px)

            # ── Smoothing: rolling average ────────────────────────────────
            smooth_buf.append(raw_vol)
            smoothed_vol = sum(smooth_buf) / len(smooth_buf)

            # ── Mute gesture ──────────────────────────────────────────────
            # The user must bring fingers within MUTE_THRESHOLD_PX AND hold
            # them there for MUTE_HOLD_FRAMES before the mute toggles.
            # 'mute_released' prevents continuous toggling while held.
            if dist_px < MUTE_THRESHOLD_PX:
                if mute_released:
                    mute_hold_count += 1
                    if mute_hold_count >= MUTE_HOLD_FRAMES:
                        is_muted = not is_muted
                        set_system_mute(is_muted)
                        mute_hold_count = 0
                        mute_released   = False  # require a release first
            else:
                mute_hold_count = 0
                mute_released   = True

            mute_progress = min(mute_hold_count / MUTE_HOLD_FRAMES, 1.0)

            # ── Volume update ─────────────────────────────────────────────
            # Skip OS call when muted (let the mute flag do the work) and
            # when the change is below the update epsilon (debounce).
            if not is_muted:
                if abs(smoothed_vol - last_sent_vol) >= VOL_UPDATE_EPSILON:
                    set_system_volume(smoothed_vol)
                    last_sent_vol = smoothed_vol

            current_vol = smoothed_vol

            # ── Draw overlays ─────────────────────────────────────────────
            draw_hand_skeleton(frame, mp_results, mp_hands, mp_draw, mp_draw_styles)
            draw_fingertip_overlay(frame, thumb_pt, index_pt)

        # ── Always-on UI ─────────────────────────────────────────────────────
        draw_volume_bar(frame, current_vol, is_muted)
        draw_mute_ui(frame, is_muted, mute_progress if hand_detected else 0.0)
        draw_hud(frame, fps, dist_px, hand_detected)
        draw_instructions(frame)

        cv2.imshow("Hand Volume Control  |  press Q to quit", frame)

        # Exit on Q or on window close
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q") or cv2.getWindowProperty(
            "Hand Volume Control  |  press Q to quit",
            cv2.WND_PROP_VISIBLE,
        ) < 1:
            break

    # ── Cleanup ──────────────────────────────────────────────────────────────
    cap.release()
    detector.close()
    cv2.destroyAllWindows()
    print("[INFO] Exited cleanly.")


if __name__ == "__main__":
    main()

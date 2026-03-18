#!/usr/bin/env python3
"""
Hand Gesture Volume Control
============================
Control your system's master volume in real-time by pinching and spreading
your thumb and index finger in front of a webcam.

Gestures
--------
  Spread thumb & index apart   →  increase volume
  Pinch thumb & index together →  decrease volume
  Hold pinch for ~0.5 s        →  toggle mute / unmute
  Press Q                      →  quit cleanly

Quick-start
-----------
  python3.11 -m venv .venv
  source .venv/bin/activate          # macOS / Linux
  .venv\\Scripts\\activate             # Windows
  pip install -r requirements.txt
  python src/hand_volume_control.py

Platform support
----------------
  macOS   – osascript (built-in, no extra package needed)
  Windows – pycaw + comtypes  (install from requirements.txt)
  Linux   – amixer / PulseAudio  (install alsa-utils or pulseaudio-utils)

Model
-----
  On first run, the MediaPipe hand-landmarker model (~29 MB) is downloaded
  automatically to models/hand_landmarker.task and reused on future runs.
"""

import collections
import math
import os
import platform
import subprocess
import sys
import time
import urllib.request

import cv2
import mediapipe as mp
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Camera
CAMERA_INDEX  = 1       # 0 = first camera; change if wrong device is picked
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
SMOOTH_WINDOW = 8

# Mute gesture: distance below this threshold (px) triggers mute countdown
MUTE_THRESHOLD_PX = 42
# Frames the mute gesture must be held to toggle mute  (~0.5 s at 30 fps)
MUTE_HOLD_FRAMES  = 20

# Only call the OS volume API when volume changes by at least this amount
VOL_UPDATE_EPSILON = 0.015  # 1.5 %

# ─── MediaPipe model ──────────────────────────────────────────────────────────
# The hand landmarker model is downloaded once and cached locally.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR   = os.path.join(_SCRIPT_DIR, "..", "models")
MODEL_PATH  = os.path.join(MODEL_DIR, "hand_landmarker.task")
MODEL_URL   = (
    "https://storage.googleapis.com/mediapipe-models/"
    "hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
)

# ─── Hand landmark indices (MediaPipe convention) ─────────────────────────────
THUMB_TIP = 4
INDEX_TIP = 8

# ─── Hand skeleton connections (21 landmarks, 0-indexed) ──────────────────────
# Used to draw the skeleton without the legacy mp.solutions drawing utilities.
HAND_CONNECTIONS = [
    # Thumb
    (0, 1), (1, 2), (2, 3), (3, 4),
    # Index finger
    (0, 5), (5, 6), (6, 7), (7, 8),
    # Middle finger
    (5, 9), (9, 10), (10, 11), (11, 12),
    # Ring finger
    (9, 13), (13, 14), (14, 15), (15, 16),
    # Pinky
    (13, 17), (17, 18), (18, 19), (19, 20),
    # Palm
    (0, 17),
]

# ─── UI geometry ─────────────────────────────────────────────────────────────
BAR_X = 50
BAR_Y = 150
BAR_W = 32
BAR_H = 300

# ─── Colours (BGR) ───────────────────────────────────────────────────────────
C_TEXT        = (255, 255, 255)
C_DIM         = (160, 160, 160)
C_LINE        = (0,   255, 255)   # cyan   – thumb-to-index connector
C_TIP         = (255,   0, 255)   # magenta – fingertip circles
C_MID         = (0,   255, 255)   # midpoint dot
C_BONE        = (80,  180,  80)   # green  – skeleton bones
C_JOINT       = (200, 200, 200)   # white  – skeleton joints
C_BAR_BG      = (55,   55,  55)
C_BAR_ACTIVE  = (0,   210,   0)   # green  – normal volume bar
C_BAR_MUTED   = (200,  80,   0)   # blue   – muted volume bar
C_MUTE_WARN   = (0,   130, 255)   # orange – mute-hold progress arc
C_MUTE_LABEL  = (200,  80,   0)   # blue   – "MUTED" text

# ─────────────────────────────────────────────────────────────────────────────
# OS-specific volume control
# ─────────────────────────────────────────────────────────────────────────────

OS = platform.system()  # 'Darwin', 'Windows', 'Linux'

if OS == "Windows":
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        _win_devices  = AudioUtilities.GetSpeakers()
        _win_iface    = _win_devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None
        )
        _win_vol_ctrl = cast(_win_iface, POINTER(IAudioEndpointVolume))
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
    # Linux
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
        subprocess.Popen(
            ["osascript", "-e", f"set volume output volume {int(scalar * 100)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
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
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)  # minimal buffer → lowest latency
    return cap


# ─────────────────────────────────────────────────────────────────────────────
# Hand detection  (MediaPipe Tasks API — works with mediapipe >= 0.10)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_model() -> None:
    """Download the hand landmarker model on first run."""
    if os.path.exists(MODEL_PATH):
        return
    os.makedirs(MODEL_DIR, exist_ok=True)
    print("[INFO] Downloading hand landmarker model (~29 MB) — one-time setup…")
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    print(f"[INFO] Model saved to {MODEL_PATH}")


def init_hand_detector():
    """Return a configured MediaPipe HandLandmarker (Tasks API)."""
    ensure_model()
    base_options = mp.tasks.BaseOptions(model_asset_path=MODEL_PATH)
    options = mp.tasks.vision.HandLandmarkerOptions(
        base_options=base_options,
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=MIN_DETECTION_CONF,
        min_hand_presence_confidence=MIN_TRACKING_CONF,
        min_tracking_confidence=MIN_TRACKING_CONF,
    )
    return mp.tasks.vision.HandLandmarker.create_from_options(options)


def process_frame(
    frame_rgb: np.ndarray,
    detector,
    timestamp_ms: int,
) -> list | None:
    """
    Run hand detection on one RGB frame.

    Returns a list of 21 (x_px, y_px) tuples for the detected hand,
    or None if no hand is found.
    """
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    result   = detector.detect_for_video(mp_image, timestamp_ms)

    if not result.hand_landmarks:
        return None

    h, w = frame_rgb.shape[:2]
    return [
        (int(lm.x * w), int(lm.y * h))
        for lm in result.hand_landmarks[0]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Geometry & mapping
# ─────────────────────────────────────────────────────────────────────────────

def euclidean(p1: tuple, p2: tuple) -> float:
    """Euclidean distance between two (x, y) pixel points."""
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def dist_to_volume(
    dist_px: float,
    min_dist: float = MIN_DIST_PX,
    max_dist: float = MAX_DIST_PX,
) -> float:
    """
    Map finger-gap (pixels) to a volume scalar in [0.0, 1.0].

      dist ≤ min_dist  →  0.0   (pinched  → silent)
      dist ≥ max_dist  →  1.0   (spread   → full volume)
      in-between       →  linear interpolation, clamped
    """
    if max_dist <= min_dist:
        return 0.0
    return max(0.0, min(1.0, (dist_px - min_dist) / (max_dist - min_dist)))


# ─────────────────────────────────────────────────────────────────────────────
# UI drawing
# ─────────────────────────────────────────────────────────────────────────────

def draw_hand_skeleton(frame: np.ndarray, landmarks_px: list) -> None:
    """Draw the full 21-point hand skeleton from pixel-coordinate landmarks."""
    # Connections
    for start, end in HAND_CONNECTIONS:
        cv2.line(
            frame, landmarks_px[start], landmarks_px[end],
            C_BONE, 2, cv2.LINE_AA,
        )
    # Joint dots
    for pt in landmarks_px:
        cv2.circle(frame, pt, 5, C_JOINT, cv2.FILLED)
        cv2.circle(frame, pt, 5, C_BONE,  1, cv2.LINE_AA)


def draw_fingertip_overlay(
    frame: np.ndarray,
    thumb_pt: tuple,
    index_pt: tuple,
) -> None:
    """Cyan line + magenta circles on the two tracked fingertips."""
    mid_pt = (
        (thumb_pt[0] + index_pt[0]) // 2,
        (thumb_pt[1] + index_pt[1]) // 2,
    )
    cv2.line(frame, thumb_pt, index_pt, C_LINE, 3, cv2.LINE_AA)
    cv2.circle(frame, thumb_pt, 14, C_TIP,  cv2.FILLED)
    cv2.circle(frame, index_pt, 14, C_TIP,  cv2.FILLED)
    cv2.circle(frame, thumb_pt, 14, C_TEXT,  1, cv2.LINE_AA)
    cv2.circle(frame, index_pt, 14, C_TEXT,  1, cv2.LINE_AA)
    cv2.circle(frame, mid_pt,    8, C_MID,  cv2.FILLED)


def draw_volume_bar(
    frame: np.ndarray,
    volume_scalar: float,
    is_muted: bool,
) -> None:
    """Vertical volume bar — green (active) or blue (muted), fills from bottom."""
    x, y, w, h = BAR_X, BAR_Y, BAR_W, BAR_H

    cv2.rectangle(frame, (x, y), (x + w, y + h), C_BAR_BG, cv2.FILLED)

    fill_h    = int(h * volume_scalar)
    bar_color = C_BAR_MUTED if is_muted else C_BAR_ACTIVE
    if fill_h > 0:
        cv2.rectangle(
            frame,
            (x,     y + h - fill_h),
            (x + w, y + h),
            bar_color, cv2.FILLED,
        )

    cv2.rectangle(frame, (x, y), (x + w, y + h), C_DIM, 1)

    cv2.putText(
        frame, f"{int(volume_scalar * 100)}%",
        (x - 2, y + h + 26),
        cv2.FONT_HERSHEY_SIMPLEX, 0.85, C_TEXT, 2, cv2.LINE_AA,
    )
    cv2.putText(
        frame, "MAX", (x + w + 8, y + 14),
        cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_DIM, 1, cv2.LINE_AA,
    )
    cv2.putText(
        frame, "MIN", (x + w + 8, y + h),
        cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_DIM, 1, cv2.LINE_AA,
    )


def draw_mute_ui(
    frame: np.ndarray,
    is_muted: bool,
    mute_progress: float,
) -> None:
    """'MUTED' banner when muted; sweep arc while holding the pinch gesture."""
    fh, fw = frame.shape[:2]
    cx, cy = fw // 2, 65

    if is_muted:
        text = "MUTED"
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.5, 3)
        cv2.rectangle(
            frame,
            ((fw - tw) // 2 - 16, 30),
            ((fw + tw) // 2 + 16, 90),
            (20, 20, 20), cv2.FILLED,
        )
        cv2.putText(
            frame, text, ((fw - tw) // 2, 82),
            cv2.FONT_HERSHEY_SIMPLEX, 1.5, C_MUTE_LABEL, 3, cv2.LINE_AA,
        )
    elif mute_progress > 0.0:
        cv2.ellipse(
            frame, (cx, cy), (28, 28),
            -90, 0, int(360 * mute_progress),
            C_MUTE_WARN, 4, cv2.LINE_AA,
        )
        cv2.putText(
            frame, "Hold to mute",
            (cx - 58, cy + 50),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, C_MUTE_WARN, 1, cv2.LINE_AA,
        )


def draw_hud(
    frame: np.ndarray,
    fps: float,
    dist_px: float,
    hand_detected: bool,
) -> None:
    """Top-right HUD: FPS, detection status, finger gap."""
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
    """Small help legend at the bottom-left corner."""
    fh = frame.shape[0]
    for i, line in enumerate([
        "Q  – quit",
        "Hold pinch  – mute toggle",
        "Pinch closer  – quieter",
        "Spread apart  – louder",
    ]):
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
    detector = init_hand_detector()

    # ── State ────────────────────────────────────────────────────────────────
    smooth_buf       = collections.deque(maxlen=SMOOTH_WINDOW)
    mute_hold_count  = 0
    mute_released    = True
    is_muted         = False
    last_sent_vol    = -1.0
    dist_px          = 0.0

    try:
        current_vol = get_system_volume()
    except Exception:
        current_vol = 0.5
    smooth_buf.extend([current_vol] * SMOOTH_WINDOW)

    fps_buf      = collections.deque(maxlen=30)
    prev_time    = time.perf_counter()
    start_time   = time.perf_counter()  # reference for monotonic timestamps

    print("[INFO] Running — press Q in the video window to quit.\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[ERROR] Failed to read camera frame — exiting.")
            break

        frame = cv2.flip(frame, 1)  # mirror so it feels natural

        # ── FPS ──────────────────────────────────────────────────────────────
        now = time.perf_counter()
        fps_buf.append(1.0 / max(now - prev_time, 1e-9))
        prev_time = now
        fps = sum(fps_buf) / len(fps_buf)

        # ── Hand detection ───────────────────────────────────────────────────
        frame_rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        timestamp_ms = int((now - start_time) * 1000)
        landmarks    = process_frame(frame_rgb, detector, timestamp_ms)
        hand_detected = landmarks is not None

        mute_progress = 0.0

        if hand_detected:
            thumb_pt = landmarks[THUMB_TIP]
            index_pt = landmarks[INDEX_TIP]
            dist_px  = euclidean(thumb_pt, index_pt)
            raw_vol  = dist_to_volume(dist_px)

            # ── Smoothing ────────────────────────────────────────────────────
            smooth_buf.append(raw_vol)
            smoothed_vol = sum(smooth_buf) / len(smooth_buf)

            # ── Mute gesture ─────────────────────────────────────────────────
            if dist_px < MUTE_THRESHOLD_PX:
                if mute_released:
                    mute_hold_count += 1
                    if mute_hold_count >= MUTE_HOLD_FRAMES:
                        is_muted = not is_muted
                        set_system_mute(is_muted)
                        mute_hold_count = 0
                        mute_released   = False
            else:
                mute_hold_count = 0
                mute_released   = True

            mute_progress = min(mute_hold_count / MUTE_HOLD_FRAMES, 1.0)

            # ── Volume update ────────────────────────────────────────────────
            if not is_muted:
                if abs(smoothed_vol - last_sent_vol) >= VOL_UPDATE_EPSILON:
                    set_system_volume(smoothed_vol)
                    last_sent_vol = smoothed_vol

            current_vol = smoothed_vol

            # ── Draw hand ────────────────────────────────────────────────────
            draw_hand_skeleton(frame, landmarks)
            draw_fingertip_overlay(frame, thumb_pt, index_pt)

        # ── Always-on UI ─────────────────────────────────────────────────────
        draw_volume_bar(frame, current_vol, is_muted)
        draw_mute_ui(frame, is_muted, mute_progress if hand_detected else 0.0)
        draw_hud(frame, fps, dist_px, hand_detected)
        draw_instructions(frame)

        cv2.imshow("Hand Volume Control  |  press Q to quit", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q") or cv2.getWindowProperty(
            "Hand Volume Control  |  press Q to quit",
            cv2.WND_PROP_VISIBLE,
        ) < 1:
            break

    cap.release()
    detector.close()
    cv2.destroyAllWindows()
    print("[INFO] Exited cleanly.")


if __name__ == "__main__":
    main()

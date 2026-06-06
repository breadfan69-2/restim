"""
stereostim_player.py — Calibration player for raw stereostim audio files.

Controls:
  - Master gain       [0–200%]
  - L gain            [0–200%]
  - R gain            [0–200%]
  - Center reduction  [0–100%]  (attenuates bilateral moments to protect N electrode)
  - Envelope tau      [5–200ms] (IIR envelope follower time constant)

Profiles persisted to stereostim_profiles.json in the same directory.

Usage:
    python stereostim_player.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from net.media_source.heresphere import HereSphere
from net.media_source.interface import MediaSourceInterface
from net.media_source.kodi import Kodi
from net.media_source.mpc import MPC
from net.media_source.vlc import VLC
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg
from qt_ui import settings as app_settings

pg.setConfigOptions(antialias=False, useOpenGL=False)


# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

RESOURCE_BASE = Path(
    getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
)
APP_ICON_FILE = RESOURCE_BASE / "resources" / "bREadbeats.ico"
PROFILE_FILE = Path(__file__).parent / "stereostim_profiles.json"
AUTO_MATCH_AUDIO_EXTENSIONS = (".mp3",)
SYNC_DRIFT_THRESHOLD_S = 0.150
SYNC_SEEK_COOLDOWN_S = 0.250

DEFAULT_PROFILE: dict = {
    "master": 1.0,
    "l_gain": 1.0,
    "r_gain": 1.0,
    "center_reduction": 0.0,
    "env_tau_ms": 30.0,
}


# ---------------------------------------------------------------------------
# Profile I/O
# ---------------------------------------------------------------------------

def load_profiles() -> dict:
    if PROFILE_FILE.exists():
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return {"Default": dict(DEFAULT_PROFILE)}


def save_profiles(profiles: dict) -> None:
    tmp = PROFILE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    tmp.replace(PROFILE_FILE)


def _normalized_search_paths(media_path: str) -> list[str]:
    dirname = os.path.dirname(media_path)
    extra_paths = app_settings.additional_search_paths.get()
    seen: set[str] = set()
    search_paths: list[str] = []
    for raw_path in [dirname] + extra_paths:
        norm = os.path.normcase(os.path.normpath(raw_path.rstrip("/*")))
        suffix = "/*" if raw_path.endswith("/*") else ""
        key = norm + suffix
        if key in seen:
            continue
        seen.add(key)
        search_paths.append(raw_path)
    return search_paths


def _find_matching_audio_in_root(start_dir: str, media_stem: str) -> Optional[Path]:
    pending = [start_dir]
    media_stem = media_stem.lower()

    while pending:
        raw_dir = os.path.expanduser(pending.pop(0))
        recursive = raw_dir.endswith("/*")
        current_dir = raw_dir[:-2] if recursive else raw_dir
        base_path = Path(current_dir)
        if not base_path.exists() or not base_path.is_dir():
            continue

        matches: list[Path] = []
        fallback_dirs: list[str] = []
        recursive_dirs: list[str] = []
        try:
            for node in base_path.iterdir():
                if node.is_dir():
                    if node.name.lower() == media_stem:
                        fallback_dirs.append(str(node))
                    elif recursive:
                        recursive_dirs.append(str(node) + "/*")
                    continue

                if node.suffix.lower() in AUTO_MATCH_AUDIO_EXTENSIONS:
                    if node.stem.lower() == media_stem:
                        matches.append(node)
        except OSError:
            continue

        if matches:
            matches.sort(key=lambda path: (path.suffix.lower(), str(path).lower()))
            return matches[0]

        pending = recursive_dirs + fallback_dirs + pending

    return None


def find_matching_audio_file(media_path: str) -> Optional[Path]:
    media_stem = Path(media_path).stem
    for search_path in _normalized_search_paths(media_path):
        match = _find_matching_audio_in_root(search_path, media_stem)
        if match is not None:
            return match
    return None


# ---------------------------------------------------------------------------
# SharedState — audio callback <-> UI bridge
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._raw_lock = threading.Lock()
        # metrics (written by callback, read by UI)
        self.env_L: float = 0.0
        self.env_R: float = 0.0
        self.balance: float = 0.0
        self.position_samples: int = 0
        self.total_samples: int = 0
        self.is_playing: bool = False
        self.playback_finished: bool = False
        # calibration (written by UI, read by callback)
        self.master: float = 1.0
        self.l_gain: float = 1.0
        self.r_gain: float = 1.0
        self.center_reduction: float = 0.0
        self.env_tau_ms: float = 30.0
        # seek request (written by UI, consumed by feeder)
        self.seek_to: Optional[int] = None
        # pause flag (written by UI, read by callback)
        self.paused: bool = False
        # envelope history for Lissajous (written by callback, read by UI at ~30 Hz)
        self._waveform: deque = deque(maxlen=600)
        # raw audio chunks for waveform display (~1 s of output samples)
        self._raw_chunks_L: deque = deque()  # deque of np.ndarray
        self._raw_chunks_R: deque = deque()
        self._raw_max_samples: int = 44100  # 1 second

    def write_metrics(
        self,
        env_L: float,
        env_R: float,
        balance: float,
        position_samples: int,
        env_pair: tuple,
    ) -> None:
        with self._lock:
            self.env_L = env_L
            self.env_R = env_R
            self.balance = balance
            self.position_samples = position_samples
            self._waveform.append(env_pair)

    def read_metrics(self):
        with self._lock:
            return (
                self.env_L,
                self.env_R,
                self.balance,
                self.position_samples,
                list(self._waveform),
            )

    def write_calibration(
        self,
        master: float,
        l_gain: float,
        r_gain: float,
        center_reduction: float,
        env_tau_ms: float,
    ) -> None:
        with self._lock:
            self.master = master
            self.l_gain = l_gain
            self.r_gain = r_gain
            self.center_reduction = center_reduction
            self.env_tau_ms = env_tau_ms

    def read_calibration(self):
        with self._lock:
            return (
                self.master,
                self.l_gain,
                self.r_gain,
                self.center_reduction,
                self.env_tau_ms,
            )

    def request_seek(self, sample: int) -> None:
        with self._lock:
            self.seek_to = sample

    def clear_seek(self) -> None:
        with self._lock:
            self.seek_to = None

    def consume_seek(self) -> Optional[int]:
        with self._lock:
            s = self.seek_to
            self.seek_to = None
            return s

    def set_position(self, sample: int) -> None:
        with self._lock:
            self.position_samples = sample

    def clear_playback_finished(self) -> None:
        with self._lock:
            self.playback_finished = False

    def mark_playback_finished(self) -> None:
        with self._lock:
            self.is_playing = False
            self.paused = False
            self.playback_finished = True

    def consume_playback_finished(self) -> bool:
        with self._lock:
            finished = self.playback_finished
            self.playback_finished = False
            return finished

    def push_raw(self, out_L: np.ndarray, out_R: np.ndarray) -> None:
        """Store processed output samples for waveform display."""
        with self._raw_lock:
            self._raw_chunks_L.append(out_L.copy())
            self._raw_chunks_R.append(out_R.copy())
            total = sum(len(c) for c in self._raw_chunks_L)
            while total > self._raw_max_samples and self._raw_chunks_L:
                removed = self._raw_chunks_L.popleft()
                self._raw_chunks_R.popleft()
                total -= len(removed)

    def read_raw(self):
        """Return snapshot of recent processed samples for both channels."""
        with self._raw_lock:
            if not self._raw_chunks_L:
                return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
            return (
                np.concatenate(list(self._raw_chunks_L)),
                np.concatenate(list(self._raw_chunks_R)),
            )


# ---------------------------------------------------------------------------
# PlaybackEngine
# ---------------------------------------------------------------------------

_FEEDER_CHUNK = 4096
_FEEDER_MAXBUF = 50


class PlaybackEngine:
    def __init__(self, shared: SharedState) -> None:
        self._shared = shared
        self._file_path: Optional[str] = None
        self._sf: Optional[sf.SoundFile] = None
        self._stream: Optional[sd.OutputStream] = None
        self._feeder_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._buf: deque[tuple[np.ndarray, float]] = deque()
        self._buf_lock = threading.Lock()
        self.sample_rate: int = 44100
        self.file_sample_rate: int = 44100
        self._env_L: float = 0.0
        self._env_R: float = 0.0
        self._position: int = 0
        self._source_per_output_frame: float = 1.0
        self._resample_buffer = np.zeros((0, 2), dtype=np.float32)
        self._resample_buffer_start: int = 0
        self._next_output_source_pos: float = 0.0
        self._reached_eof = False

    def open_file(self, path: str) -> None:
        """Validate file. Raises ValueError if not stereo."""
        try:
            with sf.SoundFile(path) as f:
                channels = f.channels
                frames = f.frames
                samplerate = f.samplerate
        except Exception as exc:
            raise ValueError(f"Could not open audio file:\n{exc}") from exc
        if channels != 2:
            raise ValueError(
                f"File must be stereo (2 channels), got {channels} channel(s)."
            )
        self._file_path = path
        self.file_sample_rate = int(samplerate)
        self._shared.total_samples = frames
        self._shared.set_position(0)
        self._shared.is_playing = False
        with self._shared._lock:
            self._shared.paused = False
        self._shared.clear_seek()
        self._shared.clear_playback_finished()

    def start(self, host_api_name: str, device_name: str) -> None:
        self.stop()
        self._stop_event.clear()
        self._buf.clear()
        self._env_L = 0.0
        self._env_R = 0.0
        self._reached_eof = False
        self._shared.clear_playback_finished()
        start_sample = self._shared.consume_seek()
        if start_sample is None:
            start_sample = self._shared.position_samples
        start_sample = max(0, min(int(start_sample), self._shared.total_samples))
        self._position = start_sample

        # locate device
        device_index = -1
        for dev in sd.query_devices():
            if sd.query_hostapis(dev["hostapi"])["name"] == host_api_name:
                if dev["name"] == device_name:
                    device_index = dev["index"]
                    break
        if device_index == -1:
            raise RuntimeError(
                f"Audio device not found:\n{host_api_name} / {device_name}"
            )

        device_info = sd.query_devices(device_index)
        self.sample_rate = int(device_info["default_samplerate"])
        self._source_per_output_frame = (
            self.file_sample_rate / self.sample_rate
            if self.sample_rate > 0
            else 1.0
        )
        self._reset_resampler(start_sample)

        self._sf = sf.SoundFile(self._file_path)
        self._sf.seek(start_sample)
        self._shared.set_position(start_sample)

        self._feeder_thread = threading.Thread(target=self._feeder, daemon=True)
        self._feeder_thread.start()

        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            device=device_index,
            channels=2,
            dtype=np.float32,
            callback=self._callback,
            latency="low",
        )
        self._stream.start()
        self._shared.is_playing = True

    def pause(self) -> None:
        with self._shared._lock:
            self._shared.paused = True

    def resume(self) -> None:
        with self._shared._lock:
            self._shared.paused = False

    def stop(self) -> None:
        self._shared.is_playing = False
        with self._shared._lock:
            self._shared.paused = False
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._feeder_thread is not None:
            self._feeder_thread.join(timeout=2.0)
            self._feeder_thread = None
        if self._sf is not None:
            try:
                self._sf.close()
            except Exception:
                pass
            self._sf = None
        with self._buf_lock:
            self._buf.clear()
        self._reached_eof = False
        self._reset_resampler(0)
        self._shared.clear_playback_finished()

    def _reset_resampler(self, source_start: int) -> None:
        self._resample_buffer = np.zeros((0, 2), dtype=np.float32)
        self._resample_buffer_start = source_start
        self._next_output_source_pos = float(source_start)

    def _resample_source_chunk(
        self, chunk: np.ndarray, flush: bool = False
    ) -> Optional[tuple[np.ndarray, float]]:
        if len(chunk) > 0:
            if len(self._resample_buffer) == 0:
                self._resample_buffer = chunk.copy()
            else:
                self._resample_buffer = np.concatenate(
                    (self._resample_buffer, chunk), axis=0
                )

        if len(self._resample_buffer) == 0:
            return None

        if flush:
            samples = np.concatenate(
                (self._resample_buffer, self._resample_buffer[-1:]), axis=0
            )
            source_limit = self._resample_buffer_start + len(self._resample_buffer)
        else:
            if len(self._resample_buffer) < 2:
                return None
            samples = self._resample_buffer
            source_limit = (
                self._resample_buffer_start + len(self._resample_buffer) - 1
            )

        if self._next_output_source_pos >= source_limit:
            if flush:
                self._resample_buffer = np.zeros((0, 2), dtype=np.float32)
            return None

        step = self._source_per_output_frame
        count = int(
            math.floor((source_limit - self._next_output_source_pos - 1e-9) / step)
        ) + 1
        if count <= 0:
            return None

        positions = self._next_output_source_pos + (
            np.arange(count, dtype=np.float64) * step
        )
        local_positions = positions - self._resample_buffer_start
        left_index = np.floor(local_positions).astype(np.int64)
        frac = (local_positions - left_index).astype(np.float32)[:, None]

        left = samples[left_index]
        right = samples[left_index + 1]
        out = left + (right - left) * frac

        self._next_output_source_pos = float(positions[-1] + step)
        if flush:
            self._resample_buffer = np.zeros((0, 2), dtype=np.float32)
        else:
            drop = max(
                int(math.floor(self._next_output_source_pos))
                - self._resample_buffer_start,
                0,
            )
            if drop > 0:
                self._resample_buffer = self._resample_buffer[drop:]
                self._resample_buffer_start += drop

        return out.astype(np.float32, copy=False), float(positions[0])

    def _feeder(self) -> None:
        while not self._stop_event.is_set():
            # handle seek
            seek_to = self._shared.consume_seek()
            if seek_to is not None:
                with self._buf_lock:
                    self._buf.clear()
                try:
                    self._sf.seek(seek_to)
                    self._position = seek_to
                    self._reached_eof = False
                    self._reset_resampler(seek_to)
                except Exception:
                    pass

            # fill buffer
            with self._buf_lock:
                buf_len = len(self._buf)

            if buf_len < _FEEDER_MAXBUF:
                chunk = self._sf.read(
                    _FEEDER_CHUNK, dtype=np.float32, always_2d=True
                )
                if len(chunk) == 0:
                    tail = self._resample_source_chunk(chunk, flush=True)
                    if tail is not None:
                        with self._buf_lock:
                            self._buf.append(tail)
                    self._reached_eof = True
                    break
                resampled = self._resample_source_chunk(chunk)
                if resampled is not None:
                    with self._buf_lock:
                        self._buf.append(resampled)
            else:
                time.sleep(0.005)

    def _callback(
        self, outdata: np.ndarray, frames: int, time_info, status
    ) -> None:
        # check pause first — don't consume buffer while paused
        with self._shared._lock:
            paused = self._shared.paused
        if paused:
            outdata[:] = 0
            return

        # drain buffer
        needed = frames
        collected: list[np.ndarray] = []
        position_after = float(self._position)
        with self._buf_lock:
            while needed > 0 and self._buf:
                chunk, source_start = self._buf.popleft()
                take = min(len(chunk), needed)
                if take > 0:
                    collected.append(chunk[:take])
                    position_after = source_start + (
                        take * self._source_per_output_frame
                    )
                    needed -= take
                if take < len(chunk):
                    remaining = chunk[take:]
                    remaining_start = source_start + (
                        take * self._source_per_output_frame
                    )
                    self._buf.appendleft((remaining, remaining_start))
            reached_end = self._reached_eof and not self._buf

        if collected:
            block = np.concatenate(collected, axis=0)
            self._position = min(int(position_after), self._shared.total_samples)
        else:
            block = np.zeros((frames, 2), dtype=np.float32)
            if reached_end:
                self._position = self._shared.total_samples

        if len(block) < frames:
            block = np.pad(block, ((0, frames - len(block)), (0, 0)))

        L = block[:, 0]
        R = block[:, 1]

        # read calibration under lock (fast)
        with self._shared._lock:
            master = self._shared.master
            l_gain = self._shared.l_gain
            r_gain = self._shared.r_gain
            center_reduction = self._shared.center_reduction
            tau_ms = self._shared.env_tau_ms

        # IIR envelope follower (block-level)
        tau_s = max(tau_ms, 1.0) / 1000.0
        alpha = 1.0 - math.exp(-frames / (self.sample_rate * tau_s))
        self._env_L += alpha * (float(np.mean(np.abs(L))) - self._env_L)
        self._env_R += alpha * (float(np.mean(np.abs(R))) - self._env_R)

        balance = (
            2.0
            * min(self._env_L, self._env_R)
            / (self._env_L + self._env_R + 1e-9)
        )

        # center reduction (attenuates bilaterally-equal moments)
        center_gain = 1.0 - center_reduction * balance

        # apply gains + clip
        out_L = np.clip(L * l_gain * center_gain * master, -1.0, 1.0)
        out_R = np.clip(R * r_gain * center_gain * master, -1.0, 1.0)

        outdata[:, 0] = out_L
        outdata[:, 1] = out_R

        self._shared.push_raw(out_L, out_R)
        self._shared.write_metrics(
            self._env_L,
            self._env_R,
            balance,
            self._position,
            (self._env_L, self._env_R),
        )
        if reached_end:
            self._shared.mark_playback_finished()


# ---------------------------------------------------------------------------
# Lissajous canvas
# ---------------------------------------------------------------------------

class LissajousWidget(QWidget):
    """
    X-axis = env_R, Y-axis = env_L.
    Top-right corner = maximum N load.
    Fading trail of recent envelope samples.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(200, 200)
        self._points: deque = deque(maxlen=200)

    def push(self, env_R: float, env_L: float) -> None:
        self._points.append((min(env_R, 1.0), min(env_L, 1.0)))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        painter.fillRect(0, 0, w, h, QColor("#0a0a12"))

        # reference diagonal: perfect bilateral balance
        painter.setPen(QPen(QColor("#2a2a3a"), 1))
        painter.drawLine(0, h - 1, w - 1, 0)

        pts = list(self._points)
        n = len(pts)
        for i, (rx, ly) in enumerate(pts):
            age = (n - 1 - i) / max(n - 1, 1)  # 0=newest … 1=oldest
            brightness = max(0.0, 1.0 - age ** 0.6)
            alpha_val = int(255 * brightness)
            color = QColor(0, 229, 229, alpha_val)
            painter.setPen(QPen(color, 2))
            px = 2 + int(rx * (w - 4))
            py = (h - 3) - int(ly * (h - 4))
            painter.drawPoint(px, py)

        painter.end()


# ---------------------------------------------------------------------------
# N-load bar
# ---------------------------------------------------------------------------

class NLoadMeter(QWidget):
    """Horizontal bar showing bilateral balance (0 = unilateral, 1 = equal)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(200, 22)
        self._value: float = 0.0

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        w, h = self.width(), self.height()

        painter.fillRect(0, 0, w, h, QColor("#0a0a12"))

        fill_w = int(self._value * (w - 2))
        if fill_w > 0:
            v = self._value
            if v < 0.5:
                r = int(v * 2 * 220)
                g = 200
            else:
                r = 220
                g = int((1.0 - (v - 0.5) * 2) * 200)
            painter.fillRect(1, 1, fill_w, h - 2, QColor(r, g, 30))

        painter.setPen(QPen(QColor("#3d3d3d"), 1))
        painter.drawRect(0, 0, w - 1, h - 1)
        painter.end()


# ---------------------------------------------------------------------------
# Stylesheet (breadbeats dark theme + slider / groupbox extensions)
# ---------------------------------------------------------------------------

def _stylesheet() -> str:
    return """
        QMainWindow, QWidget {
            background-color: #3d3d3d;
            color: #e0e0e0;
        }
        QFrame {
            background-color: #3d3d3d;
            color: #e0e0e0;
        }
        QPushButton {
            background-color: #565d7f;
            color: #ffffff;
            border: none;
            border-radius: 4px;
            padding: 5px 15px;
        }
        QPushButton:hover { background-color: #6d6d8f; }
        QPushButton:pressed { background-color: #4a4d6f; }
        QPushButton:checked { background-color: #008b8b; color: #ffffff; }
        QPushButton:checked:hover { background-color: #109b9b; }
        QPushButton:checked:pressed { background-color: #006f6f; }
        QPushButton:disabled { background-color: #424242; color: #757575; }

        QLabel { color: #e0e0e0; }

        QGroupBox {
            color: #e0e0e0;
            border: 1px solid #565d7f;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 6px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 4px;
            color: #aaaaff;
        }

        QComboBox {
            background-color: #4d4d4d;
            color: #e0e0e0;
            border: 1px solid #5d5d5d;
            border-radius: 4px;
            padding: 3px 8px;
        }
        QSpinBox {
            background-color: #4d4d4d;
            color: #e0e0e0;
            border: 1px solid #5d5d5d;
            border-radius: 4px;
            padding: 3px 8px;
        }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView {
            background-color: #4d4d4d;
            color: #e0e0e0;
            selection-background-color: #008b8b;
        }

        QSlider::groove:horizontal {
            background: #4d4d4d;
            height: 6px;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #565d7f;
            width: 14px;
            height: 14px;
            margin: -4px 0;
            border-radius: 7px;
        }
        QSlider::handle:horizontal:hover { background: #6d6d8f; }
        QSlider::sub-page:horizontal {
            background: #008b8b;
            border-radius: 3px;
        }

        QScrollBar:horizontal {
            background: #3d3d3d;
            height: 8px;
        }
        QScrollBar::handle:horizontal {
            background: #565d7f;
            min-width: 20px;
            border-radius: 4px;
        }
        QScrollBar:vertical {
            background: #3d3d3d;
            width: 8px;
        }
        QScrollBar::handle:vertical {
            background: #565d7f;
            min-height: 20px;
            border-radius: 4px;
        }
    """


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class StereoStimPlayer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("StereoStim Player")
        self.setMinimumSize(780, 600)

        self._shared = SharedState()
        self._engine = PlaybackEngine(self._shared)
        self._profiles = load_profiles()
        self._scrubber_dragging = False
        self._device_map: dict[str, list[str]] = {}
        self._loaded_audio_path: Optional[Path] = None
        self._matched_audio_path: Optional[Path] = None
        self._last_media_path = ""
        self._sync_status_message = ""
        self._last_sync_seek_at = 0.0
        self._sync_sources: dict[str, MediaSourceInterface] = {
            "MPC-HC": MPC(self),
            "HereSphere": HereSphere(self),
            "VLC": VLC(self),
            "Kodi": Kodi(self),
        }
        for source in self._sync_sources.values():
            source.connectionStatusChanged.connect(
                self._on_media_source_status_changed
            )

        self._build_ui()
        self._populate_devices()
        self._populate_sync_sources()

        # apply first profile without triggering profile_selected signal
        first = list(self._profiles.keys())[0]
        self._profile_combo.blockSignals(True)
        self._profile_combo.setCurrentText(first)
        self._profile_combo.blockSignals(False)
        self._apply_profile(first)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        root.addLayout(self._build_top_bar())
        root.addLayout(self._build_sync_bar())
        root.addLayout(self._build_main_area(), stretch=1)
        root.addWidget(self._build_waveform_group())
        root.addLayout(self._build_transport())

    def _build_top_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self._open_btn = QPushButton("Open File…")
        self._open_btn.clicked.connect(self._on_open)
        row.addWidget(self._open_btn)

        self._file_label = QLabel("No file loaded")
        self._file_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._file_label)

        row.addStretch()

        row.addWidget(QLabel("Host API:"))
        self._host_combo = QComboBox()
        self._host_combo.setMinimumWidth(130)
        self._host_combo.currentTextChanged.connect(self._on_host_changed)
        row.addWidget(self._host_combo)

        row.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(200)
        row.addWidget(self._device_combo)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(32)
        refresh_btn.clicked.connect(self._populate_devices)
        row.addWidget(refresh_btn)

        return row

    def _build_sync_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()

        row.addWidget(QLabel("Media Sync:"))
        self._sync_source_combo = QComboBox()
        self._sync_source_combo.setMinimumWidth(120)
        self._sync_source_combo.currentTextChanged.connect(
            self._on_sync_source_changed
        )
        row.addWidget(self._sync_source_combo)

        self._sync_status_label = QLabel("Disabled")
        self._sync_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._sync_status_label, stretch=1)

        row.addWidget(QLabel("Offset:"))
        self._sync_offset_spin = QSpinBox()
        self._sync_offset_spin.setRange(-10000, 10000)
        self._sync_offset_spin.setSingleStep(10)
        self._sync_offset_spin.setSuffix(" ms")
        self._sync_offset_spin.setValue(
            app_settings.media_sync_time_offset_ms.get()
        )
        self._sync_offset_spin.valueChanged.connect(self._on_sync_offset_changed)
        row.addWidget(self._sync_offset_spin)

        return row

    def _build_main_area(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        # Left panel: Lissajous + N-load meter
        liss_group = QGroupBox("Envelope Lissajous")
        liss_layout = QVBoxLayout(liss_group)
        liss_layout.setContentsMargins(6, 14, 6, 6)
        liss_layout.setSpacing(4)

        self._lissajous = LissajousWidget()
        liss_layout.addWidget(
            self._lissajous, alignment=Qt.AlignmentFlag.AlignHCenter
        )

        n_label = QLabel("N-Load (bilateral balance)")
        n_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        liss_layout.addWidget(n_label)

        self._nload = NLoadMeter()
        liss_layout.addWidget(
            self._nload, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        liss_layout.addStretch()

        row.addWidget(liss_group)

        # Right panel: calibration sliders + profiles
        row.addWidget(self._build_calibration_group(), stretch=1)

        return row

    def _build_calibration_group(self) -> QGroupBox:
        group = QGroupBox("Calibration")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 16, 10, 10)

        self._master_slider, self._master_val = self._add_slider_row(
            layout, "Master", 0, 200, 100, "%"
        )
        self._lgain_slider, self._lgain_val = self._add_slider_row(
            layout, "L Gain", 0, 200, 100, "%"
        )
        self._rgain_slider, self._rgain_val = self._add_slider_row(
            layout, "R Gain", 0, 200, 100, "%"
        )
        self._center_slider, self._center_val = self._add_slider_row(
            layout, "Center Reduc.", 0, 100, 0, "%"
        )
        self._tau_slider, self._tau_val = self._add_slider_row(
            layout, "Env Tau", 5, 200, 30, "ms"
        )

        for s in (
            self._master_slider,
            self._lgain_slider,
            self._rgain_slider,
            self._center_slider,
            self._tau_slider,
        ):
            s.valueChanged.connect(self._on_calibration_changed)

        layout.addStretch()

        # Profile row
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(140)
        for name in self._profiles:
            self._profile_combo.addItem(name)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        profile_row.addWidget(self._profile_combo)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_profile_save)
        profile_row.addWidget(save_btn)

        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self._on_profile_new)
        profile_row.addWidget(new_btn)

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._on_profile_delete)
        profile_row.addWidget(del_btn)

        layout.addLayout(profile_row)
        return group

    def _add_slider_row(
        self,
        parent_layout: QVBoxLayout,
        label: str,
        min_v: int,
        max_v: int,
        default: int,
        unit: str,
    ):
        row = QHBoxLayout()

        lbl = QLabel(f"{label}:")
        lbl.setFixedWidth(108)
        row.addWidget(lbl)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(min_v, max_v)
        slider.setValue(default)
        row.addWidget(slider)

        val_label = QLabel(f"{default} {unit}")
        val_label.setFixedWidth(66)
        val_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(val_label)

        parent_layout.addLayout(row)
        return slider, val_label

    def _build_waveform_group(self) -> QGroupBox:
        group = QGroupBox("Envelope Waveform")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(4, 14, 4, 4)
        layout.setSpacing(2)

        self._waveform_L = pg.PlotWidget(background="#0a0a12")
        self._waveform_R = pg.PlotWidget(background="#0a0a12")

        for pw, name in ((self._waveform_L, "L"), (self._waveform_R, "R")):
            pw.setFixedHeight(60)
            pw.setMouseEnabled(x=False, y=False)
            pw.setMenuEnabled(False)
            pw.showAxis("bottom", False)
            pw.setYRange(-1.0, 1.0)
            pw.getAxis("left").setTextPen(pg.mkPen("#888888"))
            pw.getAxis("left").setTickPen(pg.mkPen("#666666"))
            pw.getAxis("left").setLabel(name, color="#888888")
            pw.getAxis("left").setWidth(28)

        self._wave_curve_L = self._waveform_L.plot(
            pen=pg.mkPen("#008b8b", width=1)
        )
        self._wave_curve_R = self._waveform_R.plot(
            pen=pg.mkPen("#565d7f", width=1)
        )
        for curve in (self._wave_curve_L, self._wave_curve_R):
            curve.setDownsampling(ds=True, auto=True, method='peak')
            curve.setClipToView(True)

        layout.addWidget(self._waveform_L)
        layout.addWidget(self._waveform_R)
        return group

    def _build_transport(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self._play_btn = QPushButton("▶  Play")
        self._play_btn.setFixedWidth(96)
        self._play_btn.setCheckable(True)
        self._play_btn.clicked.connect(self._on_play_pause)
        row.addWidget(self._play_btn)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setFixedWidth(80)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self._stop_btn)

        self._scrubber = QSlider(Qt.Orientation.Horizontal)
        self._scrubber.setRange(0, 1000)
        self._scrubber.setValue(0)
        self._scrubber.sliderPressed.connect(self._on_scrubber_pressed)
        self._scrubber.sliderReleased.connect(self._on_scrubber_released)
        row.addWidget(self._scrubber)

        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setFixedWidth(100)
        self._time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self._time_label)

        return row

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    def _populate_devices(self) -> None:
        self._device_map.clear()
        try:
            for dev in sd.query_devices():
                if dev["max_output_channels"] < 2:
                    continue
                api_name = sd.query_hostapis(dev["hostapi"])["name"]
                self._device_map.setdefault(api_name, []).append(dev["name"])
        except Exception as exc:
            QMessageBox.warning(self, "Device Error", str(exc))
            return

        self._host_combo.blockSignals(True)
        self._host_combo.clear()
        for api in self._device_map:
            self._host_combo.addItem(api)
        self._host_combo.blockSignals(False)
        self._on_host_changed(self._host_combo.currentText())

    def _on_host_changed(self, api: str) -> None:
        self._device_combo.clear()
        for name in self._device_map.get(api, []):
            self._device_combo.addItem(name)

    def _populate_sync_sources(self) -> None:
        self._sync_source_combo.blockSignals(True)
        self._sync_source_combo.clear()
        self._sync_source_combo.addItem("Disabled")
        for name in self._sync_sources:
            self._sync_source_combo.addItem(name)

        default_source = app_settings.media_sync_default_source.get()
        if default_source not in self._sync_sources:
            default_source = "Disabled"
        self._sync_source_combo.setCurrentText(default_source)
        self._sync_source_combo.blockSignals(False)
        self._apply_sync_source(default_source)

    def _current_media_source(self) -> Optional[MediaSourceInterface]:
        return self._sync_sources.get(self._sync_source_combo.currentText())

    def _apply_sync_source(self, name: str) -> None:
        for source_name, source in self._sync_sources.items():
            if source_name != name:
                source.disable()

        self._engine.stop()
        self._sync_stopped_ui()
        self._shared.clear_seek()
        self._last_media_path = ""
        self._matched_audio_path = None
        self._sync_status_message = ""
        self._last_sync_seek_at = 0.0

        if name == "Disabled":
            app_settings.media_sync_default_source.set("Internal")
        else:
            app_settings.media_sync_default_source.set(name)
            self._sync_sources[name].enable()

        self._refresh_sync_status()

    def _on_sync_source_changed(self, name: str) -> None:
        self._apply_sync_source(name)

    def _on_sync_offset_changed(self, value: int) -> None:
        app_settings.media_sync_time_offset_ms.set(int(value))

    def _on_media_source_status_changed(self) -> None:
        if self.sender() is self._current_media_source():
            self._refresh_sync_status()

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _on_calibration_changed(self) -> None:
        master = self._master_slider.value() / 100.0
        l_gain = self._lgain_slider.value() / 100.0
        r_gain = self._rgain_slider.value() / 100.0
        center = self._center_slider.value() / 100.0
        tau_ms = float(self._tau_slider.value())

        self._master_val.setText(f"{self._master_slider.value()} %")
        self._lgain_val.setText(f"{self._lgain_slider.value()} %")
        self._rgain_val.setText(f"{self._rgain_slider.value()} %")
        self._center_val.setText(f"{self._center_slider.value()} %")
        self._tau_val.setText(f"{int(tau_ms)} ms")

        self._shared.write_calibration(master, l_gain, r_gain, center, tau_ms)

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def _on_profile_selected(self, name: str) -> None:
        if name in self._profiles:
            self._apply_profile(name)

    def _apply_profile(self, name: str) -> None:
        p = self._profiles.get(name, DEFAULT_PROFILE)
        self._master_slider.setValue(int(round(p.get("master", 1.0) * 100)))
        self._lgain_slider.setValue(int(round(p.get("l_gain", 1.0) * 100)))
        self._rgain_slider.setValue(int(round(p.get("r_gain", 1.0) * 100)))
        self._center_slider.setValue(int(round(p.get("center_reduction", 0.0) * 100)))
        self._tau_slider.setValue(int(round(p.get("env_tau_ms", 30.0))))
        self._on_calibration_changed()

    def _current_profile_dict(self) -> dict:
        return {
            "master": self._master_slider.value() / 100.0,
            "l_gain": self._lgain_slider.value() / 100.0,
            "r_gain": self._rgain_slider.value() / 100.0,
            "center_reduction": self._center_slider.value() / 100.0,
            "env_tau_ms": float(self._tau_slider.value()),
        }

    def _on_profile_save(self) -> None:
        name = self._profile_combo.currentText()
        if not name:
            return
        self._profiles[name] = self._current_profile_dict()
        save_profiles(self._profiles)

    def _on_profile_new(self) -> None:
        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._profiles[name] = self._current_profile_dict()
        save_profiles(self._profiles)
        self._profile_combo.addItem(name)
        self._profile_combo.setCurrentText(name)

    def _on_profile_delete(self) -> None:
        name = self._profile_combo.currentText()
        if name == "Default":
            QMessageBox.information(
                self, "Delete Profile", "Cannot delete the Default profile."
            )
            return
        if name not in self._profiles:
            return
        del self._profiles[name]
        save_profiles(self._profiles)
        idx = self._profile_combo.currentIndex()
        self._profile_combo.removeItem(idx)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _selected_output_device(self) -> tuple[str, str]:
        return self._host_combo.currentText(), self._device_combo.currentText()

    def _load_audio_file(self, path: str, show_dialog: bool = True) -> bool:
        audio_path = Path(path)
        if self._loaded_audio_path == audio_path:
            return True

        if self._shared.is_playing or self._shared.paused:
            self._engine.stop()
            self._sync_stopped_ui()

        try:
            self._engine.open_file(str(audio_path))
        except ValueError as exc:
            if show_dialog:
                QMessageBox.warning(self, "File Error", str(exc))
            self._sync_status_message = str(exc).splitlines()[0]
            self._refresh_sync_status()
            return False

        self._loaded_audio_path = audio_path
        self._file_label.setText(audio_path.name)
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)
        total = self._shared.total_samples
        self._time_label.setText(
            f"00:00 / {self._fmt_time(total, self._engine.file_sample_rate)}"
        )
        return True

    def _start_playback(self, show_dialog: bool = True) -> bool:
        host, device = self._selected_output_device()
        if not host or not device:
            message = "Select a host API and device."
            if show_dialog:
                QMessageBox.warning(self, "No Device", message)
            self._sync_status_message = message
            self._refresh_sync_status()
            return False
        if self._shared.total_samples == 0:
            message = "Open a file first."
            if show_dialog:
                QMessageBox.warning(self, "No File", message)
            self._sync_status_message = message
            self._refresh_sync_status()
            return False

        try:
            self._engine.start(host, device)
        except Exception as exc:
            if show_dialog:
                QMessageBox.warning(self, "Playback Error", str(exc))
            self._sync_status_message = str(exc).splitlines()[0]
            self._refresh_sync_status()
            return False

        self._play_btn.setText("⏸  Pause")
        self._stop_btn.setEnabled(True)
        self._sync_status_message = ""
        self._refresh_sync_status()
        return True

    def _desired_media_sample(self, source: MediaSourceInterface) -> Optional[int]:
        state = source.state()
        if not state.is_file_loaded() or self._shared.total_samples <= 0:
            return None
        desired_seconds = source.map_timestamp(time.time())
        desired_seconds = max(0.0, desired_seconds)
        return min(
            int(round(desired_seconds * self._engine.file_sample_rate)),
            self._shared.total_samples,
        )

    def _refresh_sync_status(self) -> None:
        source = self._current_media_source()
        if source is None:
            self._sync_status_label.setText("Disabled")
            return

        state = source.state()
        if not state.is_connected():
            status = "Connecting..."
        elif not state.is_file_loaded():
            status = "Connected, no media loaded"
        else:
            status = Path(source.media_path()).name or "Connected"

        if self._sync_status_message:
            status = f"{status} | {self._sync_status_message}"

        self._sync_status_label.setText(status)

    def _sync_media_path(self, media_path: str) -> None:
        self._matched_audio_path = None
        if not media_path:
            self._sync_status_message = ""
            return

        match = find_matching_audio_file(media_path)
        if match is None:
            self._sync_status_message = "No matching .mp3 found"
            if self._shared.is_playing or self._shared.paused:
                self._engine.stop()
                self._sync_stopped_ui()
            return

        self._matched_audio_path = match
        self._sync_status_message = f"Matched {match.name}"
        self._load_audio_file(str(match), show_dialog=False)

    def _sync_to_media(self) -> None:
        source = self._current_media_source()
        if source is None:
            return

        media_path = source.media_path()
        if media_path != self._last_media_path:
            self._last_media_path = media_path
            self._sync_media_path(media_path)
            self._refresh_sync_status()

        if self._matched_audio_path is not None:
            if self._loaded_audio_path != self._matched_audio_path:
                if not self._load_audio_file(
                    str(self._matched_audio_path), show_dialog=False
                ):
                    return

        desired_sample = self._desired_media_sample(source)
        state = source.state()
        if desired_sample is None:
            if self._shared.is_playing and not self._shared.paused:
                self._engine.pause()
                self._play_btn.setText("▶  Resume")
            return

        if not state.is_playing():
            self._shared.request_seek(desired_sample)
            self._shared.set_position(desired_sample)
            if self._shared.is_playing and not self._shared.paused:
                self._engine.pause()
                self._play_btn.setText("▶  Resume")
            return

        if self._matched_audio_path is None:
            return

        if not self._shared.is_playing:
            self._shared.request_seek(desired_sample)
            self._shared.set_position(desired_sample)
            if not self._start_playback(show_dialog=False):
                return
            self._play_btn.setChecked(True)
            return

        if self._shared.paused:
            self._shared.request_seek(desired_sample)
            self._shared.set_position(desired_sample)
            self._engine.resume()
            self._play_btn.setChecked(True)
            self._play_btn.setText("⏸  Pause")
            return

        threshold = max(
            int(self._engine.file_sample_rate * SYNC_DRIFT_THRESHOLD_S),
            1,
        )
        drift = desired_sample - self._shared.position_samples
        now = time.time()
        if (
            abs(drift) > threshold
            and (now - self._last_sync_seek_at) >= SYNC_SEEK_COOLDOWN_S
        ):
            self._shared.request_seek(desired_sample)
            self._shared.set_position(desired_sample)
            self._last_sync_seek_at = now

    def _on_open(self) -> None:
        self._finalize_finished_playback_if_needed()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Stereostim Audio File",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.ogg *.aiff *.m4a);;All Files (*)",
        )
        if not path:
            return

        if not self._load_audio_file(path):
            return

    def _on_play_pause(self, checked: bool) -> None:
        self._finalize_finished_playback_if_needed()
        if checked:
            # If already playing but paused, just resume
            if self._shared.is_playing and self._shared.paused:
                self._engine.resume()
                self._play_btn.setText("⏸  Pause")
                return
            # Fresh start
            if not self._start_playback():
                self._play_btn.setChecked(False)
                return
        else:
            # Button unchecked = pause
            if self._shared.is_playing:
                self._engine.pause()
                self._play_btn.setText("▶  Resume")

    def _on_stop(self) -> None:
        self._engine.stop()
        self._sync_stopped_ui()
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)
        self._shared.set_position(0)
        self._shared.clear_seek()

    def _on_scrubber_pressed(self) -> None:
        self._scrubber_dragging = True

    def _on_scrubber_released(self) -> None:
        self._scrubber_dragging = False
        total = self._shared.total_samples
        if total > 0:
            frac = self._scrubber.value() / 1000.0
            seek_sample = int(frac * total)
            self._shared.request_seek(seek_sample)
            if not self._shared.is_playing:
                self._shared.set_position(seek_sample)
                self._time_label.setText(
                    f"{self._fmt_time(seek_sample, self._engine.file_sample_rate)} / "
                    f"{self._fmt_time(total, self._engine.file_sample_rate)}"
                )

    def _sync_stopped_ui(self) -> None:
        self._play_btn.setChecked(False)
        self._play_btn.setText("▶  Play")
        self._stop_btn.setEnabled(False)

    def _finalize_finished_playback_if_needed(self) -> None:
        if self._shared.consume_playback_finished():
            self._engine.stop()
            self._sync_stopped_ui()

    # ------------------------------------------------------------------
    # UI refresh timer (~30 Hz)
    # ------------------------------------------------------------------

    def _on_timer(self) -> None:
        self._finalize_finished_playback_if_needed()
        self._sync_to_media()
        env_L, env_R, balance, pos, waveform = self._shared.read_metrics()
        total = self._shared.total_samples
        sr = self._engine.file_sample_rate

        # engine stopped at end of file (not paused, not playing)
        if (not self._shared.is_playing
                and not self._shared.paused
                and self._play_btn.isChecked()):
            self._sync_stopped_ui()

        # Lissajous
        self._lissajous.push(env_R, env_L)

        # N-load meter
        self._nload.set_value(balance)

        # waveform strips — raw audio samples centered at zero
        raw_L, raw_R = self._shared.read_raw()
        if raw_L.size > 0:
            self._wave_curve_L.setData(raw_L)
            self._wave_curve_R.setData(raw_R)

        # scrubber + time
        if not self._scrubber_dragging and total > 0:
            self._scrubber.blockSignals(True)
            self._scrubber.setValue(int(pos / total * 1000))
            self._scrubber.blockSignals(False)
            self._time_label.setText(
                f"{self._fmt_time(pos, sr)} / {self._fmt_time(total, sr)}"
            )

    @staticmethod
    def _fmt_time(samples: int, sr: int) -> str:
        if sr <= 0:
            return "00:00"
        t = samples / sr
        m = int(t) // 60
        s = int(t) % 60
        return f"{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        for source in self._sync_sources.values():
            source.disable()
        self._engine.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    if APP_ICON_FILE.exists():
        app_icon = QIcon(str(APP_ICON_FILE))
        app.setWindowIcon(app_icon)
    else:
        app_icon = None
    app.setStyle("Fusion")
    app.setStyleSheet(_stylesheet())
    window = StereoStimPlayer()
    if app_icon is not None:
        window.setWindowIcon(app_icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

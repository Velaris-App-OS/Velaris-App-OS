"""HxMeet P4a-live — streaming ASR backend (auto-detected accelerator).

The operator never picks an engine: at first use the service probes the
machine and loads the best backend for the detected accelerator —
NVIDIA → faster-whisper (CUDA), AMD/Intel GPU → whisper.cpp (Vulkan build
of pywhispercpp), otherwise faster-whisper on CPU int8 (visibly-labelled
lag mode, never fake-realtime). `VELARIS_CASE_MEET_ASR_BACKEND` can pin an
engine for support; `auto` is the default and the normal case.

One process-global model, lazily loaded, serialized behind a lock —
whisper contexts are not thread-safe and a modern GPU transcribes a
rolling caption window far faster than real time, so serialization is
not the bottleneck.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass

import numpy as np

from case_service.config import get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
# whisper.cpp refuses windows under ~1s; pad short utterances with silence.
_MIN_SAMPLES = int(SAMPLE_RATE * 1.1)


@dataclass
class ASRInfo:
    engine: str      # "whisper.cpp" | "faster-whisper"
    device: str      # "vulkan" | "cuda" | "cpu"
    model: str
    realtime: bool   # False = CPU lag mode


def _has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None or any(
        os.path.exists(f"/dev/nvidia{i}") for i in range(2))


def _has_gpu_dri() -> bool:
    """AMD (kfd) or any DRM render node — the Vulkan path covers both AMD and Intel."""
    if os.path.exists("/dev/kfd"):
        return True
    try:
        return any(e.startswith("renderD") for e in os.listdir("/dev/dri"))
    except OSError:
        return False


def detect_backend() -> tuple[str, str]:
    """(engine, device), honouring the config override, else probing."""
    override = get_settings().meet_asr_backend.lower()
    if override == "cuda":
        return "faster-whisper", "cuda"
    if override in ("vulkan", "rocm"):
        return "whisper.cpp", "vulkan"
    if override == "cpu":
        return "faster-whisper", "cpu"
    if _has_nvidia():
        return "faster-whisper", "cuda"
    if _has_gpu_dri():
        return "whisper.cpp", "vulkan"
    return "faster-whisper", "cpu"


class _WhisperCppBackend:
    """pywhispercpp — the portable engine (Vulkan/ROCm/SYCL/Metal builds)."""

    def __init__(self, model: str):
        from pywhispercpp.model import Model
        self._model = Model(model, print_realtime=False, print_progress=False,
                            print_timestamps=False, redirect_whispercpp_logs_to=None)

    def transcribe(self, pcm: np.ndarray) -> str:
        segments = self._model.transcribe(pcm)
        return " ".join(s.text.strip() for s in segments).strip()


class _FasterWhisperBackend:
    """CTranslate2 — fastest on NVIDIA CUDA; int8 on CPU is the lag-mode path."""

    def __init__(self, model: str, device: str):
        from faster_whisper import WhisperModel
        compute = "float16" if device == "cuda" else "int8"
        self._model = WhisperModel(model, device=device, compute_type=compute)

    def transcribe(self, pcm: np.ndarray) -> str:
        segments, _info = self._model.transcribe(pcm, beam_size=1, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()


_backend = None
_info: ASRInfo | None = None
_load_lock = asyncio.Lock()
_transcribe_lock = asyncio.Lock()


def available() -> bool:
    engine, _device = detect_backend()
    try:
        if engine == "whisper.cpp":
            import pywhispercpp  # noqa: F401
        else:
            import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def info() -> ASRInfo | None:
    """The loaded backend's identity (None until first use)."""
    return _info


async def _ensure_loaded():
    global _backend, _info
    if _backend is not None:
        return
    async with _load_lock:
        if _backend is not None:
            return
        engine, device = detect_backend()
        model = get_settings().meet_asr_live_model
        logger.info("hxmeet.asr: loading %s (%s) model=%s", engine, device, model)
        if engine == "whisper.cpp":
            _backend = await asyncio.to_thread(_WhisperCppBackend, model)
        else:
            _backend = await asyncio.to_thread(_FasterWhisperBackend, model, device)
        _info = ASRInfo(engine=engine, device=device, model=model,
                        realtime=device != "cpu")
        logger.info("hxmeet.asr: active backend %s", _info)


async def transcribe_pcm16(pcm16: bytes) -> str:
    """Transcribe raw little-endian int16 mono 16 kHz PCM → text."""
    await _ensure_loaded()
    pcm = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.shape[0] < _MIN_SAMPLES:
        pcm = np.pad(pcm, (0, _MIN_SAMPLES - pcm.shape[0]))
    async with _transcribe_lock:
        return await asyncio.to_thread(_backend.transcribe, pcm)


# ── caption stream state machine (per WebSocket connection) ─────────────────

# Energy gate: int16 RMS below this is treated as silence. Cheap, deterministic
# VAD-lite — enough to stop Whisper hallucinating captions out of room tone.
# Real mics behind AGC/noise-suppression run much quieter than studio audio —
# keep this low; Whisper itself shrugs off borderline noise.
_SILENCE_RMS = 100.0
_PARTIAL_EVERY_S = 1.2      # re-transcribe cadence while speech continues
_FINALIZE_SILENCE_S = 0.8   # trailing silence that closes an utterance
_MAX_UTTERANCE_S = 15.0     # hard cap: finalize and restart the window


class CaptionStream:
    """Accumulates one speaker's PCM and decides when to emit partial/final
    captions. Feed chunks; each feed returns ``None`` or a caption dict."""

    def __init__(self):
        self._buf = bytearray()
        self._had_speech = False
        self._silence_s = 0.0
        self._since_partial_s = 0.0

    @staticmethod
    def _rms(chunk: bytes) -> float:
        a = np.frombuffer(chunk, dtype=np.int16)
        return float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if a.size else 0.0

    async def feed(self, chunk: bytes) -> dict | None:
        dur = len(chunk) / 2 / SAMPLE_RATE
        speech = self._rms(chunk) >= _SILENCE_RMS

        if speech:
            self._had_speech = True
            self._silence_s = 0.0
        elif self._had_speech:
            self._silence_s += dur
        else:
            return None  # leading silence — don't buffer room tone

        self._buf.extend(chunk)
        self._since_partial_s += dur
        buffered_s = len(self._buf) / 2 / SAMPLE_RATE

        if self._had_speech and (
                self._silence_s >= _FINALIZE_SILENCE_S or buffered_s >= _MAX_UTTERANCE_S):
            text = await transcribe_pcm16(bytes(self._buf))
            self._buf.clear()
            self._had_speech = False
            self._silence_s = 0.0
            self._since_partial_s = 0.0
            return {"text": text, "is_final": True} if text else None

        if speech and self._since_partial_s >= _PARTIAL_EVERY_S:
            self._since_partial_s = 0.0
            text = await transcribe_pcm16(bytes(self._buf))
            return {"text": text, "is_final": False} if text else None
        return None

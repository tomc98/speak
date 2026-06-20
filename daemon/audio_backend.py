"""Audio backend abstraction for cross-platform support.

Provides pluggable audio playback implementations:
- MacOSBackend: afplay, afinfo, ffmpeg (current behavior)
- LinuxBackend: paplay, ffprobe (stub for future implementation)
"""

import asyncio
import os
import platform
import re
import shutil
import struct
import subprocess
import tempfile
from abc import ABC, abstractmethod


class AudioBackend(ABC):
    """Abstract interface for audio playback operations."""

    @abstractmethod
    async def play(self, path: str) -> asyncio.subprocess.Process:
        """Spawn async playback process.

        Args:
            path: Path to MP3 file

        Returns:
            Process handle for kill() control
        """

    @abstractmethod
    async def get_duration(self, path: str) -> float | None:
        """Extract audio duration in seconds.

        Args:
            path: Path to audio file

        Returns:
            Duration in seconds, or None on failure
        """

    @abstractmethod
    async def extract_envelope(self, path: str, chunk_ms: int = 50) -> list[float]:
        """Extract RMS envelope for lip-sync animation.

        Args:
            path: Path to audio file
            chunk_ms: Chunk size in milliseconds (default: 50ms)

        Returns:
            List of normalized RMS values (0-1), one per chunk
        """

    @abstractmethod
    async def trim_audio(self, path: str, offset_seconds: float) -> str:
        """Create temporary MP3 starting at offset.

        Used for pause/resume and seek operations.

        Args:
            path: Path to original audio file
            offset_seconds: Start time in seconds

        Returns:
            Path to temporary trimmed MP3 file
        """


class MacOSBackend(AudioBackend):
    """macOS audio backend using afplay, afinfo, and ffmpeg."""

    def __init__(self, ffmpeg_path: str, temp_prefix: str = "claude-tts-"):
        """Initialize macOS backend.

        Args:
            ffmpeg_path: Path to ffmpeg binary
            temp_prefix: Prefix for temporary files
        """
        self.ffmpeg = ffmpeg_path
        self.temp_prefix = temp_prefix

    async def play(self, path: str) -> asyncio.subprocess.Process:
        """Play MP3 using afplay."""
        return await asyncio.create_subprocess_exec(
            "afplay", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def get_duration(self, path: str) -> float | None:
        """Extract duration using afinfo."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["afinfo", path],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"estimated duration:\s*([\d.]+)", result.stdout)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return None

    async def extract_envelope(self, path: str, chunk_ms: int = 50) -> list[float]:
        """Extract RMS envelope using ffmpeg.

        Decodes MP3 to PCM, computes RMS per chunk, normalizes to 0-1.
        """
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [self.ffmpeg, "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000",
                 "-acodec", "pcm_s16le", "-loglevel", "error", "-"],
                capture_output=True, timeout=30,
            )
            raw = result.stdout
        except Exception:
            return []

        if not raw:
            return []

        samples_per_chunk = 16000 * chunk_ms // 1000
        bytes_per_chunk = samples_per_chunk * 2
        envelope = []

        for i in range(0, len(raw) - 1, bytes_per_chunk):
            chunk = raw[i:i + bytes_per_chunk]
            n = len(chunk) // 2
            if n == 0:
                break
            vals = struct.unpack(f'<{n}h', chunk[:n * 2])
            rms = (sum(v * v for v in vals) / n) ** 0.5 / 32768.0
            envelope.append(rms)

        if envelope:
            p95 = sorted(envelope)[int(len(envelope) * 0.95)] or 0.001
            envelope = [round(min(v / p95, 1.0), 3) for v in envelope]

        return envelope

    async def trim_audio(self, path: str, offset_seconds: float) -> str:
        """Trim audio using ffmpeg -ss."""
        fd, tmp = tempfile.mkstemp(prefix=self.temp_prefix, suffix=".mp3")
        os.close(fd)

        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-ss", str(offset_seconds), "-i", path,
            "-acodec", "libmp3lame", "-ab", "128k", "-y", tmp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return tmp


class LinuxBackend(AudioBackend):
    """Linux audio backend (stub implementation).

    Uses paplay for playback. Duration and envelope are stubs
    that return safe defaults, allowing the daemon to start
    without crashing on Linux systems.

    Future implementation should use ffprobe for duration and
    reuse the ffmpeg envelope logic (which works on Linux).
    """

    async def play(self, path: str) -> asyncio.subprocess.Process:
        """Play MP3 using paplay (PulseAudio)."""
        return await asyncio.create_subprocess_exec(
            "paplay", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def get_duration(self, path: str) -> float | None:
        """Stub: returns None (disables duration display).

        Future: Use ffprobe -show_entries format=duration
        """
        return None

    async def extract_envelope(self, path: str, chunk_ms: int = 50) -> list[float]:
        """Stub: returns empty list (disables lip-sync).

        Future: Reuse ffmpeg RMS logic from MacOSBackend
        """
        return []

    async def trim_audio(self, path: str, offset_seconds: float) -> str:
        """Stub: returns original path (pause/resume broken).

        Future: Use ffmpeg -ss (same as macOS)
        """
        return path


def get_audio_backend() -> AudioBackend:
    """Factory function to select audio backend based on OS.

    Returns:
        MacOSBackend on Darwin, LinuxBackend on Linux

    Raises:
        RuntimeError: If OS is not supported
    """
    system = platform.system()

    if system == "Darwin":  # macOS
        ffmpeg = (
            shutil.which("ffmpeg")
            or next(
                (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                 if os.path.exists(p)),
                "ffmpeg"
            )
        )
        return MacOSBackend(ffmpeg)
    elif system == "Linux":
        return LinuxBackend()
    else:
        raise RuntimeError(f"Unsupported OS: {system}")

# /// script
# requires-python = ">=3.12"
# dependencies = ["starlette", "uvicorn"]
# ///
"""ElevenLabs V3 TTS HTTP Daemon for Claude Code.

Standalone Starlette+Uvicorn server replacing the MCP server.
Provides REST API for TTS with audio queuing, multi-voice dialogue,
channel-based queue management, pause/resume, and playback history.

Dashboard at http://127.0.0.1:7865

Endpoints:
  POST /speak              Single voice TTS
  POST /speak/dialogue     Multi-voice dialogue
  GET  /queue              Queue status
  POST /queue/clear        Clear queue
  POST /queue/skip         Skip current
  POST /queue/pause        Pause playback
  POST /queue/resume       Resume playback
  GET  /history            Playback history
  POST /history/replay     Replay from cache
  GET  /voices             Voice configuration
  GET  /events             SSE stream
  GET  /health             Health check
  GET  /                   Dashboard
  GET  /portraits/{name}   Portrait images
"""

import asyncio
import collections
import json
import logging
import os

log = logging.getLogger("voice-daemon")
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

def _is_local_origin(origin: str) -> bool:
    if not origin:
        return True  # No Origin header = non-browser (curl, etc.)
    if origin == "null":
        return False  # Sandboxed iframes send "null" — reject
    origin = origin.rstrip("/")
    for prefix in ("http://127.0.0.1", "http://localhost", "http://[::1]"):
        if origin == prefix or origin.startswith(prefix + ":"):
            return True
    return False


class LocalhostGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            if origin and not _is_local_origin(origin):
                return JSONResponse({"error": "Forbidden origin"}, status_code=403)
        return await call_next(request)

# --- Config ---

REPO_ROOT = Path(__file__).resolve().parent.parent

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_v3"
DEFAULT_FORMAT = "mp3_44100_128"
TEMP_PREFIX = "claude-tts-"
DASHBOARD_DIR = REPO_ROOT / "dashboard"
FFMPEG = (
    shutil.which("ffmpeg")
    or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).exists()), "ffmpeg")
)


def _load_dotenv():
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


_load_dotenv()

DASHBOARD_PORT = int(os.environ.get("SPEAK_PORT", "7865"))
CACHE_DIR = Path(os.environ.get("SPEAK_CACHE_DIR", str(REPO_ROOT / "cache")))


def _load_voices() -> tuple[dict[str, str], dict[str, str]]:
    voices_path = REPO_ROOT / "voices.json"
    roster: dict[str, str] = {}
    by_name: dict[str, str] = {}
    if voices_path.exists():
        try:
            entries = json.loads(voices_path.read_text())
            if not isinstance(entries, list):
                log.warning("voices.json is not a list")
                return roster, by_name
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                vid = entry.get("id")
                if not isinstance(name, str) or not isinstance(vid, str):
                    continue
                roster[vid] = name
                by_name[name.lower()] = vid
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Failed to load voices.json: {e}")
    return roster, by_name


VOICE_ROSTER, VOICE_BY_NAME = _load_voices()

_api_voices_cache: dict[str, str] | None = None


def _fetch_voices_from_api() -> dict[str, str]:
    """Blocking call — must be run via asyncio.to_thread from async context."""
    global _api_voices_cache
    if _api_voices_cache is not None:
        return _api_voices_cache
    _api_voices_cache = {}
    key = _api_key()
    if not key:
        return _api_voices_cache
    try:
        req = Request(f"{API_BASE}/voices", headers={"xi-api-key": key})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for v in data.get("voices", []):
            name = v.get("name")
            vid = v.get("voice_id")
            if isinstance(name, str) and isinstance(vid, str):
                _api_voices_cache[name.lower()] = vid
    except Exception as e:
        log.warning(f"Failed to fetch voices from API: {e}")
    return _api_voices_cache


def resolve_voice(voice: str | None) -> str:
    if not voice:
        return os.environ.get("ELEVENLABS_VOICE_ID", "")
    if voice.lower() in VOICE_BY_NAME:
        return VOICE_BY_NAME[voice.lower()]
    if _api_voices_cache is not None and voice.lower() in _api_voices_cache:
        return _api_voices_cache[voice.lower()]
    return voice


async def resolve_voice_async(voice: str | None) -> str:
    if not voice:
        return os.environ.get("ELEVENLABS_VOICE_ID", "")
    if voice.lower() in VOICE_BY_NAME:
        return VOICE_BY_NAME[voice.lower()]
    api_voices = await asyncio.to_thread(_fetch_voices_from_api)
    if voice.lower() in api_voices:
        return api_voices[voice.lower()]
    return voice


def voice_label(voice_id: str) -> str:
    return VOICE_ROSTER.get(voice_id, voice_id[:12])


# --- SSE Broadcaster ---

MAX_SSE_QUEUE = 256
MAX_TEXT_LENGTH = 10000
MAX_HISTORY = 1000


class SSEBroadcaster:
    def __init__(self):
        self._clients: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_SSE_QUEUE)
        self._clients.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._clients.remove(q)
        except ValueError:
            pass

    async def send(self, event: str, data: dict):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        dead: list[asyncio.Queue] = []
        for q in list(self._clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._clients.remove(q)
            except ValueError:
                pass


# --- Audio Duration ---

def _get_audio_duration(path: str) -> float | None:
    try:
        result = subprocess.run(
            ["afinfo", path],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"estimated duration:\s*([\d.]+)", result.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def _extract_envelope(path: str, chunk_ms: int = 50) -> list[float]:
    try:
        result = subprocess.run(
            [FFMPEG, "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000",
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


# --- ElevenLabs API (sync, run via asyncio.to_thread) ---

def _api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "")


def _fetch_tts(text: str, voice_id: str) -> str:
    url = f"{API_BASE}/text-to-speech/{voice_id}?output_format={DEFAULT_FORMAT}"
    payload = json.dumps({"text": text, "model_id": DEFAULT_MODEL}).encode()
    req = Request(url, data=payload, headers={
        "xi-api-key": _api_key(),
        "Content-Type": "application/json",
    })
    with urlopen(req) as resp:
        data = resp.read()
    fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _fetch_dialogue(inputs: list[dict]) -> str:
    url = f"{API_BASE}/text-to-dialogue?output_format={DEFAULT_FORMAT}"
    payload = json.dumps({"inputs": inputs, "model_id": DEFAULT_MODEL}).encode()
    req = Request(url, data=payload, headers={
        "xi-api-key": _api_key(),
        "Content-Type": "application/json",
    })
    with urlopen(req) as resp:
        data = resp.read()
    fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


# --- Audio Queue ---

@dataclass
class QueueEntry:
    id: str
    audio_path: str
    text_preview: str
    voice_label: str
    created_at: float
    entry_type: str = "speak"
    dialogue_segments: list[dict] = field(default_factory=list)
    channel: str | None = None
    priority: bool = False
    history_id: str = ""
    full_text: str = ""
    is_replay: bool = False

    def __post_init__(self):
        if not self.history_id:
            self.history_id = self.id


class AudioQueue:
    def __init__(self, broadcaster: SSEBroadcaster):
        self._deque: collections.deque[QueueEntry] = collections.deque()
        self._has_items = asyncio.Event()
        self._paused_global = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._paused_channels: set[str] = set()
        self._current: QueueEntry | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._history: list[dict] = []
        self._broadcaster = broadcaster
        self._cache_dir = CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._pause_requested = False
        self._play_start: float = 0.0
        self._seek_offset: float | None = None

    def start(self):
        asyncio.create_task(self._worker())

    def enqueue(self, entry: QueueEntry) -> int:
        if entry.priority:
            self._deque.appendleft(entry)
        else:
            self._deque.append(entry)
        self._has_items.set()
        return len(self._deque)

    def _pick_next(self) -> QueueEntry | None:
        for i, entry in enumerate(self._deque):
            if entry.channel and entry.channel in self._paused_channels:
                continue
            del self._deque[i]
            return entry
        return None

    async def _trim_audio(self, path: str, offset_seconds: float) -> str:
        fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
        os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-ss", str(offset_seconds), "-i", path,
            "-acodec", "libmp3lame", "-ab", "128k", "-y", tmp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return tmp

    async def _worker(self):
        while True:
            await self._has_items.wait()

            if self._paused_global:
                await self._resume_event.wait()

            entry = self._pick_next()
            if not entry:
                self._has_items.clear()
                continue

            self._current = entry
            self._seek_offset = None
            duration = None
            play_offset = 0.0
            trimmed_path = None
            play_failed = False
            try:
                duration, envelope = await asyncio.gather(
                    asyncio.to_thread(_get_audio_duration, entry.audio_path),
                    asyncio.to_thread(_extract_envelope, entry.audio_path),
                )

                # Cache MP3 for history replay
                cache_path = self._cache_dir / f"{entry.history_id}.mp3"
                try:
                    await asyncio.to_thread(shutil.copy2, entry.audio_path, str(cache_path))
                except Exception:
                    pass

                if entry.entry_type == "dialogue" and entry.dialogue_segments and duration:
                    total_chars = sum(s.get("chars", 1) for s in entry.dialogue_segments)
                    seg_offset = 0.0
                    for seg in entry.dialogue_segments:
                        seg_dur = (seg.get("chars", 1) / max(total_chars, 1)) * duration
                        seg["start"] = round(seg_offset, 3)
                        seg["end"] = round(seg_offset + seg_dur, 3)
                        seg_offset += seg_dur

                while True:
                    # Determine which file to play
                    if play_offset > 0:
                        trimmed_path = await self._trim_audio(entry.audio_path, play_offset)
                        play_file = trimmed_path
                    else:
                        play_file = entry.audio_path

                    # Get envelope for current play file
                    play_dur, play_env = None, envelope
                    if play_offset > 0:
                        play_dur, play_env = await asyncio.gather(
                            asyncio.to_thread(_get_audio_duration, play_file),
                            asyncio.to_thread(_extract_envelope, play_file),
                        )
                    else:
                        play_dur = duration

                    self._process = await asyncio.create_subprocess_exec(
                        "afplay", play_file,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    self._play_start = time.monotonic()

                    voice_event = {
                        "id": entry.id,
                        "voice": entry.voice_label,
                        "type": entry.entry_type,
                        "text": entry.text_preview,
                        "duration": round(play_dur, 3) if play_dur else None,
                        "total_duration": round(duration, 3) if duration else None,
                        "offset": round(play_offset, 3),
                        "segments": entry.dialogue_segments if entry.entry_type == "dialogue" else None,
                        "envelope": play_env,
                        "chunk_ms": 50,
                        "queued": len(self._deque),
                        "channel": entry.channel,
                        "priority": entry.priority,
                    }
                    await self._broadcaster.send("voice_active", voice_event)

                    ret = await self._process.wait()
                    log.info(f"Worker: process exited rc={ret}, pause_requested={self._pause_requested}")

                    if ret != 0 and not self._pause_requested:
                        play_failed = True

                    # Clean up trimmed file
                    if trimmed_path:
                        try:
                            os.unlink(trimmed_path)
                        except OSError:
                            pass
                        trimmed_path = None

                    if self._pause_requested:
                        if self._seek_offset is not None:
                            play_offset = self._seek_offset
                            self._seek_offset = None
                            self._pause_requested = False
                            self._process = None
                            log.info(f"Worker: seek to offset={play_offset:.2f}s")
                            continue
                        elapsed = time.monotonic() - self._play_start
                        play_offset += elapsed
                        self._pause_requested = False
                        self._process = None
                        log.info(f"Worker: paused at offset={play_offset:.2f}s, waiting for resume")
                        await self._resume_event.wait()
                        log.info(f"Worker: resumed, will play from offset={play_offset:.2f}s")
                        continue
                    else:
                        break
            except Exception as exc:
                play_failed = True
                log.error(f"Worker: exception in playback loop: {exc}", exc_info=True)
            finally:
                if trimmed_path:
                    try:
                        os.unlink(trimmed_path)
                    except OSError:
                        pass
                try:
                    os.unlink(entry.audio_path)
                except OSError:
                    pass

                if not entry.is_replay:
                    history_entry = {
                        "id": entry.history_id,
                        "voice": entry.voice_label,
                        "text": entry.full_text or entry.text_preview,
                        "channel": entry.channel,
                        "timestamp": entry.created_at,
                        "duration": round(duration, 3) if duration else None,
                        "type": entry.entry_type,
                        "failed": play_failed,
                    }
                    self._history.append(history_entry)
                    if len(self._history) > MAX_HISTORY:
                        self._history = self._history[-MAX_HISTORY:]

                    await self._broadcaster.send("history_update", history_entry)

                self._current = None
                self._process = None

                if not self._deque:
                    self._has_items.clear()

                await self._broadcaster.send("voice_active", {
                    "id": None, "voice": None, "type": "idle",
                    "text": None, "duration": None, "segments": None,
                    "queued": len(self._deque),
                    "channel": None, "priority": False,
                })

    def status(self, channel: str | None = None) -> dict:
        items = []
        if self._current:
            if channel is None or self._current.channel == channel:
                items.append({
                    "position": 0, "status": "playing",
                    "id": self._current.id,
                    "voice": self._current.voice_label,
                    "text": self._current.text_preview,
                    "channel": self._current.channel,
                    "priority": self._current.priority,
                })

        for i, entry in enumerate(self._deque):
            if channel is not None and entry.channel != channel:
                continue
            items.append({
                "position": i + 1, "status": "queued",
                "id": entry.id,
                "voice": entry.voice_label,
                "text": entry.text_preview,
                "channel": entry.channel,
                "priority": entry.priority,
            })

        return {
            "playing": self._current is not None,
            "queued": len(self._deque),
            "total": len(items),
            "items": items,
            "paused": self._paused_global,
            "channel_paused": sorted(self._paused_channels),
        }

    async def clear(self, channel: str | None = None) -> int:
        cleared = 0
        if channel is None:
            while self._deque:
                entry = self._deque.popleft()
                try:
                    os.unlink(entry.audio_path)
                except OSError:
                    pass
                cleared += 1
            if self._process and self._process.returncode is None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                cleared += 1
            self._has_items.clear()
        else:
            new_deque = collections.deque()
            for entry in self._deque:
                if entry.channel == channel:
                    try:
                        os.unlink(entry.audio_path)
                    except OSError:
                        pass
                    cleared += 1
                else:
                    new_deque.append(entry)
            self._deque = new_deque
            if not self._deque:
                self._has_items.clear()
        return cleared

    async def skip(self) -> bool:
        if self._process and self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            return True
        return False

    def seek(self, offset: float) -> bool:
        if not self._current or not self._process or self._process.returncode is not None:
            return False
        self._seek_offset = offset
        self._pause_requested = True
        try:
            self._process.kill()
        except ProcessLookupError:
            self._pause_requested = False
            self._seek_offset = None
            return False
        return True

    def pause(self, channel: str | None = None):
        if channel is None:
            self._paused_global = True
            self._resume_event.clear()
            if self._process and self._process.returncode is None:
                self._pause_requested = True
                try:
                    self._process.kill()
                    log.info("Pause: killed process, pause_requested=True")
                except ProcessLookupError:
                    log.warning("Pause: process already dead")
                    self._pause_requested = False
            else:
                log.info("Pause: no active process to kill")
        else:
            self._paused_channels.add(channel)

    def resume(self, channel: str | None = None):
        if channel is None:
            self._paused_global = False
            self._resume_event.set()
            log.info("Resume: set resume event")
        else:
            self._paused_channels.discard(channel)

    def get_history(self, limit: int = 50, offset: int = 0, channel: str | None = None) -> list[dict]:
        entries = self._history
        if channel is not None:
            entries = [e for e in entries if e.get("channel") == channel]
        entries = list(reversed(entries))
        return entries[offset:offset + limit]

    def find_history(self, history_id: str) -> dict | None:
        for entry in reversed(self._history):
            if entry["id"] == history_id:
                return entry
        return None


def _clean_old_cache(cache_dir: Path, max_age_hours: int = 24):
    if not cache_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


# --- REST API Route Handlers ---

async def handle_speak(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "No text provided"}, status_code=400)
    if len(text) > MAX_TEXT_LENGTH:
        return JSONResponse({"error": f"Text too long (max {MAX_TEXT_LENGTH} chars)"}, status_code=400)

    voice_raw = body.get("voice")
    if voice_raw is not None and not isinstance(voice_raw, str):
        return JSONResponse({"error": "Voice must be a string"}, status_code=400)
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)

    vid = await resolve_voice_async(voice_raw)
    if not _api_key():
        return JSONResponse({"error": "ELEVENLABS_API_KEY not set"}, status_code=500)
    if not vid:
        return JSONResponse({"error": "No voice specified and ELEVENLABS_VOICE_ID not set"}, status_code=400)

    try:
        path = await asyncio.to_thread(_fetch_tts, text, vid)
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        return JSONResponse({"error": f"API {e.code}: {err_body}"}, status_code=502)
    except URLError as e:
        return JSONResponse({"error": f"Network: {e.reason}"}, status_code=502)

    entry_id = uuid.uuid4().hex[:8]
    entry = QueueEntry(
        id=entry_id,
        audio_path=path,
        text_preview=text[:100],
        voice_label=voice_label(vid),
        created_at=time.time(),
        channel=channel or None,
        priority=bool(body.get("priority", False)),
        full_text=text,
    )
    pos = queue.enqueue(entry)
    return JSONResponse({
        "id": entry.id,
        "position": pos,
        "voice": entry.voice_label,
        "text_preview": entry.text_preview,
    })


async def handle_speak_dialogue(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    dialogue = body.get("dialogue", [])
    if not isinstance(dialogue, list) or not dialogue:
        return JSONResponse({"error": "No dialogue provided"}, status_code=400)
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)
    if not _api_key():
        return JSONResponse({"error": "ELEVENLABS_API_KEY not set"}, status_code=500)

    inputs = []
    labels = []
    for i, line in enumerate(dialogue):
        if not isinstance(line, dict):
            return JSONResponse({"error": f"Dialogue item {i} must be an object"}, status_code=400)
        text = line.get("text")
        voice = line.get("voice")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": f"Dialogue item {i} missing 'text'"}, status_code=400)
        if len(text) > MAX_TEXT_LENGTH:
            return JSONResponse({"error": f"Dialogue item {i} text too long"}, status_code=400)
        if voice is not None and not isinstance(voice, str):
            return JSONResponse({"error": f"Dialogue item {i} voice must be a string"}, status_code=400)
        vid = await resolve_voice_async(voice)
        if not vid:
            return JSONResponse({"error": f"Cannot resolve voice: {voice}"}, status_code=400)
        inputs.append({"voice_id": vid, "text": text})
        labels.append(voice_label(vid))

    try:
        path = await asyncio.to_thread(_fetch_dialogue, inputs)
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        return JSONResponse({"error": f"API {e.code}: {err_body}"}, status_code=502)
    except URLError as e:
        return JSONResponse({"error": f"Network: {e.reason}"}, status_code=502)

    voices_str = " + ".join(sorted(set(labels)))
    preview = " / ".join(f"{l}: \"{t['text'][:25]}\"" for l, t in zip(labels, dialogue))
    full_dialogue = " / ".join(f"{l}: \"{line['text']}\"" for l, line in zip(labels, dialogue))
    segments = [
        {"voice": lbl, "text": line["text"], "chars": len(line["text"])}
        for lbl, line in zip(labels, dialogue)
    ]

    entry_id = uuid.uuid4().hex[:8]
    entry = QueueEntry(
        id=entry_id,
        audio_path=path,
        text_preview=preview[:100],
        voice_label=voices_str,
        created_at=time.time(),
        entry_type="dialogue",
        dialogue_segments=segments,
        channel=channel or None,
        priority=bool(body.get("priority", False)),
        full_text=full_dialogue,
    )
    pos = queue.enqueue(entry)
    return JSONResponse({
        "id": entry.id,
        "position": pos,
        "voices": voices_str,
    })


async def handle_queue_status(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    channel = request.query_params.get("channel")
    return JSONResponse(queue.status(channel=channel))


async def handle_queue_clear(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)
    n = await queue.clear(channel=channel)
    return JSONResponse({"cleared": n})


async def handle_queue_skip(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    skipped = await queue.skip()
    return JSONResponse({"skipped": skipped})


async def handle_queue_seek(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)
    offset = body.get("offset")
    if offset is None:
        return JSONResponse({"error": "No offset provided"}, status_code=400)
    try:
        offset = float(offset)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid offset"}, status_code=400)
    seeked = queue.seek(max(0.0, offset))
    if not seeked:
        return JSONResponse({"error": "Nothing playing to seek"}, status_code=409)
    return JSONResponse({"seeked": True, "offset": offset})


async def handle_queue_pause(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)
    queue.pause(channel=channel)

    await request.app.state.broadcaster.send("pause_state", {
        "global_paused": queue._paused_global,
        "channel_paused": sorted(queue._paused_channels),
    })
    return JSONResponse({"paused": True, "channel": channel})


async def handle_queue_resume(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)
    queue.resume(channel=channel)

    await request.app.state.broadcaster.send("pause_state", {
        "global_paused": queue._paused_global,
        "channel_paused": sorted(queue._paused_channels),
    })
    return JSONResponse({"resumed": True, "channel": channel})


async def handle_history(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except (ValueError, TypeError):
        offset = 0
    channel = request.query_params.get("channel")
    entries = queue.get_history(limit=limit, offset=offset, channel=channel)
    return JSONResponse({"entries": entries, "total": len(queue._history)})


async def handle_history_replay(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    history_id = body.get("id", "")
    if not isinstance(history_id, str) or not history_id:
        return JSONResponse({"error": "No id provided"}, status_code=400)

    entry_data = queue.find_history(history_id)
    if not entry_data:
        return JSONResponse({"error": "Entry not found in history"}, status_code=404)

    cache_path = queue._cache_dir / f"{history_id}.mp3"
    if not cache_path.exists():
        return JSONResponse({"error": "Cached audio not found (may have expired)"}, status_code=404)

    # Copy cached MP3 to temp file for playback (worker deletes after play)
    fd, tmp_path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(cache_path.read_bytes())

    replay_id = uuid.uuid4().hex[:8]
    entry = QueueEntry(
        id=replay_id,
        audio_path=tmp_path,
        text_preview=entry_data.get("text", ""),
        voice_label=entry_data.get("voice", ""),
        created_at=time.time(),
        entry_type=entry_data.get("type", "speak"),
        channel=entry_data.get("channel"),
        history_id=replay_id,
        is_replay=True,
    )
    pos = queue.enqueue(entry)
    return JSONResponse({"id": replay_id, "position": pos, "replaying": history_id})


async def handle_events(request: StarletteRequest) -> StreamingResponse:
    broadcaster: SSEBroadcaster = request.app.state.broadcaster
    queue: AudioQueue = request.app.state.queue
    client_q = broadcaster.subscribe()

    async def stream():
        try:
            state = queue.status()
            state["recent_history"] = queue.get_history(limit=20)
            yield f"event: state\ndata: {json.dumps(state)}\n\n"
            while True:
                msg = await client_q.get()
                yield msg
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(client_q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_health(request: StarletteRequest) -> JSONResponse:
    queue: AudioQueue = request.app.state.queue
    return JSONResponse({
        "status": "ok",
        "version": "2.0",
        "queue_size": len(queue._deque) + (1 if queue._current else 0),
    })


async def handle_index(request: StarletteRequest) -> HTMLResponse:
    index_path = DASHBOARD_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


async def handle_voices(request: StarletteRequest) -> JSONResponse:
    voices_path = REPO_ROOT / "voices.json"
    if voices_path.exists():
        try:
            data = json.loads(voices_path.read_text())
            return JSONResponse(data)
        except json.JSONDecodeError:
            pass
    return JSONResponse([])


async def handle_portrait(request: StarletteRequest) -> FileResponse | HTMLResponse:
    name = request.path_params["name"]
    portraits_root = (DASHBOARD_DIR / "portraits").resolve()
    portrait_path = (portraits_root / name).resolve()
    try:
        portrait_path.relative_to(portraits_root)
    except ValueError:
        return HTMLResponse("Not found", status_code=404)

    if portrait_path.exists() and portrait_path.is_file():
        suffix = portrait_path.suffix.lower()
        media = {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        return FileResponse(portrait_path, media_type=media)
    return HTMLResponse("Not found", status_code=404)


# --- Main ---

async def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _clean_old_cache(CACHE_DIR)

    async def _periodic_cache_cleanup():
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(_clean_old_cache, CACHE_DIR)
            except Exception as e:
                log.warning(f"Cache cleanup error: {e}")

    broadcaster = SSEBroadcaster()
    queue = AudioQueue(broadcaster)
    queue.start()
    asyncio.create_task(_periodic_cache_cleanup())

    app = Starlette(middleware=[Middleware(LocalhostGuardMiddleware)], routes=[
        Route("/speak", handle_speak, methods=["POST"]),
        Route("/speak/dialogue", handle_speak_dialogue, methods=["POST"]),
        Route("/queue", handle_queue_status, methods=["GET"]),
        Route("/queue/clear", handle_queue_clear, methods=["POST"]),
        Route("/queue/skip", handle_queue_skip, methods=["POST"]),
        Route("/queue/seek", handle_queue_seek, methods=["POST"]),
        Route("/queue/pause", handle_queue_pause, methods=["POST"]),
        Route("/queue/resume", handle_queue_resume, methods=["POST"]),
        Route("/history", handle_history, methods=["GET"]),
        Route("/history/replay", handle_history_replay, methods=["POST"]),
        Route("/events", handle_events, methods=["GET"]),
        Route("/voices", handle_voices, methods=["GET"]),
        Route("/health", handle_health, methods=["GET"]),
        Route("/", handle_index, methods=["GET"]),
        Route("/portraits/{name:path}", handle_portrait, methods=["GET"]),
    ])
    app.state.queue = queue
    app.state.broadcaster = broadcaster

    config = uvicorn.Config(
        app, host="127.0.0.1", port=DASHBOARD_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

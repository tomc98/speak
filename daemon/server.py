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
  POST /voices             Create voice
  PATCH /voices/{name}     Update/rename voice
  DELETE /voices/{name}    Delete voice
  GET  /events             SSE stream
  GET  /health             Health check
  GET  /                   Dashboard
  GET  /portraits/{name}   Portrait images
  POST /portraits/{name}   Upload portrait PNG (?frame=default|slight|open)
"""

import asyncio
import collections
import concurrent.futures
import contextlib
import json
import logging
import os

log = logging.getLogger("voice-daemon")
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from http.client import IncompleteRead
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
FFPLAY = (
    shutil.which("ffplay")
    or next((p for p in ("/opt/homebrew/bin/ffplay", "/usr/local/bin/ffplay") if Path(p).exists()), "ffplay")
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
        if not value:
            # A blank template line is an ABSENT setting, not an empty one: a blank
            # SPEAK_CACHE_DIR would resolve the cache sweep to the working directory,
            # and a blank int knob raises at import with no way to override it.
            continue
        os.environ.setdefault(key, value)


_load_dotenv()

DASHBOARD_PORT = int(os.environ.get("SPEAK_PORT", "7865"))
CACHE_DIR = Path(os.environ.get("SPEAK_CACHE_DIR", str(REPO_ROOT / "cache")))

# --- Streaming engine config ---

STREAMING_ENABLED = os.environ.get("SPEAK_STREAMING", "1") != "0"
SPEAK_MODEL = os.environ.get("SPEAK_MODEL", DEFAULT_MODEL)
PREROLL_MS = int(os.environ.get("SPEAK_PREROLL_MS", "500"))
RESUME_REWIND_MS = int(os.environ.get("SPEAK_RESUME_REWIND_MS", "1000"))
LIVE_PLAYER_PREF = os.environ.get("SPEAK_LIVE_PLAYER", "auto")

CONVERSATIONAL_MODEL = "eleven_v3_conversational"
CONVERSATIONAL_MAX_CHARS = 2000  # vendor: dialogue requests beyond this can terminate early
STREAM_MAX_CHARS = 5000          # eleven_v3 documented character limit

AVAILABLE_MODELS = [DEFAULT_MODEL, CONVERSATIONAL_MODEL]
CONFIG_PATH = REPO_ROOT / "config.json"


def _read_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"Failed to load {path.name}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_model(path: Path | None = None) -> str:
    """Persisted setting wins over the env default, which wins over the built-in.

    Every source is validated, because the resolved value is what /config and the
    state snapshot ADVERTISE. An unknown value passed through would have the UI
    naming one model while the hop chain, which can only route the known set,
    synthesized with another.
    """
    stored = _read_config(path).get("model")
    if isinstance(stored, str) and stored in AVAILABLE_MODELS:
        return stored
    if isinstance(stored, str):
        log.warning(f"config.json model={stored!r} is not one of {AVAILABLE_MODELS} — ignoring it")
    if SPEAK_MODEL in AVAILABLE_MODELS:
        return SPEAK_MODEL
    log.warning(
        f"SPEAK_MODEL={SPEAK_MODEL!r} is not one of {AVAILABLE_MODELS} — "
        f"falling back to {DEFAULT_MODEL}, which is what synthesis would have used anyway"
    )
    return DEFAULT_MODEL


CURRENT_MODEL = _resolve_model()


def _write_config(model: str, path: Path | None = None):
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"model": model}, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

SOCKET_TIMEOUT = 30    # urlopen's single per-operation timeout = the inter-chunk deadline
ENTRY_DEADLINE = 180   # per-entry wall clock, enforced by an independent supervisor
ATTEMPT_BUDGET_EXTRA = 1   # one spare slot so a same-hop transport retry never costs a hop
CANCEL_JOIN_TIMEOUT = 5    # bound on waiting for aborted collectors, in clear and shutdown
MAX_WORKER_RESTARTS = 5
WORKER_RESTART_DECAY = 600  # seconds of health after which the restart budget resets
CHUNK_SIZE = 16384
ENVELOPE_CHUNK_MS = 50
APPEND_BATCH_MS = 300
# mp3_44100_128 is CBR; bytes fed to the player convert to seconds at this rate.
LIVE_CBR_BYTES_PER_SEC = 16000
# How far ahead of real time the player feeder may run. The pipe and the player's own
# queue accept far more audio than they have played, so an unpaced feeder's bytes-fed
# count races seconds ahead of what was heard and a resume would SKIP. Keeping the lead
# below SPEAK_RESUME_REWIND_MS is what makes the resume land at-or-before the heard
# position. The attempt file on disk stays the jitter buffer, so network tolerance is
# unchanged. Writes go out in SLICE-sized pieces paced at their START, so the first
# bytes reach the player immediately and the worst-case lead is LEAD + SLICE.
LIVE_FEED_LEAD_BYTES = 8000
LIVE_FEED_SLICE_BYTES = 4000
COLLECTOR_WORKERS = int(os.environ.get("SPEAK_COLLECTOR_WORKERS", "8"))

PROBE_FIXTURE = REPO_ROOT / "assets" / "probe.mp3"

# One table: the probe runs the exact command that will play, over a digitally silent
# fixture (so no mute flag is needed and no backend can be probed but never run).
# Probing with EMPTY input instead false-passes ffplay (exit 0) and false-fails
# audiotoolbox (exit 183) — both reproduced on this machine.
PLAYER_COMMANDS = {
    "ffplay": [FFPLAY, "-autoexit", "-nodisp", "-loglevel", "quiet", "-i", "pipe:0"],
    "audiotoolbox": [FFMPEG, "-loglevel", "error", "-i", "pipe:0", "-f", "audiotoolbox", "-"],
}

LIVE_PLAYER: str | None = None
_collector_executor: "concurrent.futures.ThreadPoolExecutor | None" = None


def _collector_pool() -> "concurrent.futures.ThreadPoolExecutor":
    """Collections run off the default executor so they cannot starve playback feeders."""
    global _collector_executor
    if _collector_executor is None:
        _collector_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=COLLECTOR_WORKERS, thread_name_prefix="tts-collect",
        )
    return _collector_executor


def _probe_player(name: str) -> bool:
    try:
        fixture = PROBE_FIXTURE.read_bytes()
    except OSError as e:
        log.warning(f"live player probe: fixture unreadable ({e}) — live mode unavailable")
        return False
    try:
        result = subprocess.run(
            PLAYER_COMMANDS[name], input=fixture,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except Exception as e:
        log.warning(f"live player probe: {name} failed to run ({e})")
        return False
    if result.returncode != 0:
        log.warning(f"live player probe: {name} exited {result.returncode}")
        return False
    return True


def _select_live_player() -> str | None:
    if not STREAMING_ENABLED:
        return None
    order = ["ffplay", "audiotoolbox"] if LIVE_PLAYER_PREF == "auto" else [LIVE_PLAYER_PREF]
    for name in order:
        if name not in PLAYER_COMMANDS:
            log.warning(f"SPEAK_LIVE_PLAYER={name!r} is not a known backend")
            continue
        if _probe_player(name):
            log.info(f"live player: {name}")
            return name
    log.warning("live player: none available — every entry plays in file mode")
    return None


def _validate_model_config():
    if CURRENT_MODEL == CONVERSATIONAL_MODEL and not STREAMING_ENABLED:
        log.warning(
            f"model={CURRENT_MODEL} but SPEAK_STREAMING=0 — the legacy path synthesizes "
            f"with {DEFAULT_MODEL}, so the conversational model cannot be reached"
        )
    if COLLECTOR_WORKERS < 1:
        log.warning(
            f"SPEAK_COLLECTOR_WORKERS={COLLECTOR_WORKERS} is below 1 — the collector pool "
            f"cannot be built and every entry will fail at its first collection"
        )
    worst_lead_ms = (LIVE_FEED_LEAD_BYTES + LIVE_FEED_SLICE_BYTES) / LIVE_CBR_BYTES_PER_SEC * 1000
    if RESUME_REWIND_MS <= worst_lead_ms:
        log.warning(
            f"SPEAK_RESUME_REWIND_MS={RESUME_REWIND_MS} is at or below the feeder's worst-case "
            f"lead of {worst_lead_ms:.0f}ms — a live resume can SKIP audio instead of replaying it"
        )


VOICES_PATH = REPO_ROOT / "voices.json"
PORTRAITS_DIR = DASHBOARD_DIR / "portraits"
PORTRAIT_FRAMES = {"default": "", "slight": "_slight", "open": "_open"}


def _load_voices() -> tuple[list[dict], dict[str, str], dict[str, str]]:
    records: list[dict] = []
    roster: dict[str, str] = {}
    by_name: dict[str, str] = {}
    if VOICES_PATH.exists():
        try:
            entries = json.loads(VOICES_PATH.read_text())
            if not isinstance(entries, list):
                log.warning("voices.json is not a list")
                return records, roster, by_name
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                vid = entry.get("id")
                if not isinstance(name, str) or not isinstance(vid, str):
                    continue
                if "kind" not in entry:
                    entry["kind"] = "default"
                records.append(entry)
                roster[vid] = name
                by_name[name.lower()] = vid
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Failed to load voices.json: {e}")
    return records, roster, by_name


VOICE_RECORDS, VOICE_ROSTER, VOICE_BY_NAME = _load_voices()


def _rebuild_voice_indexes():
    VOICE_ROSTER.clear()
    VOICE_BY_NAME.clear()
    for rec in VOICE_RECORDS:
        name = rec.get("name")
        vid = rec.get("id")
        if isinstance(name, str) and isinstance(vid, str):
            VOICE_ROSTER[vid] = name
            VOICE_BY_NAME[name.lower()] = vid


def _save_voices():
    VOICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".voices-", suffix=".json", dir=str(VOICES_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(VOICE_RECORDS, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, VOICES_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find_voice_index(name: str) -> int:
    target = name.lower()
    for i, rec in enumerate(VOICE_RECORDS):
        if isinstance(rec.get("name"), str) and rec["name"].lower() == target:
            return i
    return -1


def _portrait_path(name: str, frame: str = "default") -> Path:
    suffix = PORTRAIT_FRAMES[frame]
    return PORTRAITS_DIR / f"{name.lower()}{suffix}.png"


def _has_portrait(name: str) -> bool:
    return _portrait_path(name, "default").exists()


def _delete_portraits(name: str):
    for frame in PORTRAIT_FRAMES:
        p = _portrait_path(name, frame)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                log.warning(f"Failed to delete portrait {p}: {e}")


def _rename_portraits(old_name: str, new_name: str):
    for frame in PORTRAIT_FRAMES:
        src = _portrait_path(old_name, frame)
        dst = _portrait_path(new_name, frame)
        if src.exists() and src != dst:
            try:
                os.replace(src, dst)
            except OSError as e:
                log.warning(f"Failed to rename portrait {src} -> {dst}: {e}")

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


def _extract_envelope(path: str, chunk_ms: int = ENVELOPE_CHUNK_MS) -> list[float]:
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


def _validate_mp3(data: bytes) -> bool:
    if len(data) < 4:
        return False
    if data[:3] == b"ID3":
        return True
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    return False


def _fetch_tts(text: str, voice_id: str, retries: int = 2) -> str:
    url = f"{API_BASE}/text-to-speech/{voice_id}?output_format={DEFAULT_FORMAT}"
    payload = json.dumps({"text": text, "model_id": DEFAULT_MODEL}).encode()
    for attempt in range(1 + retries):
        req = Request(url, data=payload, headers={
            "xi-api-key": _api_key(),
            "Content-Type": "application/json",
        })
        with urlopen(req) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
        if not _validate_mp3(data):
            log.warning(f"TTS attempt {attempt+1}: invalid MP3 (Content-Type={content_type}, {len(data)} bytes)")
            if attempt < retries:
                continue
            raise ValueError(f"API returned invalid audio after {1+retries} attempts")
        break
    fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _attempt_budget(chain: list[tuple[str, str]]) -> int:
    """One slot per hop plus one spare.

    A same-hop transport retry consumes a slot, so a budget equal to the chain length
    silently spends the middle hop: a Stage-2 entry whose conversational attempt fails
    on transport would go conversational, conversational, legacy and never try the
    v3 stream hop the fallback exists for.
    """
    return len(chain) + ATTEMPT_BUDGET_EXTRA


def _hop_chain(text: str) -> list[tuple[str, str]]:
    """Model-fallback hops for one entry, most-preferred first.

    The last hop is always the legacy non-stream route, so an entry that exhausts
    its transport retries still gets today's synthesis path.
    """
    chain: list[tuple[str, str]] = []
    if CURRENT_MODEL == CONVERSATIONAL_MODEL and len(text) <= CONVERSATIONAL_MAX_CHARS:
        chain.append(("conversational", CONVERSATIONAL_MODEL))
    chain.append(("v3_stream", DEFAULT_MODEL))
    chain.append(("legacy", DEFAULT_MODEL))
    return chain


def _build_request(hop: str, model: str, text: str, voice_id: str) -> tuple[str, bytes]:
    if hop == "conversational":
        url = f"{API_BASE}/text-to-dialogue/stream?output_format={DEFAULT_FORMAT}"
        payload = {"inputs": [{"text": text, "voice_id": voice_id}], "model_id": model}
    elif hop == "v3_stream":
        url = f"{API_BASE}/text-to-speech/{voice_id}/stream?output_format={DEFAULT_FORMAT}"
        payload = {"text": text, "model_id": model}
    else:
        url = f"{API_BASE}/text-to-speech/{voice_id}?output_format={DEFAULT_FORMAT}"
        payload = {"text": text, "model_id": model}
    return url, json.dumps(payload).encode()


def _framing_of(resp) -> str:
    te = (resp.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        return "chunked"
    if resp.headers.get("Content-Length") is not None:
        return "content-length"
    return "close"


def _atomic_cache_commit(src: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".commit-", suffix=".mp3", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            shutil.copyfileobj(f, out)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fetch_dialogue(inputs: list[dict], retries: int = 2) -> str:
    url = f"{API_BASE}/text-to-dialogue?output_format={DEFAULT_FORMAT}"
    payload = json.dumps({"inputs": inputs, "model_id": DEFAULT_MODEL}).encode()
    for attempt in range(1 + retries):
        req = Request(url, data=payload, headers={
            "xi-api-key": _api_key(),
            "Content-Type": "application/json",
        })
        with urlopen(req) as resp:
            data = resp.read()
        if not _validate_mp3(data):
            log.warning(f"Dialogue attempt {attempt+1}: invalid MP3 ({len(data)} bytes)")
            if attempt < retries:
                continue
            raise ValueError(f"API returned invalid audio after {1+retries} attempts")
        break
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
    session: str | None = None
    priority: bool = False
    history_id: str = ""
    full_text: str = ""
    is_replay: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Any lifecycle change (new generation, pre-roll reached, terminal outcome) sets wake;
    # the worker re-reads state on every wake rather than trusting the event it woke on.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: str | None = None
    playback_path: str | None = None
    attempt_path: str | None = None
    generation: int = 0
    started_generation: int | None = None
    claimed_generation: int | None = None
    epoch: str | None = None
    detached: bool = False
    # Cleared is distinct from detached: skip pins a failed:true history record, clear
    # pins none. It also survives a write-once finish() that no-ops because the entry
    # had already completed, which is the only signal a cleared file-mode entry has.
    cleared: bool = False
    collector: "StreamCollector | None" = None
    final_duration: float | None = None
    final_envelope: list[float] = field(default_factory=list)
    history_deferred: bool = False
    fetch_task: "asyncio.Task | None" = None
    # created_at is wall-clock, for display. This is the monotonic origin the SLO
    # measures from, stamped at enqueue.
    enqueued_at: float = 0.0
    stats: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.history_id:
            self.history_id = self.id
        if self.audio_path:
            self.playback_path = self.audio_path
            self.outcome = "complete"
            self.ready.set()

    @property
    def fetch_failed(self) -> bool:
        return self.outcome == "failed"


@dataclass
class Attempt:
    generation: int
    path: str
    hop: str
    model: str
    resp: object | None = None
    framing: str = "unknown"
    bytes_written: int = 0
    ttfb_ms: float | None = None


class StreamCollector:
    """Streams one entry's audio into generation-scoped attempt files.

    Network and file only: it spawns no decoders and emits no SSE, so a queued
    entry costs one request and one file handle no matter how deep it sits.
    """

    def __init__(self, queue: "AudioQueue", entry: QueueEntry, text: str, voice_id: str):
        self._queue = queue
        self._entry = entry
        self._text = text
        self._voice_id = voice_id
        self._attempt: Attempt | None = None
        self._attempt_paths: list[str] = []
        self._published_path: str | None = None
        self._aborted = False
        self._deadline_hit = False
        self._task: asyncio.Task | None = None
        self._deadline_task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        self._queue.register_collector(self)

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    async def _run(self):
        entry = self._entry
        self._deadline_task = asyncio.create_task(self._supervise_deadline())
        chain = _hop_chain(self._text)
        budget = _attempt_budget(chain)
        hop_idx = 0
        hop_retried = False
        started_at = time.monotonic()
        try:
            for attempt_no in range(budget):
                if self._aborted or self._deadline_hit or entry.outcome is not None:
                    return
                if attempt_no == budget - 1:
                    hop_idx = len(chain) - 1
                hop, model = chain[min(hop_idx, len(chain) - 1)]
                attempt = self._new_attempt(hop, model)
                loop = asyncio.get_running_loop()
                kind, detail = await loop.run_in_executor(_collector_pool(), self._pull, attempt)
                # _deadline_hit belongs in this gate: under chunked framing a fired
                # supervisor surfaces as IncompleteRead, which classifies as transport
                # and would otherwise buy a whole further attempt past the deadline.
                if self._aborted or self._deadline_hit or entry.outcome is not None:
                    if self._deadline_hit:
                        self._queue.finish(entry, entry.generation, "failed")
                    return
                if kind == "ok":
                    await self._commit(attempt, started_at)
                    return
                log.warning(
                    f"tts attempt {attempt_no + 1}/{budget} id={entry.id} hop={hop} "
                    f"model={model} gen={attempt.generation} {kind}: {detail}"
                )
                if entry.claimed_generation is not None:
                    # Post-claim the audio is already playing: the feeder exhausts and the
                    # player drains. Nothing re-synthesizes after a claim.
                    self._queue.finish(entry, entry.generation, "failed")
                    return
                self._discard(attempt)
                if kind == "interrupted":
                    break
                if kind == "rejection":
                    hop_idx += 1
                    hop_retried = False
                elif hop_retried:
                    hop_idx += 1
                    hop_retried = False
                else:
                    hop_retried = True
            self._queue.finish(entry, entry.generation, "failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"Collector failed for {entry.id}: {exc}", exc_info=True)
            self._queue.finish(entry, entry.generation, "failed")
        finally:
            if self._deadline_task:
                self._deadline_task.cancel()
            self.cleanup()
            self._queue.unregister_collector(self)
            # A detached entry's history row was held back until its audio was
            # replayable; publish it now, whatever the outcome turned out to be.
            with contextlib.suppress(Exception):
                await self._queue.flush_deferred_history(entry)

    async def _supervise_deadline(self):
        # Independent of any in-flight read: a read begun near the deadline would
        # otherwise overrun it by a full socket timeout.
        try:
            await asyncio.sleep(ENTRY_DEADLINE)
        except asyncio.CancelledError:
            return
        self._deadline_hit = True
        log.warning(f"tts deadline: id={self._entry.id} exceeded {ENTRY_DEADLINE}s")
        self._shutdown_socket()

    def _new_attempt(self, hop: str, model: str) -> Attempt:
        entry = self._entry
        fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
        os.close(fd)
        self._attempt_paths.append(path)
        entry.generation += 1
        entry.started_generation = None
        entry.attempt_path = path
        attempt = Attempt(entry.generation, path, hop, model)
        self._attempt = attempt
        entry.wake.set()
        return attempt

    def _pull(self, attempt: Attempt) -> tuple[str, str | None]:
        """Blocking: runs in a worker thread, touches only its own attempt."""
        if self._aborted or self._deadline_hit:
            # The pool is bounded, so this call may have waited for a slot. Issuing a
            # billable request after the deadline already fired helps nobody.
            return ("interrupted", "aborted before the request was issued")
        url, payload = _build_request(attempt.hop, attempt.model, self._text, self._voice_id)
        req = Request(url, data=payload, headers={
            "xi-api-key": _api_key(),
            "Content-Type": "application/json",
        })
        t0 = time.monotonic()
        try:
            resp = urlopen(req, timeout=SOCKET_TIMEOUT)
        except HTTPError as e:
            return ("rejection" if 400 <= e.code < 500 else "transport", f"HTTP {e.code}")
        except (URLError, OSError) as e:
            return ("transport", repr(e))
        attempt.resp = resp
        attempt.framing = _framing_of(resp)
        # urlopen blocks before the response exists, so an abort or a deadline that
        # fired during connect found no socket to shut down. Re-check now that there is.
        if self._aborted or self._deadline_hit:
            self._shutdown_socket()
            with contextlib.suppress(Exception):
                resp.close()
            return ("interrupted", "aborted before first read")
        first_byte_at = None
        total = 0
        head = b""
        validated = False
        try:
            with open(attempt.path, "wb") as f:
                while True:
                    try:
                        chunk = resp.read1(CHUNK_SIZE)
                    except IncompleteRead as e:
                        return ("transport", f"IncompleteRead after {total} bytes: {e}")
                    except (OSError, ValueError) as e:
                        return ("transport", repr(e))
                    if not chunk:
                        break
                    if first_byte_at is None:
                        first_byte_at = time.monotonic()
                    if not validated:
                        # read1 may legally return 1-3 bytes; judging the format on a
                        # short first slice condemns a healthy stream as a rejection
                        # and burns the hop.
                        head += chunk
                        if len(head) >= 4:
                            validated = True
                            if not _validate_mp3(head):
                                return ("rejection", "response is not audio")
                    f.write(chunk)
                    f.flush()
                    total += len(chunk)
        finally:
            attempt.bytes_written = total
            if first_byte_at is not None:
                attempt.ttfb_ms = (first_byte_at - t0) * 1000.0
            with contextlib.suppress(Exception):
                resp.close()

        if self._aborted or self._deadline_hit:
            return ("interrupted", "socket shutdown")
        if total == 0:
            return ("transport", "empty body")
        if not validated and not _validate_mp3(head):
            return ("rejection", "response is not audio")
        if attempt.framing == "content-length" and getattr(resp, "length", None):
            # http.client deliberately does not raise here — the check is ours.
            return ("transport", f"truncated: {resp.length} bytes short")
        if attempt.framing == "close":
            log.info(f"tts id={self._entry.id} framing=close — early close is indistinguishable from EOF")
        return ("ok", None)

    async def _commit(self, attempt: Attempt, started_at: float):
        entry = self._entry
        cache_path = self._queue._cache_dir / f"{entry.history_id}.mp3"
        try:
            await asyncio.to_thread(_atomic_cache_commit, attempt.path, cache_path)
            entry.playback_path = str(cache_path)
        except OSError as exc:
            # The commit is publication, not a synthesis gate: EOF-verified audio must
            # not be thrown away because the cache directory is unwritable.
            log.warning(f"tts id={entry.id} cache commit failed ({exc}) — playing from the attempt file")
            entry.playback_path = attempt.path
            self._published_path = attempt.path
        entry.stats.update({
            "model": attempt.model,
            "hop": attempt.hop,
            "gen": attempt.generation,
            "ttfb_ms": round(attempt.ttfb_ms) if attempt.ttfb_ms is not None else None,
            "bytes": attempt.bytes_written,
            "framing": attempt.framing,
            "total_ms": round((time.monotonic() - started_at) * 1000),
        })
        # Metadata BEFORE the terminal outcome: a short live clip can otherwise finish,
        # drain and have its history written before the duration exists, so the record
        # keeps a null duration and the voice_update never reaches the clients.
        try:
            await self._queue.on_collection_complete(entry)
        except Exception as exc:
            log.warning(f"tts id={entry.id} post-collection metadata failed: {exc}")
        self._queue.finish(entry, attempt.generation, "complete")
        self.cleanup()

    def _discard(self, attempt: Attempt):
        try:
            os.unlink(attempt.path)
        except OSError:
            pass

    def _shutdown_socket(self):
        attempt = self._attempt
        resp = attempt.resp if attempt else None
        if resp is None:
            return
        # A finished collector has nothing to interrupt — routine on every clear.
        if getattr(resp, "fp", None) is None or resp.isclosed():
            return
        # The only interrupt that unblocks a read: close() deadlocks behind the
        # BufferedReader lock, and cancelling the thread does not stop it.
        try:
            resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception as exc:
            # Silence here means the read stays blocked to the socket timeout, so the
            # one unblock mechanism failing must never be invisible.
            log.warning(f"tts id={self._entry.id} socket shutdown failed: {exc!r}")

    def abort(self):
        self._aborted = True
        self._shutdown_socket()

    def cleanup(self):
        for path in self._attempt_paths:
            if path == self._published_path:
                continue  # the cache commit failed and this file IS the playback path
            try:
                os.unlink(path)
            except OSError:
                pass
        self._attempt_paths = [p for p in self._attempt_paths if p == self._published_path]


@dataclass
class LiveFeedState:
    started_at: float = 0.0
    bytes_fed: int = 0
    frozen: int | None = None
    # Time the feeder spent waiting for the collector. Excluded from the pacing clock:
    # counted, a stall banks credit and the catch-up burst puts the watermark back
    # ahead of the audio, which is the skip this pacing exists to prevent.
    stalled: float = 0.0

    def watermark(self) -> int:
        return self.bytes_fed if self.frozen is None else self.frozen


class EnvelopePipeline:
    """Worker-owned read-follow decoder for the head-of-queue entry.

    Drives pre-roll, the running-peak lip-sync envelope, and nothing else — it is
    silent until the entry it belongs to is the active playback generation.
    """

    def __init__(self, queue: "AudioQueue", entry: QueueEntry, generation: int, path: str):
        self._queue = queue
        self._entry = entry
        self.generation = generation
        self._path = path
        self._proc: asyncio.subprocess.Process | None = None
        self._src = None
        self._feeder: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self._values: list[float] = []
        self._peak = 0.0
        self._emitted = 0
        self._active = False
        self.done = False

    async def start(self):
        try:
            self._src = open(self._path, "rb")
        except OSError:
            self.done = True
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000",
                "-acodec", "pcm_s16le", "-loglevel", "error", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            # Missing ffmpeg, EMFILE, ENOMEM: the entry loses live mode, not the daemon.
            log.warning(f"envelope decoder unavailable for {self._entry.id}: {exc}")
            self.done = True
            with contextlib.suppress(Exception):
                self._src.close()
            self._src = None
            return
        self._feeder = asyncio.create_task(self._feed())
        self._reader = asyncio.create_task(self._read())

    @property
    def decoded_ms(self) -> int:
        return len(self._values) * ENVELOPE_CHUNK_MS

    def snapshot(self) -> tuple[list[float], int]:
        return self._normalized(0, len(self._values)), 0

    async def notify_active(self):
        """Publish the pre-roll batch immediately — a stall right after activation would
        otherwise leave audible playback with no envelope until the next PCM chunk."""
        self._active = True
        await self._maybe_emit(1)

    def _normalized(self, start: int, end: int) -> list[float]:
        peak = self._peak or 0.001
        return [round(min(v / peak, 1.0), 3) for v in self._values[start:end]]

    async def _feed(self):
        entry = self._entry
        drained = False
        try:
            while True:
                data = await asyncio.to_thread(self._src.read, CHUNK_SIZE)
                if data:
                    drained = False
                    self._proc.stdin.write(data)
                    await self._proc.stdin.drain()
                    continue
                if entry.generation != self.generation:
                    break
                if entry.outcome is not None:
                    # Bytes flushed between the empty read and the terminal outcome are
                    # already on disk: re-read to a true EOF before closing the pipe.
                    if drained:
                        break
                    drained = True
                    continue
                await asyncio.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

    async def _read(self):
        bytes_per_chunk = (16000 * ENVELOPE_CHUNK_MS // 1000) * 2
        batch_chunks = max(1, APPEND_BATCH_MS // ENVELOPE_CHUNK_MS)
        buf = b""
        try:
            while True:
                data = await self._proc.stdout.read(bytes_per_chunk * batch_chunks)
                if not data:
                    break
                buf += data
                while len(buf) >= bytes_per_chunk:
                    frame, buf = buf[:bytes_per_chunk], buf[bytes_per_chunk:]
                    n = len(frame) // 2
                    vals = struct.unpack(f"<{n}h", frame[:n * 2])
                    rms = (sum(v * v for v in vals) / n) ** 0.5 / 32768.0
                    self._values.append(rms)
                    self._peak = max(self._peak, rms)
                self._on_progress()
                await self._maybe_emit(batch_chunks)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning(f"envelope decoder error for {self._entry.id}: {exc}")
        finally:
            self.done = True
            with contextlib.suppress(Exception):
                await self._maybe_emit(1)

    def _on_progress(self):
        entry = self._entry
        if entry.generation != self.generation:
            return
        if entry.started_generation != self.generation and self.decoded_ms >= PREROLL_MS:
            entry.started_generation = self.generation
            entry.wake.set()

    async def _maybe_emit(self, batch_chunks: int):
        entry = self._entry
        if not self._active or entry.detached or entry.cleared or not entry.epoch:
            return
        if entry.generation != self.generation or self._queue.current is not entry:
            return
        pending = len(self._values) - self._emitted
        if pending <= 0 or (pending < batch_chunks and not self.done):
            return
        values = self._normalized(self._emitted, len(self._values))
        seq = self._emitted
        self._emitted = len(self._values)
        await self._queue.broadcaster.send("envelope_append", {
            "id": entry.id,
            "epoch": entry.epoch,
            "seq": seq,
            "values": values,
            "chunk_ms": ENVELOPE_CHUNK_MS,
        })

    async def aclose(self):
        self.done = True
        self._active = False
        for task in (self._feeder, self._reader):
            if task and not task.done():
                task.cancel()
        tasks = [t for t in (self._feeder, self._reader) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        if self._src:
            with contextlib.suppress(Exception):
                self._src.close()


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
        self._play_offset: float = 0.0
        self._seek_offset: float | None = None
        self._phase: str = "idle"
        self._live: LiveFeedState | None = None
        self._envelope: EnvelopePipeline | None = None
        self._worker_task: asyncio.Task | None = None
        self._shutting_down = False
        self._collectors: set[StreamCollector] = set()
        self._worker_restarts = 0
        self._worker_restart_window = 0.0
        self._worker_stopped = False

    @property
    def current(self) -> QueueEntry | None:
        return self._current

    @property
    def broadcaster(self) -> SSEBroadcaster:
        return self._broadcaster

    def register_collector(self, collector: "StreamCollector"):
        self._collectors.add(collector)

    def unregister_collector(self, collector: "StreamCollector"):
        self._collectors.discard(collector)

    def live_history_ids(self) -> set[str]:
        ids = {e.history_id for e in self._deque}
        if self._current is not None:
            ids.add(self._current.history_id)
        return ids

    def start(self):
        self._worker_task = asyncio.create_task(self._worker())
        self._worker_task.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, task: asyncio.Task):
        """The worker is the daemon's only playback driver — losing it is silent death."""
        if task.cancelled() or self._shutting_down:
            return
        exc = task.exception()
        if exc is None:
            log.error("Worker exited unexpectedly")
        else:
            log.error(f"Worker crashed: {exc!r}", exc_info=exc)
        # The budget counts crashes in a window, not over the daemon's lifetime: five
        # unrelated transient failures across weeks must not stop playback forever.
        now = time.monotonic()
        if now - self._worker_restart_window > WORKER_RESTART_DECAY:
            self._worker_restarts = 0
        self._worker_restart_window = now
        if self._worker_restarts >= MAX_WORKER_RESTARTS:
            self._worker_stopped = True
            log.error(
                f"Worker crashed {self._worker_restarts} times within "
                f"{WORKER_RESTART_DECAY}s — playback is stopped until the daemon restarts"
            )
            return
        self._worker_restarts += 1
        log.error(f"Restarting worker (attempt {self._worker_restarts})")
        self.start()

    def finish(self, entry: QueueEntry, generation: int, outcome: str) -> bool:
        """Write-once terminal transition on the event loop. ready is terminal for all outcomes."""
        if generation != entry.generation or entry.outcome is not None:
            return False
        entry.outcome = outcome
        entry.ready.set()
        entry.wake.set()
        return True

    def enqueue(self, entry: QueueEntry) -> int:
        entry.enqueued_at = time.monotonic()
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

    async def _await_playable(self, entry: QueueEntry) -> str:
        """Started-or-ready wait: revalidates the generation and the pause gate on every wake."""
        while True:
            if self._shutting_down:
                return "cancelled"
            if self._paused_global:
                self._phase = "paused"
                await self._resume_event.wait()
                continue
            if entry.cleared or entry.outcome == "cancelled":
                return "cancelled"
            if entry.outcome == "failed":
                return "failed"
            if entry.outcome == "complete":
                self._phase = "starting"
                return "file"
            await self._ensure_envelope_pipeline(entry)
            if LIVE_PLAYER is not None and entry.started_generation == entry.generation:
                return "live"
            self._phase = "collecting"
            entry.wake.clear()
            # A retry that landed during the pipeline await bumped the generation and
            # set wake, which the clear above just discarded — the stale-pipeline term
            # is what stops the entry silently falling through to file mode.
            stale_pipeline = self._envelope is not None and self._envelope.generation != entry.generation
            if (entry.outcome is not None or self._paused_global or entry.cleared
                    or stale_pipeline
                    or entry.started_generation == entry.generation):
                # Yield: without a suspension point this re-check loop can starve the
                # event loop outright if a guard above it is ever wrong.
                await asyncio.sleep(0)
                continue
            await entry.wake.wait()

    async def _ensure_envelope_pipeline(self, entry: QueueEntry):
        if entry.collector is None or LIVE_PLAYER is None or not entry.attempt_path:
            return
        pipe = self._envelope
        if pipe is not None and pipe.generation == entry.generation:
            return
        if pipe is not None:
            await self._retire_envelope_pipeline()
        self._envelope = EnvelopePipeline(self, entry, entry.generation, entry.attempt_path)
        await self._envelope.start()

    async def _retire_envelope_pipeline(self):
        pipe = self._envelope
        self._envelope = None
        if pipe is not None:
            await pipe.aclose()

    def _mark_first_audio(self, entry: QueueEntry):
        """Stamp when audio first reached a player — the SLO's actual subject.

        ttfb_ms is the vendor's first-byte latency, measured on the collector thread
        before any player exists, so it says nothing about when the user heard anything.
        """
        if entry.enqueued_at:
            entry.stats.setdefault(
                "first_audio_ms", round((time.monotonic() - entry.enqueued_at) * 1000)
            )

    def _freeze_live(self):
        """Freeze the bytes-fed watermark at the moment a control arrives."""
        state = self._live
        if state is not None and state.frozen is None:
            state.frozen = state.bytes_fed

    async def _pace_feed(self, state: LiveFeedState):
        """Hold the feeder to real time plus LIVE_FEED_LEAD_BYTES.

        Without this, drain() returns as fast as the pipe and the player's queue will
        accept — measured at 11.26 s of audio handed over by 5.09 s of wall clock — so
        the bytes-fed watermark stops describing what was heard and a resume skips.
        Paced at each slice's START, so the first bytes reach the player immediately.
        """
        ahead = state.bytes_fed - LIVE_FEED_LEAD_BYTES
        if ahead <= 0:
            return
        due = state.started_at + state.stalled + ahead / LIVE_CBR_BYTES_PER_SEC
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _feed_player(self, entry: QueueEntry, generation: int, src,
                           proc: asyncio.subprocess.Process, state: LiveFeedState):
        drained = False
        try:
            while True:
                data = await asyncio.to_thread(src.read, CHUNK_SIZE)
                if data:
                    drained = False
                    for i in range(0, len(data), LIVE_FEED_SLICE_BYTES):
                        if entry.detached or entry.cleared or entry.generation != generation:
                            return
                        await self._pace_feed(state)
                        piece = data[i:i + LIVE_FEED_SLICE_BYTES]
                        proc.stdin.write(piece)
                        await proc.stdin.drain()
                        if state.bytes_fed == 0:
                            self._mark_first_audio(entry)
                        state.bytes_fed += len(piece)
                    continue
                if entry.detached or entry.cleared or entry.generation != generation:
                    break
                if entry.outcome is not None:
                    # Bytes flushed between the empty read and the terminal outcome are
                    # already on disk: re-read to a true EOF or the last word is clipped.
                    if drained:
                        break
                    drained = True
                    if self._current is entry and self._phase == "playing":
                        self._phase = "draining"
                    continue
                stall_started = time.monotonic()
                await asyncio.sleep(0.05)
                state.stalled += time.monotonic() - stall_started
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    async def _play_live(self, entry: QueueEntry) -> tuple[str, float]:
        """Returns (result, resume_offset).

        result is done | skipped | cleared | truncated | failed | resume.
        """
        generation = entry.generation
        if self._shutting_down:
            return ("failed", 0.0)
        entry.claimed_generation = generation  # the single irreversible boundary
        self._phase = "starting"
        try:
            src = open(entry.attempt_path, "rb")
        except OSError as exc:
            log.warning(f"Worker: live open failed for {entry.id}: {exc}")
            return ("failed", 0.0)

        # The watermark's clock starts before the spawn so pacing counts the player's
        # own startup latency against the lead rather than banking it.
        state = LiveFeedState(started_at=time.monotonic())
        self._live = state
        try:
            proc = await asyncio.create_subprocess_exec(
                *PLAYER_COMMANDS[LIVE_PLAYER],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning(f"Worker: live player spawn failed for {entry.id}: {exc}")
            self._live = None
            with contextlib.suppress(Exception):
                src.close()
            return ("failed", 0.0)

        self._process = proc
        self._play_start = time.monotonic()
        self._play_offset = 0.0
        if self._paused_global or self._shutting_down or entry.cleared:
            # A pause, a clear or a shutdown that landed while the player was spawning.
            self._pause_requested = self._paused_global and not entry.cleared
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

        entry.epoch = uuid.uuid4().hex[:8]
        self._phase = "playing"
        feeder = asyncio.create_task(self._feed_player(entry, generation, src, proc, state))

        await self._broadcaster.send("voice_active", {
            "id": entry.id,
            "voice": entry.voice_label,
            "type": entry.entry_type,
            "text": entry.text_preview,
            "duration": None,
            "total_duration": None,
            "offset": 0.0,
            "segments": None,
            "envelope": None,
            "chunk_ms": ENVELOPE_CHUNK_MS,
            "queued": len(self._deque),
            "channel": entry.channel,
            "session": entry.session,
            "priority": entry.priority,
            "live": True,
            "epoch": entry.epoch,
        })
        if self._envelope is not None and self._envelope.generation == generation:
            await self._envelope.notify_active()

        ret = await proc.wait()
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        with contextlib.suppress(Exception):
            src.close()
        self._process = None

        offset = max(0.0, state.watermark() / LIVE_CBR_BYTES_PER_SEC - RESUME_REWIND_MS / 1000.0)
        self._live = None
        if self._envelope is not None:
            entry.stats.setdefault("decoded_ms", self._envelope.decoded_ms)
        await self._retire_envelope_pipeline()
        entry.epoch = None

        # Detachment outranks a pending pause or seek: a skipped or cleared entry that
        # took this branch second would wait out the resume and then play from cache.
        # Skip outranks clear — the user heard this one start, so it keeps skip's
        # failed:true history rather than vanishing.
        if entry.detached or entry.cleared:
            self._pause_requested = False
            self._seek_offset = None
            return ("skipped" if entry.detached else "cleared", 0.0)

        if entry.outcome == "failed":
            # Post-claim truncation: the feeder ran dry and the player drained cleanly,
            # so the exit code says success for audio that was cut short.
            log.warning(f"tts id={entry.id} live playback cut short — collection failed mid-stream")
            self._pause_requested = False
            self._seek_offset = None
            return ("truncated", 0.0)

        if self._pause_requested:
            if self._seek_offset is not None:
                offset = max(0.0, self._seek_offset)
                self._seek_offset = None
            self._pause_requested = False
            self._play_offset = offset
            if self._paused_global:
                self._phase = "paused"
                log.info(f"Worker: live paused at offset={offset:.2f}s, waiting for resume")
                await self._resume_event.wait()
            return ("resume", offset)

        if ret != 0:
            return ("failed", 0.0)
        return ("done", 0.0)

    async def _worker(self):
        while True:
            await self._has_items.wait()
            if self._shutting_down:
                return

            if self._paused_global:
                await self._resume_event.wait()

            entry = self._pick_next()
            if not entry:
                self._has_items.clear()
                continue

            self._current = entry
            self._seek_offset = None
            self._play_offset = 0.0
            duration = None
            play_offset = 0.0
            trimmed_path = None
            play_failed = False
            live_mode = False

            mode = None
            try:
                # Everything from here runs inside the try: an unhandled error in the
                # readiness wait used to kill the worker task outright, and a dead
                # worker means the daemon queues forever and plays nothing.
                mode = await self._await_playable(entry)

                if mode == "failed":
                    log.warning(f"Worker: skipping {entry.id} — TTS fetch failed")
                    play_failed = True
                elif mode == "live":
                    live_mode = True
                    result, play_offset = await self._play_live(entry)
                    if result == "resume":
                        await entry.ready.wait()
                        if entry.outcome == "complete" and not entry.cleared:
                            mode = "file"
                        else:
                            play_failed = entry.outcome == "failed"
                    else:
                        play_failed = result in ("skipped", "failed", "truncated")
                    duration = entry.final_duration

                if mode == "file":
                    play_source = entry.playback_path or entry.audio_path
                    if entry.final_duration is not None and entry.final_envelope:
                        # Already probed once when collection completed; probing again
                        # spawns a second afinfo and ffmpeg over the same file.
                        duration, envelope = entry.final_duration, entry.final_envelope
                    else:
                        duration, envelope = await asyncio.gather(
                            asyncio.to_thread(_get_audio_duration, play_source),
                            asyncio.to_thread(_extract_envelope, play_source, ENVELOPE_CHUNK_MS),
                        )
                        entry.final_duration = duration
                        entry.final_envelope = envelope
                    # The decode has to happen for playback anyway, so the truncation
                    # observability hook costs nothing here — and every entry that plays
                    # gets a decoded_ms, not only the ones live at completion time.
                    entry.stats.setdefault("decoded_ms", len(envelope) * ENVELOPE_CHUNK_MS)

                    # Cache MP3 for history replay (collector-less entries only — a
                    # streamed entry's cache file is already committed and complete).
                    cache_path = self._cache_dir / f"{entry.history_id}.mp3"
                    if entry.collector is None and not cache_path.exists():
                        try:
                            await asyncio.to_thread(shutil.copy2, play_source, str(cache_path))
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
                        if self._paused_global:
                            self._phase = "paused"
                            await self._resume_event.wait()

                        # Pre-spawn, and so also after every resume: a clear that landed
                        # while this entry was parked must not play on resume. The
                        # post-spawn check below only re-parks, so it cannot cover this.
                        if entry.cleared or entry.outcome == "cancelled" or self._shutting_down:
                            break

                        # Determine which file to play
                        if play_offset > 0:
                            trimmed_path = await self._trim_audio(play_source, play_offset)
                            play_file = trimmed_path
                        else:
                            play_file = play_source

                        # Get envelope for current play file
                        play_dur, play_env = None, envelope
                        if play_offset > 0:
                            play_dur, play_env = await asyncio.gather(
                                asyncio.to_thread(_get_audio_duration, play_file),
                                asyncio.to_thread(_extract_envelope, play_file, ENVELOPE_CHUNK_MS),
                            )
                        else:
                            play_dur = duration

                        # The claim is the boundary in BOTH modes: the spec defines
                        # post-claim as "a player is running", and a clear that reads
                        # only the live flag walks past audible file-mode playback.
                        entry.claimed_generation = entry.generation
                        self._process = await asyncio.create_subprocess_exec(
                            "afplay", play_file,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        self._play_start = time.monotonic()
                        self._play_offset = play_offset
                        self._phase = "playing"
                        self._mark_first_audio(entry)
                        if self._paused_global or self._shutting_down or entry.cleared:
                            # A pause, clear or shutdown that landed while afplay spawned.
                            self._pause_requested = self._paused_global and not entry.cleared
                            with contextlib.suppress(ProcessLookupError):
                                self._process.kill()

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
                            "chunk_ms": ENVELOPE_CHUNK_MS,
                            "queued": len(self._deque),
                            "channel": entry.channel,
                            "session": entry.session,
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
                            self._play_offset = play_offset
                            self._phase = "paused"
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
                # Only collector-less entries (replays, dialogue) own a temp file here;
                # a streamed entry's audio is the committed cache file, owned by the sweep.
                if entry.collector is None and entry.audio_path:
                    try:
                        os.unlink(entry.audio_path)
                    except OSError:
                        pass
                elif entry.playback_path and entry.playback_path == entry.attempt_path:
                    # The cache commit failed, so the attempt file became the playback
                    # path and the collector's cleanup deliberately spared it.
                    try:
                        os.unlink(entry.playback_path)
                    except OSError:
                        pass

                # A player whose spawn completed after the shutdown sweep, or one left
                # behind by an exception, has no other owner.
                if self._process is not None and self._process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.kill()

                await self._retire_envelope_pipeline()
                self._live = None
                self._phase = "idle"
                if duration is None and entry.final_duration is not None:
                    duration = entry.final_duration
                stats = entry.stats
                log.info(
                    f"tts id={entry.id} mode={'live' if live_mode else 'file'} "
                    f"model={stats.get('model', DEFAULT_MODEL)} gen={stats.get('gen', entry.generation)} "
                    f"ttfb_ms={stats.get('ttfb_ms')} first_audio_ms={stats.get('first_audio_ms')} "
                    f"decoded_ms={stats.get('decoded_ms')} "
                    f"total_ms={stats.get('total_ms')} bytes={stats.get('bytes')} "
                    f"framing={stats.get('framing', 'n/a')}"
                )

                # Cleared and cancelled entries record no history at all — unlike skip,
                # which pins failed:true for an utterance the user actually heard start.
                # A skip that a clear then overtakes still counts as a skip.
                silently_dropped = (entry.cleared or entry.outcome == "cancelled") and not entry.detached
                if not entry.is_replay and not silently_dropped:
                    history_entry = {
                        "id": entry.history_id,
                        "voice": entry.voice_label,
                        "text": entry.full_text or entry.text_preview,
                        "channel": entry.channel,
                        "session": entry.session,
                        "timestamp": entry.created_at,
                        "duration": round(duration, 3) if duration else None,
                        "type": entry.entry_type,
                        "failed": play_failed,
                    }
                    # A detached entry's audio is still being collected: announcing it
                    # now points the replay button at a cache file that does not exist
                    # yet. The collector publishes this row once its outcome lands.
                    defer = (entry.detached and entry.collector is not None
                             and entry.outcome is None)
                    self._history.append(history_entry)
                    if len(self._history) > MAX_HISTORY:
                        self._history = self._history[-MAX_HISTORY:]

                    if defer:
                        entry.history_deferred = True
                    else:
                        await self._broadcaster.send("history_update", history_entry)

                self._current = None
                self._process = None

                if not self._deque:
                    self._has_items.clear()

                await self._broadcaster.send("voice_active", {
                    "id": None, "voice": None, "type": "idle",
                    "text": None, "duration": None, "segments": None,
                    "queued": len(self._deque),
                    "channel": None, "session": None, "priority": False,
                })

    async def on_collection_complete(self, entry: QueueEntry):
        """Recompute the calibrated duration/envelope and, if this entry is live, publish it."""
        path = entry.playback_path
        if not path:
            return
        # Currency check FIRST: decoders belong to the head-of-queue entry only. Probing
        # here unconditionally spawns an afinfo and an ffmpeg for every queued, cleared
        # and detached completion, which on a deep queue is unbounded decoder pressure.
        if self._current is not entry or entry.detached or entry.cleared or not entry.epoch:
            return
        generation, epoch = entry.generation, entry.epoch
        duration, envelope = await asyncio.gather(
            asyncio.to_thread(_get_audio_duration, path),
            asyncio.to_thread(_extract_envelope, path, ENVELOPE_CHUNK_MS),
        )
        # The decode takes hundreds of milliseconds; a clear or a skip landing inside it
        # would otherwise publish a voice_update for an entry that is no longer playing.
        if (self._current is not entry or entry.detached or entry.cleared
                or entry.epoch != epoch or entry.generation != generation):
            return
        entry.final_duration = duration
        entry.final_envelope = envelope
        entry.stats["decoded_ms"] = len(envelope) * ENVELOPE_CHUNK_MS
        await self._broadcaster.send("voice_update", {
            "id": entry.id,
            "epoch": entry.epoch,
            "duration": round(duration, 3) if duration else None,
            "total_duration": round(duration, 3) if duration else None,
            "envelope": envelope,
            "chunk_ms": ENVELOPE_CHUNK_MS,
            "segments": entry.dialogue_segments if entry.entry_type == "dialogue" else None,
        })

    async def flush_deferred_history(self, entry: QueueEntry):
        """Publish a detached entry's held-back history row, once, after its audio landed."""
        if not entry.history_deferred:
            return
        entry.history_deferred = False
        record = self.find_history(entry.history_id)
        if record is not None:
            await self._broadcaster.send("history_update", record)

    def _now_playing(self) -> dict | None:
        entry = self._current
        if entry is None:
            return None
        live = self._live is not None
        pending = self._phase in ("collecting", "starting")
        elapsed = None
        if live:
            elapsed = round(self._live.watermark() / LIVE_CBR_BYTES_PER_SEC, 3)
        elif self._phase == "paused":
            # play_offset already carries the heard time; the clock must not keep ticking.
            elapsed = round(self._play_offset, 3)
        elif not pending and self._play_start:
            elapsed = round(self._play_offset + (time.monotonic() - self._play_start), 3)
        envelope_so_far, seq = (None, None)
        if live and self._envelope is not None:
            envelope_so_far, seq = self._envelope.snapshot()
        return {
            "id": entry.id,
            "live": live,
            "type": entry.entry_type,
            "phase": self._phase,
            "epoch": entry.epoch if not pending else None,
            "elapsed_estimate": elapsed if not pending else None,
            "duration": round(entry.final_duration, 3) if entry.final_duration else None,
            "total_duration": round(entry.final_duration, 3) if entry.final_duration else None,
            "envelope_so_far": envelope_so_far if not pending else None,
            "seq": seq if not pending else None,
            "chunk_ms": ENVELOPE_CHUNK_MS,
        }

    def status(self, channel: str | None = None) -> dict:
        items = []
        if self._current:
            if channel is None or self._current.channel == channel:
                items.append({
                    "position": 0, "status": "playing", "phase": self._phase,
                    "id": self._current.id,
                    "voice": self._current.voice_label,
                    "text": self._current.text_preview,
                    "channel": self._current.channel,
                    "session": self._current.session,
                    "priority": self._current.priority,
                })

        for i, entry in enumerate(self._deque):
            if channel is not None and entry.channel != channel:
                continue
            status = "queued" if entry.ready.is_set() else "pending"
            items.append({
                "position": i + 1, "status": status,
                "id": entry.id,
                "voice": entry.voice_label,
                "text": entry.text_preview,
                "channel": entry.channel,
                "session": entry.session,
                "priority": entry.priority,
            })

        return {
            "playing": self._current is not None,
            "queued": len(self._deque),
            "total": len(items),
            "items": items,
            "paused": self._paused_global,
            "channel_paused": sorted(self._paused_channels),
            "now_playing": self._now_playing(),
            "worker_stopped": self._worker_stopped,
            "model": CURRENT_MODEL,
            "streaming_enabled": STREAMING_ENABLED,
        }

    async def _cancel_entries(self, entries: list[QueueEntry]) -> int:
        """Cancel a batch concurrently under one bound.

        Cancellation is committed before the socket is touched, so a racing EOF cannot
        finalize the entry behind us. The bound matters because a collector still
        blocked in urlopen has no socket to shut down and would otherwise hold the
        HTTP handler for a full socket timeout.
        """
        if not entries:
            return 0
        for entry in entries:
            entry.cleared = True
            self.finish(entry, entry.generation, "cancelled")
            if entry.collector is not None:
                entry.collector.abort()
            else:
                if entry.fetch_task is not None and not entry.fetch_task.done():
                    entry.fetch_task.cancel()
                if entry.audio_path:
                    try:
                        os.unlink(entry.audio_path)
                    except OSError:
                        pass
        tasks = [
            e.collector.task for e in entries
            if e.collector is not None and e.collector.task is not None
            and not e.collector.task.done()
        ] + [
            e.fetch_task for e in entries
            if e.collector is None and e.fetch_task is not None and not e.fetch_task.done()
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=CANCEL_JOIN_TIMEOUT)
        for entry in entries:
            if entry.collector is not None:
                entry.collector.cleanup()
        return len(entries)

    async def _cancel_entry(self, entry: QueueEntry) -> int:
        return await self._cancel_entries([entry])

    async def _detach_current(self, cleared: bool = False):
        """Silence the entry and its SSE now; its collector runs on and caches."""
        entry = self._current
        self._freeze_live()
        if entry is not None:
            if cleared:
                entry.cleared = True
            else:
                entry.detached = True
        if self._process and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        await self._retire_envelope_pipeline()

    async def clear(self, channel: str | None = None) -> int:
        cleared = 0
        if channel is None:
            current = self._current
            audible = current is not None and (
                current.claimed_generation is not None
                or (self._process is not None and self._process.returncode is None)
            )
            # Silence what is audible FIRST: cancelling collectors can take seconds,
            # and audio must not keep playing through a clear.
            if audible:
                await self._detach_current(cleared=True)
                cleared += 1
            doomed = list(self._deque)
            self._deque.clear()
            if current is not None and not audible:
                # Never claimed, so nothing is audible and nothing ever will be — the
                # pre-spawn check in both modes reads the cleared bit.
                doomed.append(current)
            cleared += await self._cancel_entries(doomed)
            # Only if nothing arrived while we were awaiting the cancellations —
            # clearing the bit unconditionally strands an entry enqueued in that window.
            if not self._deque:
                self._has_items.clear()
        else:
            doomed = [e for e in self._deque if e.channel == channel]
            self._deque = collections.deque(e for e in self._deque if e.channel != channel)
            # Same treatment as a global clear's queued entries: without this a
            # channel-cleared streamed entry keeps its HTTP request running, commits a
            # cache file and never reaches a terminal outcome.
            cleared += await self._cancel_entries(doomed)
            if not self._deque:
                self._has_items.clear()
        return cleared

    async def skip(self) -> bool:
        if self._process and self._process.returncode is None:
            if self._live is not None:
                await self._detach_current()
            else:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            return True
        return False

    def seek(self, offset: float) -> bool:
        if not self._current or not self._process or self._process.returncode is not None:
            return False
        self._freeze_live()
        total = self._current.final_duration
        if total:
            # Seeking past the end trims to a zero-byte file, which afplay reports as a
            # failure and the history then records against a healthy utterance.
            offset = min(offset, max(0.0, total - 0.1))
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
            self._freeze_live()
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

    async def shutdown(self):
        """Silence first, then unwind: nothing audible survives past the first step."""
        self._shutting_down = True
        self._resume_event.set()
        self._has_items.set()

        self._freeze_live()
        if self._process and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        await self._retire_envelope_pipeline()

        # The registry, not the queue: a detached collector belongs to neither the deque
        # nor _current, and would otherwise outlive the daemon holding a socket open.
        collectors = list(self._collectors)
        for collector in collectors:
            collector.abort()
        pending = list(self._deque) + ([self._current] if self._current is not None else [])
        for entry in pending:
            if entry.fetch_task is not None and not entry.fetch_task.done():
                entry.fetch_task.cancel()
        tasks = [c.task for c in collectors if c.task is not None and not c.task.done()]
        tasks += [e.fetch_task for e in pending
                  if e.fetch_task is not None and not e.fetch_task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=CANCEL_JOIN_TIMEOUT)
        for collector in collectors:
            collector.cleanup()

        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker_task


def _clean_old_cache(cache_dir: Path, max_age_hours: int = 24, protected: set[str] | None = None):
    if not cache_dir.exists():
        return
    protected = protected or set()
    cutoff = time.time() - max_age_hours * 3600
    for f in cache_dir.iterdir():
        if not f.is_file() or f.stat().st_mtime >= cutoff:
            continue
        if f.stem in protected:
            # A streamed entry's cache file IS its playback path: an entry queued
            # overnight behind a pause would otherwise have its only audio swept.
            continue
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
    if len(text) > STREAM_MAX_CHARS:
        return JSONResponse({"error": f"Text too long (max {STREAM_MAX_CHARS} chars)"}, status_code=400)

    voice_raw = body.get("voice")
    if voice_raw is not None and not isinstance(voice_raw, str):
        return JSONResponse({"error": "Voice must be a string"}, status_code=400)
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        return JSONResponse({"error": "Channel must be a string"}, status_code=400)
    session = body.get("session")
    if session is not None and not isinstance(session, str):
        return JSONResponse({"error": "Session must be a string"}, status_code=400)

    vid = await resolve_voice_async(voice_raw)
    if not _api_key():
        return JSONResponse({"error": "ELEVENLABS_API_KEY not set"}, status_code=500)
    if not vid:
        return JSONResponse({"error": "No voice specified and ELEVENLABS_VOICE_ID not set"}, status_code=400)

    entry_id = uuid.uuid4().hex[:8]
    entry = QueueEntry(
        id=entry_id,
        audio_path="",
        text_preview=text[:100],
        voice_label=voice_label(vid),
        created_at=time.time(),
        channel=channel or None,
        session=session or None,
        priority=bool(body.get("priority", False)),
        full_text=text,
    )
    pos = queue.enqueue(entry)

    if STREAMING_ENABLED:
        entry.collector = StreamCollector(queue, entry, text, vid)
        entry.collector.start()
    else:
        async def _fetch_bg():
            try:
                path = await asyncio.to_thread(_fetch_tts, text, vid)
                entry.audio_path = path
                entry.playback_path = path
                queue.finish(entry, entry.generation, "complete")
            except Exception as exc:
                log.error(f"Background TTS fetch failed for {entry_id}: {exc}")
                queue.finish(entry, entry.generation, "failed")

        entry.fetch_task = asyncio.create_task(_fetch_bg())

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
    session = body.get("session")
    if session is not None and not isinstance(session, str):
        return JSONResponse({"error": "Session must be a string"}, status_code=400)
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
        audio_path="",
        text_preview=preview[:100],
        voice_label=voices_str,
        created_at=time.time(),
        entry_type="dialogue",
        dialogue_segments=segments,
        channel=channel or None,
        session=session or None,
        priority=bool(body.get("priority", False)),
        full_text=full_dialogue,
    )
    pos = queue.enqueue(entry)

    async def _fetch_bg():
        try:
            path = await asyncio.to_thread(_fetch_dialogue, inputs)
            entry.audio_path = path
            entry.playback_path = path
            queue.finish(entry, entry.generation, "complete")
        except Exception as exc:
            log.error(f"Background dialogue fetch failed for {entry_id}: {exc}")
            queue.finish(entry, entry.generation, "failed")

    entry.fetch_task = asyncio.create_task(_fetch_bg())

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
        session=entry_data.get("session"),
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


def _config_payload() -> dict:
    return {
        "model": CURRENT_MODEL,
        "available_models": list(AVAILABLE_MODELS),
        "streaming_enabled": STREAMING_ENABLED,
    }


async def handle_config_get(request: StarletteRequest) -> JSONResponse:
    return JSONResponse(_config_payload())


async def handle_config_set(request: StarletteRequest) -> JSONResponse:
    global CURRENT_MODEL
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    model = body.get("model")
    if model not in AVAILABLE_MODELS:
        return JSONResponse(
            {"error": f"model must be one of {AVAILABLE_MODELS}"}, status_code=400
        )

    if model == CONVERSATIONAL_MODEL and not STREAMING_ENABLED:
        # Accepting this would advertise a model the legacy path cannot reach.
        return JSONResponse({
            "error": (
                f"{CONVERSATIONAL_MODEL} needs the streaming engine, which is off "
                f"(SPEAK_STREAMING=0) — the legacy path synthesizes with {DEFAULT_MODEL}"
            ),
            "streaming_enabled": False,
        }, status_code=409)

    if model != CURRENT_MODEL:
        # Persist before publishing: a client that reloads on the event must not be
        # told about a model the next boot would not restore.
        try:
            _write_config(model)
        except OSError as e:
            log.warning(f"Failed to persist config.json: {e}")
            return JSONResponse({"error": "Failed to persist config"}, status_code=500)
        CURRENT_MODEL = model
        log.info(f"model set to {model}")
        broadcaster: SSEBroadcaster = request.app.state.broadcaster
        await broadcaster.send("config_updated", {"type": "config_updated", "model": model})

    return JSONResponse(_config_payload())


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


def _serialize_voices() -> list[dict]:
    out = []
    for rec in VOICE_RECORDS:
        name = rec.get("name", "")
        portraits = {
            frame: bool(name) and _portrait_path(name, frame).exists()
            for frame in PORTRAIT_FRAMES
        }
        out.append({
            "name": name,
            "id": rec.get("id", ""),
            "color": rec.get("color", ""),
            "style": rec.get("style", ""),
            "kind": rec.get("kind", "default"),
            "portraits": portraits,
            "has_portrait": portraits["default"],
        })
    return out


async def handle_voices(request: StarletteRequest) -> JSONResponse:
    return JSONResponse({"voices": _serialize_voices()})


async def _broadcast_voices_updated(request: StarletteRequest, reason: str, name: str):
    broadcaster: SSEBroadcaster = request.app.state.broadcaster
    await broadcaster.send("voices_updated", {
        "type": "voices_updated",
        "reason": reason,
        "name": name,
    })


async def handle_voices_create(request: StarletteRequest) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    name = body.get("name")
    vid = body.get("id")
    color = body.get("color", "")
    style = body.get("style", "")
    kind = body.get("kind", "default")

    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not isinstance(vid, str) or not vid.strip():
        return JSONResponse({"error": "id is required"}, status_code=400)
    if not isinstance(color, str):
        return JSONResponse({"error": "color must be a string"}, status_code=400)
    if not isinstance(style, str):
        return JSONResponse({"error": "style must be a string"}, status_code=400)
    if not isinstance(kind, str):
        return JSONResponse({"error": "kind must be a string"}, status_code=400)

    name = name.strip()
    if _find_voice_index(name) != -1:
        return JSONResponse({"error": f"Voice '{name}' already exists"}, status_code=409)

    record = {
        "name": name,
        "id": vid.strip(),
        "color": color,
        "style": style,
        "kind": kind or "default",
    }
    VOICE_RECORDS.append(record)
    try:
        await asyncio.to_thread(_save_voices)
    except Exception as e:
        VOICE_RECORDS.pop()
        return JSONResponse({"error": f"Failed to persist: {e}"}, status_code=500)
    _rebuild_voice_indexes()

    await _broadcast_voices_updated(request, "created", name)
    return JSONResponse({
        "name": name,
        "id": record["id"],
        "color": record["color"],
        "style": record["style"],
        "kind": record["kind"],
        "has_portrait": _has_portrait(name),
    }, status_code=201)


async def handle_voices_update(request: StarletteRequest) -> JSONResponse:
    name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object"}, status_code=400)

    idx = _find_voice_index(name)
    if idx == -1:
        return JSONResponse({"error": f"Voice '{name}' not found"}, status_code=404)

    record = dict(VOICE_RECORDS[idx])
    old_name = record["name"]
    new_name = old_name

    if "name" in body:
        nn = body["name"]
        if not isinstance(nn, str) or not nn.strip():
            return JSONResponse({"error": "name must be a non-empty string"}, status_code=400)
        nn = nn.strip()
        if nn.lower() != old_name.lower():
            conflict = _find_voice_index(nn)
            if conflict != -1 and conflict != idx:
                return JSONResponse({"error": f"Voice '{nn}' already exists"}, status_code=409)
        new_name = nn
        record["name"] = nn

    for field_name in ("id", "color", "style", "kind"):
        if field_name in body:
            val = body[field_name]
            if not isinstance(val, str):
                return JSONResponse({"error": f"{field_name} must be a string"}, status_code=400)
            record[field_name] = val

    VOICE_RECORDS[idx] = record
    try:
        await asyncio.to_thread(_save_voices)
    except Exception as e:
        return JSONResponse({"error": f"Failed to persist: {e}"}, status_code=500)

    if new_name.lower() != old_name.lower():
        _rename_portraits(old_name, new_name)
    _rebuild_voice_indexes()

    await _broadcast_voices_updated(request, "updated", new_name)
    return JSONResponse({
        "name": record["name"],
        "id": record.get("id", ""),
        "color": record.get("color", ""),
        "style": record.get("style", ""),
        "kind": record.get("kind", "default"),
        "has_portrait": _has_portrait(new_name),
    })


async def handle_voices_delete(request: StarletteRequest) -> JSONResponse:
    name = request.path_params["name"]
    idx = _find_voice_index(name)
    if idx == -1:
        return JSONResponse({"error": f"Voice '{name}' not found"}, status_code=404)

    record = VOICE_RECORDS.pop(idx)
    actual_name = record.get("name", name)
    try:
        await asyncio.to_thread(_save_voices)
    except Exception as e:
        VOICE_RECORDS.insert(idx, record)
        return JSONResponse({"error": f"Failed to persist: {e}"}, status_code=500)

    _delete_portraits(actual_name)
    _rebuild_voice_indexes()

    await _broadcast_voices_updated(request, "deleted", actual_name)
    return JSONResponse(None, status_code=204)


async def handle_portrait_upload(request: StarletteRequest) -> JSONResponse:
    name = request.path_params["name"]
    frame = request.query_params.get("frame", "default")
    if frame not in PORTRAIT_FRAMES:
        return JSONResponse({"error": f"frame must be one of: {sorted(PORTRAIT_FRAMES)}"}, status_code=400)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "image/png":
        return JSONResponse({"error": "Content-Type must be image/png"}, status_code=400)

    data = await request.body()
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return JSONResponse({"error": "Body is not a valid PNG"}, status_code=400)

    PORTRAITS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _portrait_path(name, frame)

    fd, tmp = tempfile.mkstemp(prefix=".portrait-", suffix=".png", dir=str(PORTRAITS_DIR))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return JSONResponse({"error": f"Failed to save portrait: {e}"}, status_code=500)

    await _broadcast_voices_updated(request, "portrait", name)
    return JSONResponse({
        "name": name,
        "frame": frame,
        "path": str(dest.relative_to(REPO_ROOT)),
        "bytes": len(data),
    })


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

def _build_app(queue: "AudioQueue", broadcaster: SSEBroadcaster, lifespan=None) -> Starlette:
    """Routes + middleware, so a test can drive the real app without a server."""
    app = Starlette(lifespan=lifespan, middleware=[Middleware(LocalhostGuardMiddleware)], routes=[
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
        Route("/voices", handle_voices_create, methods=["POST"]),
        Route("/voices/{name}", handle_voices_update, methods=["PATCH"]),
        Route("/voices/{name}", handle_voices_delete, methods=["DELETE"]),
        Route("/config", handle_config_get, methods=["GET"]),
        Route("/config", handle_config_set, methods=["POST"]),
        Route("/health", handle_health, methods=["GET"]),
        Route("/", handle_index, methods=["GET"]),
        Route("/portraits/{name}", handle_portrait_upload, methods=["POST"]),
        Route("/portraits/{name:path}", handle_portrait, methods=["GET"]),
    ])
    app.state.queue = queue
    app.state.broadcaster = broadcaster
    return app


async def main():
    # uvicorn only configures its own loggers; without this the per-entry tts line is invisible.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _clean_old_cache(CACHE_DIR)

    async def _periodic_cache_cleanup():
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(_clean_old_cache, CACHE_DIR, 24, queue.live_history_ids())
            except Exception as e:
                log.warning(f"Cache cleanup error: {e}")

    broadcaster = SSEBroadcaster()
    queue = AudioQueue(broadcaster)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        global LIVE_PLAYER
        _validate_model_config()
        LIVE_PLAYER = await asyncio.to_thread(_select_live_player)
        queue.start()
        cleanup_task = asyncio.create_task(_periodic_cache_cleanup())
        try:
            yield
        finally:
            await queue.shutdown()
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cleanup_task
            if _collector_executor is not None:
                # Non-daemon threads: atexit would otherwise join them with no timeout,
                # after the loop that could have interrupted them is already gone.
                _collector_executor.shutdown(wait=False, cancel_futures=True)

    app = _build_app(queue, broadcaster, lifespan=lifespan)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=DASHBOARD_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

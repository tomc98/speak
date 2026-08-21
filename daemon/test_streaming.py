# /// script
# requires-python = ">=3.12"
# dependencies = ["starlette", "uvicorn"]
# ///
"""Streaming-collector tests for the voice daemon.

Every test drives a local fake HTTP server over raw sockets so the three transfer
framings can be produced exactly. No request ever reaches ElevenLabs.

    uv run daemon/test_streaming.py
"""

import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent
REPO_ROOT = DAEMON_DIR.parent

os.environ["SPEAK_CACHE_DIR"] = tempfile.mkdtemp(prefix="speak-test-cache-")
os.environ.setdefault("ELEVENLABS_API_KEY", "test-key")

sys.path.insert(0, str(DAEMON_DIR))
import server  # noqa: E402

FIXTURE = (REPO_ROOT / "assets" / "probe.mp3").read_bytes()


def _load_speak_script():
    spec = importlib.util.spec_from_file_location("speak_fallback", REPO_ROOT / "scripts" / "speak.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Fake ElevenLabs stream server ---------------------------------------------------


class FakeStreamServer:
    """Serves one scripted response per connection, with byte-level framing control."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"
        self.requests: list[dict] = []
        self.handler = None
        self.release = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._running = False
        self.release.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            request = self._read_request(conn)
            if request is None:
                return
            self.requests.append(request)
            if self.handler:
                self.handler(self, conn, request)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(conn) -> dict | None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        body = rest
        while len(body) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        parsed = {}
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
        return {"method": method, "path": path, "headers": headers, "body": parsed}


def _send(conn, data: bytes):
    try:
        conn.sendall(data)
    except OSError:
        pass


def content_length(body: bytes, declared: int | None = None, first_chunk_delay: float = 0.0):
    """Content-Length framing. declared > len(body) is the silent-truncation case."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % (declared if declared is not None else len(body)))
        if first_chunk_delay:
            time.sleep(first_chunk_delay)
        _send(conn, body)
    return handler


def chunked(body: bytes, terminate: bool = True):
    """Chunked framing. terminate=False omits the 0-chunk — an IncompleteRead."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n")
        for i in range(0, len(body), 512):
            piece = body[i:i + 512]
            _send(conn, b"%x\r\n%s\r\n" % (len(piece), piece))
        if terminate:
            _send(conn, b"0\r\n\r\n")
    return handler


def close_delimited(body: bytes):
    """Neither Content-Length nor chunked: EOF is the only terminator."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n")
        _send(conn, body)
    return handler


def stalling(prefix: bytes, stall_seconds: float = 30.0):
    """Sends a prefix, then holds the connection open without sending anything."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % (len(prefix) * 4))
        _send(conn, prefix)
        srv.release.wait(timeout=stall_seconds)
    return handler


def gated(prefix: bytes, tail: bytes, declared: int):
    """Sends a prefix, waits for release, then sends the tail and closes."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % declared)
        _send(conn, prefix)
        srv.release.wait(timeout=30)
        _send(conn, tail)
    return handler


def trickle(body: bytes, slice_size: int = 4096, delay: float = 0.12):
    """Correct Content-Length, delivered slowly enough that playback starts mid-collection."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body))
        for i in range(0, len(body), slice_size):
            _send(conn, body[i:i + slice_size])
            time.sleep(delay)
    return handler


def status(code: int, message: bytes = b"nope"):
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 %d Error\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n%s" % (code, len(message), message))
    return handler


# --- Harness -------------------------------------------------------------------------


class CollectorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.srv = FakeStreamServer()
        self.addCleanup(self.srv.close)
        self.cache = Path(tempfile.mkdtemp(prefix="speak-test-cache-"))
        self._patch("API_BASE", self.srv.base)
        self._patch("CACHE_DIR", self.cache)
        self.queue = server.AudioQueue(server.SSEBroadcaster())

    def _patch(self, name: str, value):
        original = getattr(server, name)
        setattr(server, name, value)
        self.addCleanup(setattr, server, name, original)

    def _entry(self) -> server.QueueEntry:
        return server.QueueEntry(
            id="test1234", audio_path="", text_preview="hi",
            voice_label="Adam", created_at=time.time(), full_text="hello there",
        )

    async def _collect(self, entry=None, text="hello there", voice="voice-abc", timeout=20.0):
        entry = entry or self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, text, voice)
        entry.collector.start()
        await asyncio.wait_for(entry.ready.wait(), timeout=timeout)
        await asyncio.wait_for(asyncio.shield(entry.collector.task), timeout=timeout)
        return entry


# --- Transport completeness (all three framings) -------------------------------------


class FramingTests(CollectorTestCase):
    async def test_content_length_short_body_is_caught(self):
        """The silent case: http.client does not raise, so the collector checks resp.length."""
        self._patch("MAX_ATTEMPTS", 1)
        self.srv.handler = content_length(FIXTURE[:1024], declared=len(FIXTURE))
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        self.assertTrue(entry.fetch_failed)
        self.assertIsNone(entry.playback_path)
        self.assertFalse((self.cache / f"{entry.history_id}.mp3").exists())

    async def test_chunked_truncation_raises(self):
        self._patch("MAX_ATTEMPTS", 1)
        self.srv.handler = chunked(FIXTURE, terminate=False)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")

    async def test_chunked_complete_succeeds(self):
        self.srv.handler = chunked(FIXTURE, terminate=True)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(entry.stats["framing"], "chunked")

    async def test_clean_short_close_is_undetectable(self):
        """Documented residual: a close-delimited early close reads as success."""
        self._patch("MAX_ATTEMPTS", 1)
        self.srv.handler = close_delimited(FIXTURE[:1024])
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(entry.stats["framing"], "close")
        self.assertEqual(entry.stats["bytes"], 1024)

    async def test_framing_recorded_for_content_length(self):
        self.srv.handler = content_length(FIXTURE)
        entry = await self._collect()
        self.assertEqual(entry.stats["framing"], "content-length")


# --- Timing: slow start, stall, deadline supervisor ----------------------------------


class TimingTests(CollectorTestCase):
    async def test_slow_start_succeeds(self):
        self.srv.handler = content_length(FIXTURE, first_chunk_delay=1.0)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertGreaterEqual(entry.stats["ttfb_ms"], 900)

    async def test_stall_past_socket_timeout_fails(self):
        self._patch("MAX_ATTEMPTS", 1)
        self._patch("SOCKET_TIMEOUT", 1)
        self.srv.handler = stalling(FIXTURE[:512])
        started = time.monotonic()
        entry = await self._collect(timeout=15)
        self.assertEqual(entry.outcome, "failed")
        self.assertLess(time.monotonic() - started, 10)

    async def test_deadline_supervisor_fires_mid_read(self):
        """The supervisor shuts the socket down while a read is in flight."""
        self._patch("SOCKET_TIMEOUT", 30)
        self._patch("ENTRY_DEADLINE", 1)
        self.srv.handler = stalling(FIXTURE[:512])
        started = time.monotonic()
        entry = await self._collect(timeout=15)
        elapsed = time.monotonic() - started
        self.assertEqual(entry.outcome, "failed")
        self.assertLess(elapsed, 10, "deadline must not wait out the socket timeout")


# --- Cancellation --------------------------------------------------------------------


class CancellationTests(CollectorTestCase):
    async def test_clear_cancels_mid_stream_and_deletes_partials(self):
        self._patch("SOCKET_TIMEOUT", 30)
        self.srv.handler = stalling(FIXTURE[:512])
        entry = self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, "hello", "voice-abc")
        entry.collector.start()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if entry.attempt_path and os.path.exists(entry.attempt_path) \
                    and os.path.getsize(entry.attempt_path) > 0:
                break
        attempt_path = entry.attempt_path
        started = time.monotonic()
        await asyncio.wait_for(self.queue._cancel_entry(entry), timeout=10)
        self.assertEqual(entry.outcome, "cancelled")
        self.assertFalse(entry.fetch_failed, "cancelled entries are not failures")
        self.assertTrue(entry.ready.is_set(), "ready is terminal for every outcome")
        self.assertFalse(os.path.exists(attempt_path))
        self.assertLess(time.monotonic() - started, 8, "socket shutdown must unblock the read")

    async def test_cancel_wins_over_a_racing_completion(self):
        entry = self._entry()
        self.queue.finish(entry, entry.generation, "cancelled")
        self.assertFalse(self.queue.finish(entry, entry.generation, "complete"))
        self.assertEqual(entry.outcome, "cancelled")

    async def test_finish_ignores_a_stale_generation(self):
        entry = self._entry()
        entry.generation = 3
        self.assertFalse(self.queue.finish(entry, 2, "complete"))
        self.assertIsNone(entry.outcome)


# --- Budget, hop allocation, routing -------------------------------------------------


class BudgetTests(CollectorTestCase):
    async def test_rejection_advances_the_hop_each_time(self):
        self.srv.handler = status(422)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        paths = [r["path"].split("?")[0] for r in self.srv.requests]
        self.assertEqual(len(paths), 3, "3-attempt entry budget")
        self.assertEqual(paths[0], "/v1/text-to-speech/voice-abc/stream")
        self.assertEqual(paths[1], "/v1/text-to-speech/voice-abc")
        self.assertEqual(paths[2], "/v1/text-to-speech/voice-abc", "last hop is always legacy")

    async def test_transport_failure_retries_the_same_hop_once(self):
        self.srv.handler = content_length(FIXTURE[:256], declared=len(FIXTURE))
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        paths = [r["path"].split("?")[0] for r in self.srv.requests]
        self.assertEqual(paths, [
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc",
        ])

    async def test_conversational_hop_uses_the_dialogue_stream_route(self):
        self._patch("SPEAK_MODEL", server.CONVERSATIONAL_MODEL)
        self.srv.handler = status(422)
        await self._collect(text="short line")
        first = self.srv.requests[0]
        self.assertEqual(first["path"].split("?")[0], "/v1/text-to-dialogue/stream")
        self.assertEqual(first["body"]["model_id"], server.CONVERSATIONAL_MODEL)
        self.assertEqual(len(first["body"]["inputs"]), 1)
        self.assertEqual(first["body"]["inputs"][0]["voice_id"], "voice-abc")

    async def test_success_on_the_second_hop_stops_the_budget(self):
        calls = {"n": 0}
        reject, ok = status(422), content_length(FIXTURE)

        def handler(srv, conn, request):
            calls["n"] += 1
            (reject if calls["n"] == 1 else ok)(srv, conn, request)

        self.srv.handler = handler
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(len(self.srv.requests), 2)


class RouterTests(unittest.TestCase):
    def test_default_model_routes_to_the_v3_stream_endpoint(self):
        self.assertEqual(server._hop_chain("hi")[0], ("v3_stream", server.DEFAULT_MODEL))

    def test_conversational_tier_applies_under_the_char_cap(self):
        original = server.SPEAK_MODEL
        server.SPEAK_MODEL = server.CONVERSATIONAL_MODEL
        try:
            self.assertEqual(server._hop_chain("x" * 2000)[0],
                             ("conversational", server.CONVERSATIONAL_MODEL))
            self.assertEqual(server._hop_chain("x" * 2001)[0],
                             ("v3_stream", server.DEFAULT_MODEL))
        finally:
            server.SPEAK_MODEL = original

    def test_last_hop_is_always_legacy(self):
        for text in ("hi", "x" * 4999):
            self.assertEqual(server._hop_chain(text)[-1][0], "legacy")


class EnqueueLimitTests(unittest.IsolatedAsyncioTestCase):
    class _Request:
        def __init__(self, body, queue):
            self._body = body
            self.app = type("App", (), {"state": type("State", (), {"queue": queue})()})()

        async def json(self):
            return self._body

    async def test_text_over_five_thousand_chars_is_rejected_at_enqueue(self):
        queue = server.AudioQueue(server.SSEBroadcaster())
        request = self._Request({"text": "x" * 5001}, queue)
        response = await server.handle_speak(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(queue._deque), 0)


# --- Claim boundary and worker wake revalidation -------------------------------------


class ClaimTests(CollectorTestCase):
    async def test_no_refetch_after_a_claim(self):
        self.srv.handler = gated(FIXTURE[:512], b"", declared=len(FIXTURE))
        entry = self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, "hello", "voice-abc")
        entry.collector.start()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if entry.attempt_path and os.path.exists(entry.attempt_path) \
                    and os.path.getsize(entry.attempt_path) > 0:
                break
        entry.claimed_generation = entry.generation
        self.srv.release.set()
        await asyncio.wait_for(entry.ready.wait(), timeout=15)
        await asyncio.wait_for(asyncio.shield(entry.collector.task), timeout=15)
        self.assertEqual(entry.outcome, "failed")
        self.assertEqual(len(self.srv.requests), 1, "post-claim, nothing re-synthesizes")


class WorkerWaitTests(CollectorTestCase):
    async def test_stale_generation_wake_does_not_start_playback(self):
        self._patch("LIVE_PLAYER", "ffplay")
        entry = self._entry()
        entry.generation = 2
        entry.started_generation = 1  # pre-roll from a retired attempt
        waiter = asyncio.create_task(self.queue._await_playable(entry))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=0.4)

        entry.started_generation = 2
        entry.wake.set()
        self.assertEqual(await asyncio.wait_for(waiter, timeout=5), "live")

    async def test_pause_gate_holds_a_ready_entry(self):
        """The live bug: a pause landing during synthesis used to find no process to kill."""
        entry = self._entry()
        entry.playback_path = "/dev/null"
        self.queue.pause()
        self.queue.finish(entry, entry.generation, "complete")
        waiter = asyncio.create_task(self.queue._await_playable(entry))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=0.4)

        self.queue.resume()
        self.assertEqual(await asyncio.wait_for(waiter, timeout=5), "file")

    async def test_failed_and_cancelled_outcomes_branch_apart(self):
        failed = self._entry()
        self.queue.finish(failed, failed.generation, "failed")
        self.assertEqual(await asyncio.wait_for(self.queue._await_playable(failed), timeout=5), "failed")

        cancelled = self._entry()
        self.queue.finish(cancelled, cancelled.generation, "cancelled")
        self.assertEqual(await asyncio.wait_for(self.queue._await_playable(cancelled), timeout=5),
                         "cancelled")


# --- Cache commit --------------------------------------------------------------------


class CacheCommitTests(CollectorTestCase):
    async def test_commit_publishes_playback_path_before_the_outcome(self):
        self.srv.handler = content_length(FIXTURE)
        entry = await self._collect()
        cache_file = self.cache / f"{entry.history_id}.mp3"
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(entry.playback_path, str(cache_file))
        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.read_bytes(), FIXTURE)
        self.assertEqual(entry.stats["bytes"], len(FIXTURE))
        self.assertEqual(entry.stats["model"], server.DEFAULT_MODEL)

    async def test_collector_deletes_its_own_attempt_files(self):
        self.srv.handler = content_length(FIXTURE)
        entry = await self._collect()
        self.assertFalse(os.path.exists(entry.attempt_path))

    async def test_observability_fields_are_populated(self):
        self.srv.handler = content_length(FIXTURE)
        entry = await self._collect()
        for key in ("model", "gen", "ttfb_ms", "bytes", "framing", "total_ms", "decoded_ms"):
            self.assertIn(key, entry.stats, f"missing {key} in the tts log line")
        self.assertGreater(entry.stats["decoded_ms"], 0)


# --- Player backend probe ------------------------------------------------------------


class PlayerProbeTests(unittest.TestCase):
    def test_fixture_probe_passes_both_backends(self):
        """Empty-input probing false-passes ffplay and false-fails audiotoolbox."""
        self.assertTrue(server.PROBE_FIXTURE.exists())
        for backend in ("ffplay", "audiotoolbox"):
            with self.subTest(backend=backend):
                self.assertTrue(server._probe_player(backend))

    def test_unknown_backend_selects_nothing(self):
        original = server.LIVE_PLAYER_PREF
        server.LIVE_PLAYER_PREF = "not-a-player"
        try:
            self.assertIsNone(server._select_live_player())
        finally:
            server.LIVE_PLAYER_PREF = original


# --- Live playback pipeline (real ffplay + ffmpeg against the fake server) ------------


class RecordingBroadcaster(server.SSEBroadcaster):
    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    async def send(self, event: str, data: dict):
        self.events.append((event, data))
        await super().send(event, data)

    def of(self, name: str) -> list[dict]:
        return [data for event, data in self.events if event == name]


def _silent_mp3(seconds: float) -> bytes:
    import subprocess
    out = subprocess.run(
        [server.FFMPEG, "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono", "-t", str(seconds),
         "-b:a", "128k", "-f", "mp3", "-"],
        capture_output=True, timeout=30,
    )
    return out.stdout


class LivePlaybackTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        if server._probe_player("ffplay"):
            self._patch("LIVE_PLAYER", "ffplay")
        elif server._probe_player("audiotoolbox"):
            self._patch("LIVE_PLAYER", "audiotoolbox")
        else:
            self.skipTest("no live player backend available")
        self._patch("PREROLL_MS", 300)
        self.audio = _silent_mp3(3.0)
        self.assertGreater(len(self.audio), 40000)
        self.events = RecordingBroadcaster()
        self.queue = server.AudioQueue(self.events)

    async def _run_queue(self, entry, timeout=45.0):
        entry.collector = server.StreamCollector(self.queue, entry, "hello there", "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        self.queue.start()
        self.addCleanup(lambda: None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._history and self.queue._current is None:
                return
        self.fail("entry never finished")

    async def asyncTearDown(self):
        await self.queue.shutdown()

    async def test_live_playback_publishes_epoch_envelope_and_update(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._run_queue(entry)

        live = [e for e in self.events.of("voice_active") if e.get("live")]
        self.assertEqual(len(live), 1, "exactly one live voice_active")
        epoch = live[0]["epoch"]
        self.assertTrue(epoch)
        self.assertIsNone(live[0]["duration"], "totals are unknown during live playback")
        self.assertIsNone(live[0]["envelope"])

        appends = self.events.of("envelope_append")
        self.assertTrue(appends, "live playback must stream lip-sync appends")
        self.assertEqual(appends[0]["seq"], 0, "seq is absolute and starts at 0 per epoch")
        self.assertTrue(all(a["epoch"] == epoch for a in appends))
        expected_seq = 0
        for append in appends:
            self.assertEqual(append["seq"], expected_seq)
            expected_seq += len(append["values"])

        updates = self.events.of("voice_update")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["epoch"], epoch)
        self.assertAlmostEqual(updates[0]["duration"], 3.0, delta=0.4)
        self.assertGreater(len(updates[0]["envelope"]), 40)

        self.assertFalse(self.queue._history[0]["failed"])
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists())
        self.assertEqual(entry.stats["framing"], "content-length")

    async def test_file_mode_voice_active_carries_no_live_fields(self):
        self.srv.handler = content_length(self.audio)
        entry = self._entry()
        await self._run_queue(entry)
        active = [e for e in self.events.of("voice_active") if e.get("id")]
        self.assertEqual(len(active), 1)
        self.assertNotIn("live", active[0], "file-mode payload is byte-identical to today")
        self.assertNotIn("epoch", active[0])
        self.assertEqual(self.events.of("envelope_append"), [])

    async def _wait_for_live(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._live is not None:
                return
        self.fail("live playback never started")

    async def test_pause_mid_live_resumes_from_the_watermark_in_file_mode(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, "hello there", "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        self.queue.start()
        await self._wait_for_live()

        await asyncio.sleep(0.6)
        self.queue.pause()
        self.assertIsNotNone(self.queue._live.frozen, "the watermark freezes at control time")
        frozen = self.queue._live.frozen

        await asyncio.sleep(0.5)
        self.assertIsNone(self.queue._process, "pause is instant silence in live mode")

        # The collector runs on while paused: the entry renders and caches, silently.
        await asyncio.wait_for(entry.ready.wait(), timeout=30)
        self.assertEqual(entry.outcome, "complete")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists())
        self.assertEqual(
            [e for e in self.events.of("voice_active") if e.get("id") and not e.get("live")], [],
            "a paused daemon plays nothing",
        )
        self.queue.resume()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._history:
                break
        else:
            self.fail("resume never completed")

        resumed = [e for e in self.events.of("voice_active")
                   if e.get("id") and not e.get("live")]
        self.assertEqual(len(resumed), 1, "resume plays file mode under a fresh spawn")
        expected = max(0.0, frozen / server.LIVE_CBR_BYTES_PER_SEC
                       - server.RESUME_REWIND_MS / 1000.0)
        self.assertAlmostEqual(resumed[0]["offset"], expected, delta=0.05)
        self.assertLessEqual(resumed[0]["offset"], frozen / server.LIVE_CBR_BYTES_PER_SEC,
                             "the failure direction is replay, never skip")
        self.assertFalse(self.queue._history[0]["failed"])

    async def test_clear_before_the_claim_stops_the_entry_from_ever_playing(self):
        self.srv.handler = trickle(self.audio, slice_size=1024, delay=0.5)
        entry = self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, "hello there", "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        self.queue.start()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            if self.queue._current is entry:
                break
        self.assertIsNone(entry.claimed_generation, "the entry must still be pre-claim")

        attempt_path = entry.attempt_path
        self.assertEqual(await self.queue.clear(), 1)
        self.assertEqual(entry.outcome, "cancelled")
        self.assertFalse(os.path.exists(attempt_path), "partials are deleted, not orphaned")

        await asyncio.sleep(0.5)
        self.assertEqual([e for e in self.events.of("voice_active") if e.get("id")], [],
                         "a cleared pre-claim entry never reaches a player")
        self.assertEqual(self.queue._history, [], "cleared entries record no history")

    async def test_collector_less_entry_keeps_todays_file_mode_contract(self):
        """Dialogue and replay regression: segment timing, temp-file ownership, no epoch."""
        fd, path = tempfile.mkstemp(prefix=server.TEMP_PREFIX, suffix=".mp3")
        os.close(fd)
        Path(path).write_bytes(self.audio)
        entry = server.QueueEntry(
            id="dlg12345", audio_path=path, text_preview="A: hi / B: yo",
            voice_label="Adam + Ellie", created_at=time.time(), entry_type="dialogue",
            dialogue_segments=[{"voice": "Adam", "text": "hi", "chars": 2},
                               {"voice": "Ellie", "text": "yo", "chars": 2}],
            full_text="A: hi / B: yo",
        )
        self.assertEqual(entry.outcome, "complete", "a pre-fetched entry is born complete")
        self.assertEqual(entry.playback_path, path)

        self.queue.enqueue(entry)
        self.queue.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._history:
                break
        else:
            self.fail("dialogue entry never finished")

        record = self.queue._history[0]
        self.assertEqual(record["type"], "dialogue")
        self.assertFalse(record["failed"])
        self.assertAlmostEqual(entry.dialogue_segments[0]["end"], 1.5, delta=0.3)
        self.assertFalse(os.path.exists(path), "the worker unlinks collector-less temp files")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists(),
                        "collector-less entries still cache at play time")
        active = [e for e in self.events.of("voice_active") if e.get("id")]
        self.assertNotIn("epoch", active[0])
        self.assertEqual(active[0]["segments"], entry.dialogue_segments)

    async def test_skip_mid_live_advances_and_silences_the_entry(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        entry.collector = server.StreamCollector(self.queue, entry, "hello there", "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        self.queue.start()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._live is not None:
                break
        else:
            self.fail("live playback never started")

        self.assertTrue(await self.queue.skip())
        skipped_at = len(self.events.events)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.queue._history:
                break
        else:
            self.fail("skip did not advance the worker")

        self.assertTrue(self.queue._history[0]["failed"], "skip records failed:true, as today")
        after = self.events.events[skipped_at:]
        self.assertEqual([e for e, _ in after if e in ("envelope_append", "voice_update")], [],
                         "a detached entry emits no further SSE")

        await asyncio.wait_for(asyncio.shield(entry.collector.task), timeout=30)
        self.assertEqual(entry.outcome, "complete", "the detached collector runs to completion")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists())


# --- Daemon-down fallback (RO-6) -----------------------------------------------------


class FallbackRouteTests(unittest.TestCase):
    def test_speak_py_fallback_route_and_model_from_the_request_log(self):
        """Asserts route + model, not that some audio played — macOS say would false-pass."""
        srv = FakeStreamServer()
        self.addCleanup(srv.close)
        srv.handler = content_length(FIXTURE)

        speak = _load_speak_script()
        speak.API_BASE = srv.base

        played = []

        class _Subprocess:
            DEVNULL = -3

            @staticmethod
            def run(cmd, **kwargs):
                played.append(cmd)

            @staticmethod
            def Popen(cmd, **kwargs):
                played.append(cmd)

        speak.subprocess = _Subprocess

        ok = speak.speak_elevenlabs("hello", "test-key", "voice-abc", sync=True)
        self.assertTrue(ok)
        self.assertEqual(len(srv.requests), 1)
        request = srv.requests[0]
        self.assertEqual(request["path"], "/v1/text-to-speech/voice-abc")
        self.assertEqual(request["body"]["model_id"], "eleven_v3")
        self.assertEqual(played[0][0], "afplay", "fallback must not degrade to macOS say")


if __name__ == "__main__":
    unittest.main(verbosity=2)

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
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
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


def json_two_hundred(body: bytes = b'{"detail":"quota exceeded"}'):
    """A 200 whose body is not audio — the vendor documents no error codes on stream."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    return handler


def dribbled(body: bytes, first: int = 2):
    """Sends a legal sub-4-byte first slice, then the rest."""
    def handler(srv, conn, request):
        _send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body))
        _send(conn, body[:first])
        time.sleep(0.15)
        _send(conn, body[first:])
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
        # Primed so voice resolution never issues a /voices lookup of its own.
        self._patch("_api_voices_cache", {})
        self.queue = server.AudioQueue(server.SSEBroadcaster())

    def _patch(self, name: str, value):
        self._patch_attr(server, name, value)

    def _patch_attr(self, target, name: str, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, original)

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
        self._patch("_attempt_budget", lambda chain: 1)
        self.srv.handler = content_length(FIXTURE[:1024], declared=len(FIXTURE))
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        self.assertTrue(entry.fetch_failed)
        self.assertIsNone(entry.playback_path)
        self.assertFalse((self.cache / f"{entry.history_id}.mp3").exists())

    async def test_chunked_truncation_raises(self):
        self._patch("_attempt_budget", lambda chain: 1)
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
        self._patch("_attempt_budget", lambda chain: 1)
        self.srv.handler = close_delimited(FIXTURE[:1024])
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(entry.stats["framing"], "close")
        self.assertEqual(entry.stats["bytes"], 1024)

    async def test_a_two_hundred_carrying_json_is_a_rejection(self):
        """Neither stream endpoint documents error codes; a 200 can still be an error."""
        self.srv.handler = json_two_hundred()
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        paths = [r["path"].split("?")[0] for r in self.srv.requests]
        self.assertEqual(paths, [
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc",
            "/v1/text-to-speech/voice-abc",
        ], "a rejection advances the hop rather than retrying it")
        self.assertFalse((self.cache / f"{entry.history_id}.mp3").exists())

    async def test_a_sub_four_byte_first_slice_is_not_a_rejection(self):
        """read1 may legally return 1-3 bytes; judging the format there burns the hop."""
        self.srv.handler = dribbled(FIXTURE, first=2)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(len(self.srv.requests), 1, "a healthy stream must not cost a hop")
        self.assertEqual((self.cache / f"{entry.history_id}.mp3").read_bytes(), FIXTURE)

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
        self._patch("_attempt_budget", lambda chain: 1)
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

    async def test_stage_two_transport_failure_still_reaches_the_v3_stream_hop(self):
        """A same-hop retry must not cost the middle hop the fallback exists for."""
        self._patch("CURRENT_MODEL", server.CONVERSATIONAL_MODEL)
        self.srv.handler = content_length(FIXTURE[:256], declared=len(FIXTURE))
        entry = await self._collect(text="short line")
        self.assertEqual(entry.outcome, "failed")
        paths = [r["path"].split("?")[0] for r in self.srv.requests]
        self.assertEqual(paths, [
            "/v1/text-to-dialogue/stream",
            "/v1/text-to-dialogue/stream",
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc",
        ])

    def test_budget_is_one_slot_per_hop_plus_one(self):
        self.assertEqual(server._attempt_budget([("a", "m"), ("b", "m")]), 3)
        self.assertEqual(server._attempt_budget([("a", "m"), ("b", "m"), ("c", "m")]), 4)

    async def test_five_hundred_is_transport_not_rejection(self):
        """A 5xx must retry the same hop, not burn it as a model rejection."""
        self.srv.handler = status(503)
        entry = await self._collect()
        self.assertEqual(entry.outcome, "failed")
        paths = [r["path"].split("?")[0] for r in self.srv.requests]
        self.assertEqual(paths, [
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc/stream",
            "/v1/text-to-speech/voice-abc",
        ], "5xx retries the hop; a 422 would have advanced it")

    async def test_conversational_hop_uses_the_dialogue_stream_route(self):
        self._patch("CURRENT_MODEL", server.CONVERSATIONAL_MODEL)
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
        original = server.CURRENT_MODEL
        server.CURRENT_MODEL = server.CONVERSATIONAL_MODEL
        try:
            self.assertEqual(server._hop_chain("x" * 2000)[0],
                             ("conversational", server.CONVERSATIONAL_MODEL))
            self.assertEqual(server._hop_chain("x" * 2001)[0],
                             ("v3_stream", server.DEFAULT_MODEL))
        finally:
            server.CURRENT_MODEL = original

    def test_last_hop_is_always_legacy(self):
        for text in ("hi", "x" * 4999):
            self.assertEqual(server._hop_chain(text)[-1][0], "legacy")


class FakeConfigRequest:
    """Minimal stand-in for the two attributes handle_config_set touches."""

    def __init__(self, body, broadcaster):
        self._body = body
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(broadcaster=broadcaster))

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    """The model is a runtime setting both UIs can flip."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="speak-test-config-"))
        self.path = self.dir / "config.json"
        self._patch("CONFIG_PATH", self.path)
        self._patch("CURRENT_MODEL", server.DEFAULT_MODEL)
        self.broadcaster = RecordingBroadcaster()

    def _patch(self, name: str, value):
        original = getattr(server, name)
        setattr(server, name, value)
        self.addCleanup(setattr, server, name, original)

    async def _post(self, body):
        return await server.handle_config_set(FakeConfigRequest(body, self.broadcaster))

    def _json(self, response) -> dict:
        return json.loads(response.body)

    async def test_get_reports_the_current_model_and_the_available_set(self):
        payload = self._json(await server.handle_config_get(None))
        self.assertEqual(payload["model"], server.DEFAULT_MODEL)
        self.assertEqual(payload["available_models"],
                         [server.DEFAULT_MODEL, server.CONVERSATIONAL_MODEL])

    async def test_post_sets_persists_and_broadcasts(self):
        response = await self._post({"model": server.CONVERSATIONAL_MODEL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._json(response)["model"], server.CONVERSATIONAL_MODEL)
        self.assertEqual(server.CURRENT_MODEL, server.CONVERSATIONAL_MODEL)
        self.assertEqual(json.loads(self.path.read_text()), {"model": server.CONVERSATIONAL_MODEL})
        self.assertEqual(self.broadcaster.of("config_updated"),
                         [{"type": "config_updated", "model": server.CONVERSATIONAL_MODEL}])

    async def test_a_get_after_a_post_returns_the_new_model(self):
        await self._post({"model": server.CONVERSATIONAL_MODEL})
        self.assertEqual(self._json(await server.handle_config_get(None))["model"],
                         server.CONVERSATIONAL_MODEL)

    async def test_an_unknown_model_is_rejected_and_changes_nothing(self):
        response = await self._post({"model": "eleven_turbo_v2"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(server.CURRENT_MODEL, server.DEFAULT_MODEL)
        self.assertFalse(self.path.exists(), "a rejected model must not be persisted")
        self.assertEqual(self.broadcaster.of("config_updated"), [])

    async def test_a_missing_or_malformed_body_is_rejected(self):
        for body in ({}, {"model": None}, {"model": 7}, ["eleven_v3"],
                     ValueError("not json")):
            with self.subTest(body=body):
                self.assertEqual((await self._post(body)).status_code, 400)
        self.assertEqual(server.CURRENT_MODEL, server.DEFAULT_MODEL)

    async def test_a_repeat_of_the_current_model_broadcasts_nothing(self):
        response = await self._post({"model": server.DEFAULT_MODEL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.broadcaster.of("config_updated"), [])

    def test_a_persisted_model_wins_over_the_env_default_at_boot(self):
        self._patch("SPEAK_MODEL", server.DEFAULT_MODEL)
        self.path.write_text(json.dumps({"model": server.CONVERSATIONAL_MODEL}))
        self.assertEqual(server._resolve_model(self.path), server.CONVERSATIONAL_MODEL)

    def test_the_env_default_applies_when_no_config_exists(self):
        self._patch("SPEAK_MODEL", server.CONVERSATIONAL_MODEL)
        self.assertEqual(server._resolve_model(self.path), server.CONVERSATIONAL_MODEL)

    def test_an_unknown_or_unreadable_persisted_model_falls_back_to_the_env(self):
        self._patch("SPEAK_MODEL", server.DEFAULT_MODEL)
        for raw in (json.dumps({"model": "eleven_turbo_v2"}), "{not json", json.dumps([1, 2])):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.assertEqual(server._resolve_model(self.path), server.DEFAULT_MODEL)

    async def test_a_runtime_flip_reroutes_the_hop_chain(self):
        """The flip is only real if synthesis routing reads it — not just the payload."""
        self.assertEqual(server._hop_chain("short line")[0],
                         ("v3_stream", server.DEFAULT_MODEL))
        await self._post({"model": server.CONVERSATIONAL_MODEL})
        self.assertEqual(server._hop_chain("short line")[0],
                         ("conversational", server.CONVERSATIONAL_MODEL))
        await self._post({"model": server.DEFAULT_MODEL})
        self.assertEqual(server._hop_chain("short line")[0],
                         ("v3_stream", server.DEFAULT_MODEL))

    def test_the_state_snapshot_carries_the_model(self):
        """Clients render the toggle from the snapshot on connect."""
        queue = server.AudioQueue(server.SSEBroadcaster())
        self.assertEqual(queue.status()["model"], server.DEFAULT_MODEL)
        server.CURRENT_MODEL = server.CONVERSATIONAL_MODEL
        self.assertEqual(queue.status()["model"], server.CONVERSATIONAL_MODEL)


class EnqueueLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Prime the voice cache: resolve_voice_async otherwise reaches the real
        # ElevenLabs /voices endpoint for an unknown name. No test may leave this box.
        original = server._api_voices_cache
        server._api_voices_cache = {}
        self.addCleanup(setattr, server, "_api_voices_cache", original)

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

    async def test_exactly_five_thousand_chars_is_accepted(self):
        """The allow side: a gate is unverified until the boundary that must pass does."""
        queue = server.AudioQueue(server.SSEBroadcaster())
        collectors = []
        original = server.StreamCollector.start
        try:
            server.StreamCollector.start = lambda self: collectors.append(self)
            request = self._Request({"text": "x" * 5000, "voice": "voice-abc"}, queue)
            response = await server.handle_speak(request)
        finally:
            server.StreamCollector.start = original
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(queue._deque), 1)
        self.assertEqual(len(collectors), 1)


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
        for key in ("model", "gen", "ttfb_ms", "bytes", "framing", "total_ms"):
            self.assertIn(key, entry.stats, f"missing {key} in the tts log line")
        self.assertEqual(entry.stats["hop"], "v3_stream")


# --- Player backend probe ------------------------------------------------------------


class PlayerProbeTests(unittest.TestCase):
    def _patch(self, name: str, value):
        original = getattr(server, name)
        setattr(server, name, value)
        self.addCleanup(setattr, server, name, original)

    def test_fixture_probe_passes_both_backends(self):
        """Empty-input probing false-passes ffplay and false-fails audiotoolbox."""
        self.assertTrue(server.PROBE_FIXTURE.exists())
        for backend in ("ffplay", "audiotoolbox"):
            with self.subTest(backend=backend):
                if not server._probe_player(backend):
                    self.skipTest(f"{backend} unavailable on this box")

    def test_unknown_backend_selects_nothing(self):
        self._patch("LIVE_PLAYER_PREF", "not-a-player")
        self.assertIsNone(server._select_live_player())

    def test_an_unreadable_fixture_disables_live_mode(self):
        self._patch("PROBE_FIXTURE", Path("/nonexistent/probe.mp3"))
        self.assertFalse(server._probe_player("ffplay"))
        self._patch("LIVE_PLAYER_PREF", "auto")
        self.assertIsNone(server._select_live_player())

    def test_a_nonzero_exit_fails_the_probe(self):
        self._patch("PLAYER_COMMANDS", dict(server.PLAYER_COMMANDS, ffplay=["/usr/bin/false"]))
        self.assertFalse(server._probe_player("ffplay"))

    def test_auto_falls_through_to_the_second_backend(self):
        self._patch("PLAYER_COMMANDS", dict(server.PLAYER_COMMANDS, ffplay=["/usr/bin/false"]))
        self._patch("LIVE_PLAYER_PREF", "auto")
        if not server._probe_player("audiotoolbox"):
            self.skipTest("no audio output device on this box")
        self.assertEqual(server._select_live_player(), "audiotoolbox")

    def test_streaming_disabled_selects_no_player(self):
        self._patch("STREAMING_ENABLED", False)
        self.assertIsNone(server._select_live_player())


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


class QueueRunTestCase(CollectorTestCase):
    """Drives the real worker. No live player is required — see LivePlaybackTests."""

    def setUp(self):
        super().setUp()
        self._patch("PREROLL_MS", 300)
        self.audio = _silent_mp3(3.0)
        self.assertGreater(len(self.audio), 40000)
        self.events = RecordingBroadcaster()
        self.queue = server.AudioQueue(self.events)

    async def _start_collecting(self, entry, text="hello there"):
        entry.collector = server.StreamCollector(self.queue, entry, text, "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        self.queue.start()

    async def _until(self, predicate, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if predicate():
                return
        self.fail(what)

    async def _run_queue(self, entry, timeout=45.0):
        await self._start_collecting(entry)
        await self._until(lambda: bool(self.queue._history) and self.queue._current is None,
                          timeout, "entry never finished")

    async def _wait_for_live(self, timeout=30):
        await self._until(lambda: self.queue._live is not None, timeout,
                          "live playback never started")

    async def asyncTearDown(self):
        await self.queue.shutdown()


class LivePlaybackTests(QueueRunTestCase):
    def setUp(self):
        super().setUp()
        if server._probe_player("ffplay"):
            self._patch("LIVE_PLAYER", "ffplay")
        elif server._probe_player("audiotoolbox"):
            self._patch("LIVE_PLAYER", "audiotoolbox")
        else:
            self.skipTest("no live player backend available")

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

    async def test_skip_mid_live_advances_and_silences_the_entry(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()

        self.assertTrue(await self.queue.skip())
        skipped_at = len(self.events.events)
        await self._until(lambda: bool(self.queue._history), 30, "skip did not advance the worker")
        self.assertTrue(self.queue._history[0]["failed"], "skip records failed:true, as today")

        # The collector outlives the skip by ~a second, and voice_update is emitted from
        # ITS completion path — asserting before this await cannot observe the event at
        # all, so the assertion would pass with the guards deleted.
        await asyncio.wait_for(asyncio.shield(entry.collector.task), timeout=30)
        self.assertEqual(entry.outcome, "complete", "the detached collector runs to completion")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists())

        after = self.events.events[skipped_at:]
        self.assertEqual([e for e, _ in after if e in ("envelope_append", "voice_update")], [],
                         "a detached entry emits no further SSE")
        rows = [d for e, d in self.events.events
                if e == "history_update" and d["id"] == entry.history_id]
        self.assertEqual(len(rows), 1, "exactly one history row event, not an emit-then-re-emit")
        self.assertIn("history_update", [e for e, _ in after],
                      "and it lands after the commit, so replay works when it arrives")

    async def test_clear_during_live_playback_silences_and_records_nothing(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()

        player = self.queue._process
        self.assertEqual(await self.queue.clear(), 1)
        self.assertTrue(entry.cleared)
        # The kill is synchronous; _process is nulled a loop turn later, so assert the
        # process is dead rather than that the bookkeeping field caught up.
        await self._until(lambda: player.returncode is not None, 2,
                          "clear must silence audible playback immediately")

        await self._until(lambda: self.queue._current is None, 30, "clear did not advance the worker")
        await asyncio.wait_for(asyncio.shield(entry.collector.task), timeout=30)
        self.assertEqual(self.queue._history, [],
                         "a cleared entry records no history — unlike skip, which pins failed:true")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists(),
                        "the collector still finishes and caches; the audio is already billed")

    async def test_seek_during_live_replays_from_the_requested_offset(self):
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()
        await asyncio.sleep(0.4)

        self.assertTrue(self.queue.seek(1.25))
        await self._until(lambda: bool(self.queue._history), 40, "seek never completed")

        resumed = [e for e in self.events.of("voice_active") if e.get("id") and not e.get("live")]
        self.assertEqual(len(resumed), 1, "a live seek is pause-equivalent: it replays in file mode")
        self.assertAlmostEqual(resumed[0]["offset"], 1.25, delta=0.01,
                               msg="the requested offset wins over the watermark")

    async def test_the_feeder_never_runs_more_than_the_lead_ahead_of_real_time(self):
        """Unpaced, ffplay accepted 11.26 s of audio in 5.09 s of wall clock."""
        # Delivered at ~34 KB/s against 16 KB/s of playback: the file on disk always
        # holds more audio than the feeder is allowed to hand over.
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()

        state = self.queue._live
        worst = server.LIVE_FEED_LEAD_BYTES + server.LIVE_FEED_SLICE_BYTES
        samples = 0
        for _ in range(12):
            await asyncio.sleep(0.25)
            if state.frozen is not None or self.queue._live is not state:
                break
            elapsed = time.monotonic() - state.started_at - state.stalled
            lead = state.bytes_fed - elapsed * server.LIVE_CBR_BYTES_PER_SEC
            self.assertLessEqual(
                lead, worst,
                f"feeder ran {lead / server.LIVE_CBR_BYTES_PER_SEC:.2f}s ahead of real time",
            )
            samples += 1
        self.assertGreaterEqual(samples, 4, "too few samples to say anything about pacing")
        self.assertLess(worst / server.LIVE_CBR_BYTES_PER_SEC,
                        server.RESUME_REWIND_MS / 1000.0,
                        "the worst-case lead must stay under the rewind margin or a resume skips")

    async def test_the_first_slice_reaches_the_player_immediately(self):
        """Pacing a write's END rather than its START delayed the first bytes ~0.52 s."""
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()

        state = self.queue._live
        await asyncio.sleep(0.15)
        self.assertGreaterEqual(
            state.bytes_fed, server.LIVE_FEED_SLICE_BYTES,
            "the first slice must go out at once — this is the audible start, not ttfb_ms",
        )

    async def test_live_mode_records_when_audio_actually_started(self):
        """AC12's SLO subject: ttfb_ms is the vendor's latency, not the user's wait."""
        self.srv.handler = trickle(self.audio)
        entry = self._entry()
        started = time.monotonic()
        await self._run_queue(entry)
        total_ms = (time.monotonic() - started) * 1000

        first_audio = entry.stats.get("first_audio_ms")
        self.assertIsNotNone(first_audio, "the tts line must carry first_audio_ms")
        self.assertGreater(first_audio, 0)
        self.assertLess(first_audio, total_ms, "audio cannot start after the entry finished")
        self.assertLess(first_audio, entry.stats["decoded_ms"],
                        "live mode starts playing before collection has decoded in full")

    async def test_a_collector_stall_does_not_bank_pacing_credit(self):
        """Stall time counted as elapsed lets the catch-up burst race ahead again."""
        self.srv.handler = gated(self.audio[:24000], self.audio[24000:], declared=len(self.audio))
        entry = self._entry()
        await self._start_collecting(entry)
        await self._wait_for_live()

        state = self.queue._live
        await asyncio.sleep(1.5)          # the collector is stalled at the gate
        self.assertGreater(state.stalled, 0.5, "the stall must be accounted, not banked")
        self.srv.release.set()

        for _ in range(8):
            await asyncio.sleep(0.2)
            if state.frozen is not None or self.queue._live is not state:
                break
            elapsed = time.monotonic() - state.started_at - state.stalled
            lead = state.bytes_fed - elapsed * server.LIVE_CBR_BYTES_PER_SEC
            self.assertLessEqual(
                lead, server.LIVE_FEED_LEAD_BYTES + server.LIVE_FEED_SLICE_BYTES,
                f"post-stall burst put the feeder {lead / server.LIVE_CBR_BYTES_PER_SEC:.2f}s ahead",
            )


class WorkerRunTests(QueueRunTestCase):
    """Worker-level contracts that need no live player."""

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
        await self._until(lambda: bool(self.queue._history), 30, "dialogue entry never finished")

        record = self.queue._history[0]
        self.assertEqual(record["type"], "dialogue")
        self.assertFalse(record["failed"])
        self.assertAlmostEqual(record["duration"], 3.0, delta=0.4,
                               msg="a collector-less entry still gets a history duration")
        self.assertGreater(entry.stats.get("decoded_ms", 0), 0,
                           "decoded_ms is the semantic-truncation hook — every played entry has one")
        first_audio = entry.stats.get("first_audio_ms")
        self.assertIsNotNone(first_audio, "file mode stamps first_audio_ms at the afplay spawn")
        self.assertGreater(first_audio, 0)
        self.assertLess(first_audio, (time.monotonic() - entry.enqueued_at) * 1000)
        self.assertAlmostEqual(entry.dialogue_segments[0]["end"], 1.5, delta=0.3)
        self.assertFalse(os.path.exists(path), "the worker unlinks collector-less temp files")
        self.assertTrue((self.cache / f"{entry.history_id}.mp3").exists(),
                        "collector-less entries still cache at play time")
        active = [e for e in self.events.of("voice_active") if e.get("id")]
        self.assertNotIn("epoch", active[0])
        self.assertEqual(active[0]["segments"], entry.dialogue_segments)

    async def test_clear_silences_audible_file_mode_playback(self):
        """File mode claims too — a clear that reads only the live flag walks past it."""
        self.srv.handler = content_length(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._phase == "playing", 30, "playback never started")
        self.assertIsNotNone(entry.claimed_generation, "the file-mode spawn is a claim")

        cleared_at = time.monotonic()
        self.assertEqual(await self.queue.clear(), 1)
        await self._until(lambda: self.queue._process is None, 5, "the player was never killed")
        self.assertLess(time.monotonic() - cleared_at, 0.5,
                        "clear must silence audible file-mode playback immediately")

        await self._until(lambda: self.queue._current is None, 20, "worker never released the entry")
        self.assertEqual(self.queue._history, [], "a cleared entry records no history")

    async def test_an_entry_enqueued_during_a_clear_still_plays(self):
        """clear() awaits cancellation; anything arriving in that window must not strand."""
        # No worker yet: with one running it would consume the newcomer during the
        # window and the wakeup bit would be legitimately clear, hiding the defect.
        self.srv.handler = trickle(self.audio, slice_size=1024, delay=0.5)
        stuck = self._entry()
        stuck.collector = server.StreamCollector(self.queue, stuck, "hello there", "voice-abc")
        self.queue.enqueue(stuck)
        stuck.collector.start()
        await self._until(lambda: bool(stuck.attempt_path), 15, "collection never started")

        newcomer = server.QueueEntry(
            id="newcomer", audio_path="", text_preview="second",
            voice_label="Adam", created_at=time.time(), full_text="second",
        )
        # Hold clear() open inside its cancellation await so the enqueue lands squarely
        # in the window. Real cancellations are fast, so timing alone cannot pin this.
        gate = asyncio.Event()
        real_cancel = self.queue._cancel_entries

        async def gated_cancel(entries):
            await gate.wait()
            return await real_cancel(entries)

        self.queue._cancel_entries = gated_cancel
        clearing = asyncio.create_task(self.queue.clear())
        await asyncio.sleep(0.05)
        self.queue.enqueue(newcomer)
        gate.set()
        await asyncio.wait_for(clearing, timeout=20)
        self.queue._cancel_entries = real_cancel

        self.assertIn(newcomer, list(self.queue._deque), "the newcomer is still queued")
        self.assertTrue(self.queue._has_items.is_set(),
                        "the wakeup bit must survive an enqueue during the clear")

        newcomer.collector = server.StreamCollector(self.queue, newcomer, "second", "voice-abc")
        newcomer.collector.start()
        self.queue.start()
        await self._until(lambda: bool(self.queue._history), 30, "the new entry never played")
        self.assertEqual(self.queue._history[0]["id"], newcomer.history_id)

    async def test_elapsed_estimate_does_not_leak_across_entries(self):
        """A fresh entry reporting the previous entry's pause offset is a phantom clock."""
        self.srv.handler = content_length(self.audio)
        first = self._entry()
        await self._start_collecting(first)
        await self._until(lambda: self.queue._phase == "playing", 30, "playback never started")
        await asyncio.sleep(0.5)
        self.queue.pause()
        await self._until(lambda: self.queue._phase == "paused", 15, "pause never took effect")
        self.assertGreater(self.queue._play_offset, 0.0)

        self.assertEqual(await self.queue.clear(), 1)
        self.queue.resume()
        await self._until(lambda: self.queue._current is None, 20, "worker never released it")

        second = server.QueueEntry(
            id="second12", audio_path="", text_preview="second",
            voice_label="Adam", created_at=time.time(), full_text="second",
        )
        await self._start_collecting(second)
        await self._until(lambda: self.queue._current is second, 20, "second entry never picked")
        now = self.queue.status()["now_playing"]
        self.assertEqual(now["id"], second.id)
        self.assertLess(self.queue._play_offset, 0.05,
                        "the offset must reset per entry, not carry the previous pause")

    async def test_channel_clear_cancels_a_streamed_entry(self):
        """Without this the entry keeps its HTTP request, caches, and never terminates."""
        self.srv.handler = trickle(self.audio, slice_size=1024, delay=0.5)
        entry = self._entry()
        entry.channel = "chan-a"
        entry.collector = server.StreamCollector(self.queue, entry, "hello there", "voice-abc")
        self.queue.enqueue(entry)
        entry.collector.start()
        await self._until(lambda: bool(entry.attempt_path), 15, "collection never started")
        attempt_path = entry.attempt_path

        self.assertEqual(await self.queue.clear(channel="chan-a"), 1)
        self.assertEqual(entry.outcome, "cancelled")
        self.assertTrue(entry.cleared)
        self.assertTrue(entry.ready.is_set(), "ready is terminal for every outcome")
        self.assertFalse(os.path.exists(attempt_path), "the partial is deleted, not orphaned")
        self.assertFalse((self.cache / f"{entry.history_id}.mp3").exists())

    async def test_clear_while_paused_mid_file_playback_stops_the_resume(self):
        self.srv.handler = content_length(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._phase == "playing", 30, "playback never started")

        self.queue.pause()
        await self._until(lambda: self.queue._phase == "paused", 15, "pause never took effect")
        self.assertEqual(await self.queue.clear(), 1)
        self.queue.resume()

        await self._until(lambda: self.queue._current is None, 20, "worker never released the entry")
        await asyncio.sleep(0.3)
        self.assertEqual(self.queue._history, [], "a cleared entry must not play on resume")
        resumed = [e for e in self.events.of("voice_active")
                   if e.get("id") and e.get("offset")]
        self.assertEqual(resumed, [], "no player was spawned after the clear")

    async def test_pause_landing_during_the_spawn_window_is_still_absolute(self):
        """The spawn is awaited, so a pause can land after the pre-spawn gate passes."""
        self.srv.handler = content_length(self.audio)
        entry = self._entry()
        real_spawn = asyncio.create_subprocess_exec
        queue = self.queue

        async def pausing_spawn(*args, **kwargs):
            proc = await real_spawn(*args, **kwargs)
            if args and args[0] == "afplay":
                queue.pause()
            return proc

        self._patch_attr(asyncio, "create_subprocess_exec", pausing_spawn)
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._phase == "paused", 30,
                          "the post-spawn re-check never parked the entry")
        self.assertIsNone(self.queue._process, "the player spawned into a paused daemon was killed")
        self.assertEqual(self.queue._history, [], "nothing completed while paused")

    async def test_shutdown_stops_the_worker_and_joins_collectors(self):
        self.srv.handler = trickle(self.audio, slice_size=1024, delay=0.5)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._current is entry, 15, "entry never reached the head")

        await asyncio.wait_for(self.queue.shutdown(), timeout=20)
        self.assertTrue(self.queue._worker_task.done(), "the worker task must not outlive shutdown")
        self.assertTrue(self.queue._shutting_down)
        self.assertIsNone(self.queue._envelope)
        self.assertEqual(self.queue._collectors, set(), "collectors deregister on termination")


class NowPlayingTests(QueueRunTestCase):
    """Both clients consume now_playing/phase; nothing exercised it."""

    def test_the_cbr_constant_matches_the_requested_output_format(self):
        """The daemon ASKS for mp3_44100_128; the constant must be that rate in bytes."""
        codec, _rate, kbps = server.DEFAULT_FORMAT.split("_")
        self.assertEqual(codec, "mp3")
        self.assertEqual(server.LIVE_CBR_BYTES_PER_SEC, int(kbps) * 1000 // 8)

    def test_the_committed_fixture_decodes_at_that_rate(self):
        """Second, independent anchor: what ffmpeg actually encoded, per afinfo."""
        out = subprocess.run(["afinfo", str(server.PROBE_FIXTURE)],
                             capture_output=True, text=True, timeout=10).stdout
        match = re.search(r"bit rate:\s*(\d+)", out)
        self.assertIsNotNone(match, "afinfo did not report a bit rate")
        self.assertAlmostEqual(int(match.group(1)) / 8, server.LIVE_CBR_BYTES_PER_SEC, delta=200)

    async def test_idle_snapshot_carries_no_now_playing(self):
        state = self.queue.status()
        self.assertIsNone(state["now_playing"])
        self.assertFalse(state["playing"])

    async def test_collecting_phase_nulls_the_live_reconstruction_fields(self):
        self.srv.handler = trickle(self.audio, slice_size=1024, delay=0.5)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._phase == "collecting", 20, "never reached collecting")

        state = self.queue.status()
        now = state["now_playing"]
        self.assertEqual(now["id"], entry.id)
        self.assertEqual(now["phase"], "collecting")
        self.assertFalse(now["live"])
        for field in ("epoch", "elapsed_estimate", "envelope_so_far", "seq"):
            self.assertIsNone(now[field], f"{field} must be null before playback exists")
        self.assertEqual(state["items"][0]["phase"], "collecting")
        self.assertEqual(state["items"][0]["status"], "playing", "existing status values are frozen")

    async def test_paused_file_playback_freezes_the_elapsed_estimate(self):
        self.srv.handler = content_length(self.audio)
        entry = self._entry()
        await self._start_collecting(entry)
        await self._until(lambda: self.queue._phase == "playing", 30, "playback never started")
        await asyncio.sleep(0.4)

        self.queue.pause()
        await self._until(lambda: self.queue._phase == "paused", 15, "pause never took effect")
        first = self.queue.status()["now_playing"]["elapsed_estimate"]
        await asyncio.sleep(0.6)
        second = self.queue.status()["now_playing"]["elapsed_estimate"]
        self.assertEqual(first, second, "the elapsed clock must not tick while paused")
        self.assertGreater(first, 0.0)


class DetachedGuardTests(QueueRunTestCase):
    """Directly pins the guards that keep a non-active generation SSE-silent."""

    def _completed(self, **flags) -> server.QueueEntry:
        entry = self._entry()
        path = self.cache / f"{entry.history_id}.mp3"
        path.write_bytes(self.audio)
        entry.playback_path = str(path)
        entry.epoch = "abcd1234"  # epoch held: only the flag under test can suppress
        for name, value in flags.items():
            setattr(entry, name, value)
        self.queue._current = entry
        return entry

    async def test_detached_entry_holding_an_epoch_emits_no_voice_update(self):
        entry = self._completed(detached=True)
        await self.queue.on_collection_complete(entry)
        self.assertEqual(self.events.of("voice_update"), [])
        self.assertIsNone(entry.final_duration, "no decoder runs for a non-active entry")

    async def test_cleared_entry_holding_an_epoch_emits_no_voice_update(self):
        entry = self._completed(cleared=True)
        await self.queue.on_collection_complete(entry)
        self.assertEqual(self.events.of("voice_update"), [])
        self.assertIsNone(entry.final_duration)

    async def test_the_active_entry_does_get_its_voice_update(self):
        entry = self._completed()
        await self.queue.on_collection_complete(entry)
        updates = self.events.of("voice_update")
        self.assertEqual(len(updates), 1, "the allow side: an active entry must be published")
        self.assertEqual(updates[0]["epoch"], "abcd1234")
        self.assertAlmostEqual(entry.final_duration, 3.0, delta=0.4)

    async def test_a_clear_landing_inside_the_decode_suppresses_the_update(self):
        """The decode takes hundreds of ms; a control landing in it must still win."""
        entry = self._completed()
        real_extract = server._extract_envelope

        def clearing_extract(path, chunk_ms=server.ENVELOPE_CHUNK_MS):
            entry.cleared = True          # the control arrives mid-decode
            return real_extract(path, chunk_ms)

        self._patch("_extract_envelope", clearing_extract)
        await self.queue.on_collection_complete(entry)
        self.assertEqual(self.events.of("voice_update"), [],
                         "no update for an entry that stopped playing during the decode")
        self.assertIsNone(entry.final_duration, "and its metadata must not be mutated")

    async def test_a_skip_landing_inside_the_decode_suppresses_the_update(self):
        entry = self._completed()
        real_extract = server._extract_envelope

        def detaching_extract(path, chunk_ms=server.ENVELOPE_CHUNK_MS):
            entry.detached = True
            return real_extract(path, chunk_ms)

        self._patch("_extract_envelope", detaching_extract)
        await self.queue.on_collection_complete(entry)
        self.assertEqual(self.events.of("voice_update"), [])

    async def test_a_queued_entry_completing_spawns_no_decoder(self):
        entry = self._completed()
        self.queue._current = None  # completion of an entry that is not at the head
        await self.queue.on_collection_complete(entry)
        self.assertEqual(self.events.of("voice_update"), [])
        self.assertIsNone(entry.final_duration)


class LegacyPathTests(QueueRunTestCase):
    """SPEAK_STREAMING=0 — the rollback kill switch, which is new code."""

    class _Request:
        def __init__(self, body, queue):
            self._body = body
            self.app = type("App", (), {"state": type("State", (), {"queue": queue})()})()

        async def json(self):
            return self._body

    async def test_kill_switch_uses_the_legacy_fetch_with_no_collector(self):
        fetched = {}

        def fake_fetch(text, voice_id, retries=2):
            fd, path = tempfile.mkstemp(prefix=server.TEMP_PREFIX, suffix=".mp3")
            os.close(fd)
            Path(path).write_bytes(self.audio)
            fetched["text"] = text
            fetched["path"] = path
            return path

        self._patch("STREAMING_ENABLED", False)
        self._patch("_fetch_tts", fake_fetch)
        self._patch("LIVE_PLAYER", None)

        request = self._Request({"text": "legacy hello", "voice": "voice-abc"}, self.queue)
        response = await server.handle_speak(request)
        self.assertEqual(response.status_code, 200)

        entry = self.queue._deque[0]
        self.assertIsNone(entry.collector, "the kill switch attaches no collector")
        await asyncio.wait_for(entry.ready.wait(), timeout=20)
        self.assertEqual(entry.outcome, "complete")
        self.assertEqual(entry.playback_path, fetched["path"])
        self.assertEqual(entry.audio_path, fetched["path"])
        self.assertEqual(fetched["text"], "legacy hello")

        self.queue.start()
        await self._until(lambda: bool(self.queue._history), 30, "legacy entry never played")
        self.assertFalse(self.queue._history[0]["failed"])
        self.assertFalse(os.path.exists(fetched["path"]),
                         "the worker owns a collector-less temp file")

    async def test_kill_switch_records_a_failed_fetch(self):
        def boom(text, voice_id, retries=2):
            raise ValueError("API returned invalid audio")

        self._patch("STREAMING_ENABLED", False)
        self._patch("_fetch_tts", boom)
        request = self._Request({"text": "legacy hello", "voice": "voice-abc"}, self.queue)
        await server.handle_speak(request)

        entry = self.queue._deque[0]
        await asyncio.wait_for(entry.ready.wait(), timeout=20)
        self.assertEqual(entry.outcome, "failed")
        self.assertTrue(entry.fetch_failed)


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

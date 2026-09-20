import http.client
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import freeastra as fa


class AdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.originals = {
            "API_ONLY": fa.API_ONLY,
            "API_KEY": fa.API_KEY,
            "SSE_HEARTBEAT": fa.SSE_HEARTBEAT,
            "ALLOW_FALLBACK": fa.ALLOW_FALLBACK,
            "call_prism": fa.call_prism,
            "_prism_attempt": fa._prism_attempt,
            "try_refresh": fa.try_refresh,
        }
        fa.API_ONLY = True
        fa.API_KEY = ""
        fa.SSE_HEARTBEAT = 0.01
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fa.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        for name, value in cls.originals.items():
            setattr(fa, name, value)

    def tearDown(self):
        fa.API_ONLY = True
        fa.API_KEY = ""
        fa.API_REFRESH = False
        fa.SSE_HEARTBEAT = 0.01
        fa.ALLOW_FALLBACK = False
        fa.call_prism = self.originals["call_prism"]
        fa._prism_attempt = self.originals["_prism_attempt"]
        fa.try_refresh = self.originals["try_refresh"]

    def raw_request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        return self.raw_request(
            method, path, data,
            {"content-type": "application/json", **(headers or {})})

    def raw_http(self, request_bytes):
        with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(request_bytes)
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        status = int(status_line.split()[1])
        return status, head, body

    def release_turn(self, queued_at):
        if queued_at is not None:
            fa._turn_lock.release()

    def test_models_exposes_only_prism_aliases(self):
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        ids = {row["id"] for row in payload["data"]}
        self.assertEqual(ids, set(fa.ALIAS))
        self.assertNotIn("gpt-6-astra", ids)

    def test_responses_reads_nested_reasoning_effort(self):
        seen = {}

        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen.update(model=model, effort=effort, user=user)
            return "ok"

        fa.call_prism = fake
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": "hello",
            "reasoning": {"effort": "high"},
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(seen["model"], "gpt-6-astra")
        self.assertEqual(seen["effort"], "high")
        self.assertEqual(seen["user"], "[user]\nhello")
        self.assertEqual(payload["model"], "prism-astra")

    def test_responses_string_input_is_not_split_into_characters(self):
        seen = {}

        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["user"] = user
            return "ok"

        fa.call_prism = fake
        status, _, _ = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": "hello world",
        })
        self.assertEqual(status, 200)
        self.assertEqual(seen["user"], "[user]\nhello world")
        self.assertNotIn("[user]\ne\n\n[user]\nl", seen["user"])

    def test_previous_response_id_is_explicitly_rejected(self):
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": "hello",
            "previous_response_id": "resp_fake",
        })
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["param"], "previous_response_id")

    def test_image_input_is_not_silently_dropped(self):
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "https://example.invalid/a.png"}],
            }],
        })
        self.assertEqual(status, 400)
        self.assertIn(b"text-only", body)

    def test_unknown_model_does_not_passthrough_in_api_mode(self):
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "gpt-6-astra",
            "input": "hello",
        })
        self.assertEqual(status, 400)
        self.assertIn(b"model_not_found", body)

    def test_api_only_unknown_prefixed_routes_fail_closed(self):
        status, _, body = self.request("GET", "/v1/anything/models")
        self.assertEqual(status, 404)
        self.assertIn(b"unsupported_route", body)

        for path in (
            "/v1/anything/responses",
            "/v1/anything/chat/completions",
        ):
            status, _, body = self.request("POST", path, {"model": "prism-astra"})
            self.assertEqual(status, 404)
            self.assertIn(b"unsupported_route", body)

    def test_health_suffix_does_not_bypass_adapter_auth(self):
        fa.API_KEY = "secret"
        status, _, body = self.request("GET", "/anything/healthz")
        self.assertEqual(status, 401)
        self.assertIn(b"authentication_error", body)

    def test_api_only_rejects_unsafe_http_body_framing(self):
        base = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
        )

        status, _, body = self.raw_http(
            base
            + b"Transfer-Encoding: chunked\r\n\r\n"
            + b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"Transfer-Encoding", body)

        status, _, body = self.raw_http(base + b"\r\n")
        self.assertEqual(status, 411)
        self.assertIn(b"Content-Length", body)

        status, _, body = self.raw_http(
            base + b"Content-Length: -1\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"non-negative", body)

        status, _, body = self.raw_http(
            base
            + b"Content-Length: 2\r\n"
            + b"Content-Length: 2\r\n\r\n{}")
        self.assertEqual(status, 400)
        self.assertIn(b"exactly one Content-Length", body)

        status, _, body = self.raw_http(
            base + b"Content-Length: 1_0\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"non-negative", body)

        status, _, body = self.raw_http(
            base + b"Content-Length: " + b"9" * 5000 + b"\r\n\r\n")
        self.assertEqual(status, 413)
        self.assertIn(b"too large", body)

    def test_requests_with_body_are_rejected_on_get_and_options(self):
        status, head, body = self.raw_http(
            b"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: 2\r\n\r\n{}")
        self.assertEqual(status, 400)
        self.assertIn(b"Connection: close", head)
        self.assertIn(b"must not carry a body", body)

        status, head, _ = self.raw_http(
            b"OPTIONS /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"Connection: close", head)

    def test_api_only_early_rejections_close_connections(self):
        base = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
        )

        status, head, _ = self.raw_http(
            base + b"Content-Length: %d\r\n\r\n" % (fa.MAX_BODY + 1))
        self.assertEqual(status, 413)
        self.assertIn(b"Connection: close", head)

        fa.API_KEY = "secret"
        status, head, _ = self.raw_http(
            base + b"Content-Length: 2\r\n\r\n{}")
        self.assertEqual(status, 401)
        self.assertIn(b"Connection: close", head)
        fa.API_KEY = ""

        status, head, _ = self.raw_http(
            b"POST /v1/embeddings HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}")
        self.assertEqual(status, 404)
        self.assertIn(b"Connection: close", head)

    def test_successful_requests_keep_connection_alive(self):
        request = (b"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   b"Content-Length: 0\r\n\r\n")
        with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(request * 2)
            data = b""
            while data.count(b"HTTP/1.1 200") < 2:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.shutdown(socket.SHUT_WR)
            while sock.recv(65536):
                pass
        self.assertEqual(data.count(b"HTTP/1.1 200"), 2)

    def test_api_only_rejects_compressed_body_before_decompression(self):
        # The body does not need to be valid compressed data: API-only must reject
        # Content-Encoding before invoking any decompressor.
        status, _, body = self.raw_request(
            "POST", "/v1/responses",
            b"not-even-valid-gzip",
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 415)
        self.assertIn(b"unsupported_media_type", body)

    def test_invalid_json_field_types_return_400(self):
        cases = [
            {"model": 123, "input": "hello"},
            {"model": "", "input": "hello"},
            {"model": "prism-astra", "input": 123},
            {"model": "prism-astra", "input": "hello", "tools": ""},
            {"model": "prism-astra", "input": "hello", "tools": 0},
            {"model": "prism-astra", "input": "hello", "tools": None},
            {"model": "prism-astra", "input": "hello", "tools": [123]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "name": 123, "parameters": {}}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "function": "not-an-object"}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "function": {}}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "name": "x", "parameters": []}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "name": "x", "parameters": ""}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "name": "x", "parameters": None}]},
            {"model": "prism-astra", "input": "hello",
             "tools": [{"type": "function", "name": "x",
                        "parameters": {}, "description": 0}]},
            {"model": "prism-astra",
             "input": [{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": 123}]}]},
            {"model": "prism-astra", "input": "hello", "instructions": 0},
            {"model": "prism-astra", "input": "hello", "background": 0},
            {"model": "prism-astra", "input": "hello", "store": ""},
            {"model": "prism-astra", "input": "hello",
             "reasoning": {"effort": 0}},
            {"model": "prism-astra", "input": "hello", "reasoning_effort": 0},
            {"model": "prism-astra", "input": "hello", "text": ""},
            {"model": "prism-astra", "input": "hello",
             "text": {"format": ""}},
            {"model": "prism-astra", "input": "hello", "parallel_tool_calls": ""},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, _, body = self.request("POST", "/v1/responses", payload)
                self.assertEqual(status, 400, body)
                self.assertIn(b"invalid_request_error", body)

    def test_api_only_requires_explicit_non_null_model(self):
        called = []

        def fake(*args, **kwargs):
            called.append(True)
            return "should not run"

        fa.call_prism = fake
        for payload in (
            {"input": "hello"},
            {"model": None, "input": "hello"},
        ):
            with self.subTest(payload=payload):
                status, _, body = self.request(
                    "POST", "/v1/responses", payload)
                self.assertEqual(status, 400)
                parsed = json.loads(body)
                self.assertEqual(parsed["error"]["param"], "model")
        self.assertEqual(called, [])

    def test_api_only_requires_input_and_chat_messages(self):
        cases = [
            ("/v1/responses", {"model": "prism-astra"}, "input"),
            ("/v1/responses", {"model": "prism-astra", "input": None}, "input"),
            ("/v1/chat/completions", {"model": "prism-astra"}, "messages"),
            ("/v1/chat/completions",
             {"model": "prism-astra", "messages": None}, "messages"),
        ]
        for path, payload, param in cases:
            with self.subTest(path=path, payload=payload):
                status, _, body = self.request("POST", path, payload)
                self.assertEqual(status, 400)
                parsed = json.loads(body)
                self.assertEqual(parsed["error"]["param"], param)

    def test_chat_optional_field_types_are_validated(self):
        cases = [
            {"model": "prism-astra", "messages": [], "reasoning_effort": 0},
            {"model": "prism-astra", "messages": [], "response_format": ""},
            {"model": "prism-astra", "messages": [], "parallel_tool_calls": 0},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, _, body = self.request(
                    "POST", "/v1/chat/completions", payload)
                self.assertEqual(status, 400, body)
                self.assertIn(b"invalid_request_error", body)

    def test_stream_emits_responses_lifecycle(self):
        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            self.release_turn(queued_at)
            return "hello"

        fa.call_prism = fake
        status, headers, body = self.request("POST", "/v1/responses", {
            "model": "prism-sol",
            "input": "hello",
            "stream": True,
        })
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        text = body.decode()
        for event in (
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ):
            self.assertIn("event: " + event, text)
        self.assertNotIn("[DONE]", text)

    def test_stream_emits_function_argument_events(self):
        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            self.release_turn(queued_at)
            return '{"tool_call":{"name":"echo","arguments":{"value":"ok"}}}'

        fa.call_prism = fake
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-sol",
            "input": "use echo",
            "stream": True,
            "tools": [{
                "type": "function",
                "name": "echo",
                "description": "echo",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }],
        })
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("event: response.function_call_arguments.delta", text)
        self.assertIn("event: response.function_call_arguments.done", text)
        done_blocks = [
            block for block in text.split("\n\n")
            if block.startswith("event: response.function_call_arguments.done\n")
        ]
        self.assertEqual(len(done_blocks), 1)
        data_line = next(
            line for line in done_blocks[0].splitlines()
            if line.startswith("data: "))
        done_payload = json.loads(data_line[len("data: "):])
        self.assertEqual(done_payload["name"], "echo")
        self.assertEqual(json.loads(done_payload["arguments"]), {"value": "ok"})

    def test_unknown_model_emitted_tool_is_rejected(self):
        with self.assertRaises(fa.ToolProtocolError):
            fa.parse_tool_call(
                '{"tool_call":{"name":"not_allowed","arguments":{}}}',
                {"allowed"},
            )

    def test_tool_schema_is_not_truncated_at_700_bytes(self):
        marker = "TAIL_SENTINEL"
        tools = [{
            "type": "function",
            "name": "long_schema",
            "description": "test",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": ("x" * 900) + marker,
                    }
                },
            },
        }]
        system, _ = fa.assemble([], ["[user]\nrun"], tools)
        self.assertIn(marker, system)

    def test_strict_model_identity_disables_fallback_by_default(self):
        calls = []

        def fake_attempt(inp, model, effort, deadline, retries, cancel=None):
            calls.append(model)
            return None, "400 Unsupported assistant model"

        fa._prism_attempt = fake_attempt
        fa.try_refresh = lambda reason, cancel=None: False
        fa.ALLOW_FALLBACK = False
        with self.assertRaises(RuntimeError):
            fa._call_prism_locked(
                "gpt-6-astra", "", "hello", "medium", 1, fa.time.time())
        self.assertEqual(calls, ["gpt-6-astra"])

    def test_unknown_responses_item_type_is_rejected_in_api_only(self):
        with self.assertRaises(fa.ClientInputError):
            fa.flatten_responses([{"type": "reasoning", "summary": []}], None, [])

    def test_unknown_responses_item_type_is_tolerated_outside_api_only(self):
        fa.API_ONLY = False
        _, user = fa.flatten_responses(
            [{"type": "reasoning", "summary": []}, "hello"], None, [])
        self.assertEqual(user, "[user]\nhello")

    def test_malformed_tool_item_fields_return_400(self):
        cases = [
            {"type": "function_call_output", "output": 123},
            {"type": "custom_tool_call", "input": 123},
            {"type": "custom_tool_call_output", "output": 123},
        ]
        for item in cases:
            with self.subTest(item=item):
                status, _, body = self.request("POST", "/v1/responses", {
                    "model": "prism-astra",
                    "input": [item],
                })
                self.assertEqual(status, 400, body)
                self.assertIn(b"invalid_request_error", body)

    def test_structured_tool_output_is_serialized(self):
        seen = {}

        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["user"] = user
            return "ok"

        fa.call_prism = fake
        status, _, _ = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [
                {"type": "function_call_output", "call_id": "c1", "output": {"a": 1}},
                "hello",
            ],
        })
        self.assertEqual(status, 200)
        self.assertIn('{"a": 1}', seen["user"])

    def test_pre_cancelled_prism_attempt_stops_immediately(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(fa.CancelledError):
            fa._prism_attempt([], "gpt-6-astra", "low", fa.time.time() + 60, 3,
                              cancel=cancel)

    def test_pre_cancelled_call_prism_does_not_take_the_sandbox(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(fa.CancelledError):
            fa.call_prism("gpt-6-astra", "", "hi", "low", retries=1, cancel=cancel)

    def test_stream_disconnect_cancels_prism_worker(self):
        started = threading.Event()
        release = threading.Event()
        seen = {}

        def slow(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["cancel"] = cancel
            started.set()
            release.wait(5)
            self.release_turn(queued_at)
            return "late"

        fa.call_prism = slow
        body = json.dumps({
            "model": "prism-astra", "input": "hello", "stream": True,
        }).encode()
        with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body)
            sock.recv(1024)
        self.assertTrue(started.wait(3))
        deadline = fa.time.time() + 5
        while not seen["cancel"].is_set() and fa.time.time() < deadline:
            fa.time.sleep(0.05)
        self.assertTrue(seen["cancel"].is_set())
        release.set()

    def test_non_stream_disconnect_cancels_prism_worker(self):
        started = threading.Event()
        release = threading.Event()
        seen = {}

        def slow(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["cancel"] = cancel
            started.set()
            release.wait(5)
            self.release_turn(queued_at)
            return "late"

        fa.call_prism = slow
        body = json.dumps({"model": "prism-astra", "input": "hello"}).encode()
        with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.assertTrue(started.wait(3))
            sock.shutdown(socket.SHUT_WR)
        deadline = fa.time.time() + 5
        while not seen["cancel"].is_set() and fa.time.time() < deadline:
            fa.time.sleep(0.05)
        self.assertTrue(seen["cancel"].is_set())
        release.set()

    def test_cancelled_queue_wait_releases_promptly(self):
        cancel = threading.Event()
        self.assertTrue(fa._turn_lock.acquire(timeout=1))
        finished = threading.Event()

        def worker():
            try:
                fa.call_prism("gpt-6-astra", "", "hi", "low", retries=1,
                              cancel=cancel)
            except fa.CancelledError:
                pass
            finally:
                finished.set()

        try:
            thread = threading.Thread(target=worker)
            thread.start()
            fa.time.sleep(0.1)
            cancel.set()
            self.assertTrue(finished.wait(3))
        finally:
            fa._turn_lock.release()

    def test_stream_busy_returns_503_before_sse(self):
        saved = fa.QUEUE_TIMEOUT
        fa.QUEUE_TIMEOUT = 0.3
        self.assertTrue(fa._turn_lock.acquire(timeout=1))
        try:
            status, headers, body = self.request("POST", "/v1/responses", {
                "model": "prism-astra", "input": "hello", "stream": True,
            })
        finally:
            fa._turn_lock.release()
            fa.QUEUE_TIMEOUT = saved
        self.assertEqual(status, 503)
        self.assertIn(b"prism_busy", body)
        self.assertNotIn("text/event-stream", headers.get("Content-Type", ""))
        self.assertNotIn(b"event:", body)

    def test_api_only_disables_browser_auto_refresh(self):
        self.assertTrue(fa.API_ONLY)
        before = fa._last_refresh[0]
        original = fa.os.path.exists
        fa.os.path.exists = lambda path: True
        try:
            self.assertFalse(fa.try_refresh("stale session"))
            touched = fa._last_refresh[0] != before
        finally:
            fa.os.path.exists = original
            fa._last_refresh[0] = before
        self.assertFalse(touched)

    def test_cancelled_refresh_never_spawns(self):
        fa.API_ONLY = False
        fa.API_REFRESH = True
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(fa.CancelledError):
            fa.try_refresh("stale session", cancel=cancel)

    def test_api_only_disables_keepalive_by_default(self):
        saved = fa.os.environ.pop("PRISM_KEEPALIVE", None)
        try:
            fa.API_ONLY = True
            self.assertEqual(fa.keepalive_period(), 0.0)
            fa.API_ONLY = False
            self.assertEqual(fa.keepalive_period(), 600.0)
            fa.os.environ["PRISM_KEEPALIVE"] = "30"
            self.assertEqual(fa.keepalive_period(), 30.0)
        finally:
            if saved is None:
                fa.os.environ.pop("PRISM_KEEPALIVE", None)
            else:
                fa.os.environ["PRISM_KEEPALIVE"] = saved

    def test_api_only_insecure_bind_is_refused(self):
        saved_bind = fa.BIND
        saved_allow = fa.os.environ.pop("PRISM_ALLOW_INSECURE", None)
        try:
            fa.API_ONLY = True
            fa.API_KEY = ""
            fa.BIND = "0.0.0.0"
            with self.assertRaises(SystemExit):
                fa.check_api_config()
            fa.BIND = "127.0.0.1"
            fa.check_api_config()
            fa.BIND = "0.0.0.0"
            fa.API_KEY = "secret"
            fa.check_api_config()
            fa.API_KEY = ""
            fa.os.environ["PRISM_ALLOW_INSECURE"] = "1"
            fa.check_api_config()
        finally:
            fa.BIND = saved_bind
            fa.API_KEY = ""
            if saved_allow is None:
                fa.os.environ.pop("PRISM_ALLOW_INSECURE", None)
            else:
                fa.os.environ["PRISM_ALLOW_INSECURE"] = saved_allow

    def test_head_mirrors_get_without_body(self):
        status, headers, body = self.raw_request("HEAD", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)

        status, _, body = self.raw_request("HEAD", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

        status, _, body = self.raw_request("HEAD", "/v1/anything")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

        fa.API_KEY = "secret"
        status, _, body = self.raw_request("HEAD", "/v1/models")
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")

    def test_head_error_paths_send_no_body(self):
        for headers in (
            b"Transfer-Encoding: chunked\r\n",
            b"Content-Length: abc\r\n",
            b"Content-Length: 2\r\n",
        ):
            with self.subTest(headers=headers):
                status, _, body = self.raw_http(
                    b"HEAD /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    + headers + b"\r\n")
                self.assertEqual(status, 400)
                self.assertEqual(body, b"")

    def test_ipv6_loopback_bind_is_supported(self):
        self.assertEqual(fa.IPv6ThreadingHTTPServer.address_family, fa.socket.AF_INET6)
        try:
            server = fa.IPv6ThreadingHTTPServer(("::1", 0), fa.Handler)
        except OSError:
            self.skipTest("IPv6 loopback is not available")
        try:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("::1", server.server_port, timeout=3)
            try:
                connection.request("GET", "/v1/models")
                response = connection.getresponse()
                status = response.status
                response.read()
            finally:
                connection.close()
            server.shutdown()
            thread.join(timeout=2)
            self.assertEqual(status, 200)
        finally:
            server.server_close()

    def test_function_call_fields_are_type_checked(self):
        cases = [
            {"type": "function_call", "name": 123, "arguments": "{}"},
            {"type": "function_call", "name": "x", "arguments": 0},
        ]
        for item in cases:
            with self.subTest(item=item):
                status, _, body = self.request("POST", "/v1/responses", {
                    "model": "prism-astra", "input": [item],
                })
                self.assertEqual(status, 400, body)
                self.assertIn(b"invalid_request_error", body)

    def test_function_call_dict_arguments_are_serialized(self):
        seen = {}

        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["user"] = user
            return "ok"

        fa.call_prism = fake
        status, _, _ = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [{"type": "function_call", "name": "x", "arguments": {"a": 1}}, "go"],
        })
        self.assertEqual(status, 200)
        self.assertIn('{"a": 1}', seen["user"])

    def test_function_call_output_content_parts_are_joined(self):
        seen = {}

        def fake(model, system, user, effort, retries=3, cancel=None, queued_at=None):
            seen["user"] = user
            return "ok"

        fa.call_prism = fake
        status, _, _ = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [
                {"type": "function_call_output", "call_id": "c1", "output": [
                    {"type": "input_text", "text": "hello"},
                    {"type": "input_text", "text": " world"},
                ]},
                "go",
            ],
        })
        self.assertEqual(status, 200)
        self.assertIn("hello world", seen["user"])

    def test_function_call_output_image_part_is_rejected(self):
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [{
                "type": "function_call_output", "call_id": "c1",
                "output": [{"type": "input_image",
                            "image_url": "https://example.invalid/a.png"}],
            }],
        })
        self.assertEqual(status, 400, body)
        self.assertIn(b"text-only", body)

    def test_deeply_nested_additional_tools_are_rejected(self):
        node = {"type": "function", "name": "leaf", "parameters": {}}
        for _ in range(12):
            node = {"type": "namespace", "name": "ns", "tools": [node]}
        status, _, body = self.request("POST", "/v1/responses", {
            "model": "prism-astra",
            "input": [{"type": "additional_tools", "tools": [node]}, "go"],
        })
        self.assertEqual(status, 400, body)
        self.assertIn(b"nests too deeply", body)

    def test_passthrough_malformed_content_length_is_400(self):
        fa.API_ONLY = False
        status, _, body = self.raw_http(
            b"POST /v1/embeddings HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: abc\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn(b"non-negative", body)

        status, _, body = self.raw_http(
            b"POST /v1/embeddings HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
        self.assertEqual(status, 400)
        self.assertIn(b"Content-Length", body)

    def test_optional_adapter_api_key(self):
        fa.API_KEY = "secret"
        status, _, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 401)
        status, _, body = self.request(
            "GET", "/v1/models",
            headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)
        self.assertIn(b"prism-astra", body)


if __name__ == "__main__":
    unittest.main()

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

    def test_models_exposes_only_prism_aliases(self):
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        ids = {row["id"] for row in payload["data"]}
        self.assertEqual(ids, set(fa.ALIAS))
        self.assertNotIn("gpt-6-astra", ids)

    def test_responses_reads_nested_reasoning_effort(self):
        seen = {}

        def fake(model, system, user, effort, retries=3):
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

        def fake(model, system, user, effort, retries=3):
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

    def test_stream_emits_responses_lifecycle(self):
        fa.call_prism = lambda model, system, user, effort, retries=3: "hello"
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
        fa.call_prism = lambda model, system, user, effort, retries=3: (
            '{"tool_call":{"name":"echo","arguments":{"value":"ok"}}}'
        )
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

        def fake_attempt(inp, model, effort, deadline, retries):
            calls.append(model)
            return None, "400 Unsupported assistant model"

        fa._prism_attempt = fake_attempt
        fa.try_refresh = lambda reason: False
        fa.ALLOW_FALLBACK = False
        with self.assertRaises(RuntimeError):
            fa._call_prism_locked(
                "gpt-6-astra", "", "hello", "medium", 1, fa.time.time())
        self.assertEqual(calls, ["gpt-6-astra"])

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

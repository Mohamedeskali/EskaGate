#!/usr/bin/env python3
"""Key/model detection against a fake provider: python3 -m unittest discover tests  (standard library only)."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_CONSOLE_HOME", tempfile.mkdtemp(prefix="eskagate-test-"))
# The fake provider lives on 127.0.0.1: never send it through a proxy.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))

import i18n  # noqa: E402
import detect  # noqa: E402
import gateway  # noqa: E402
import api_web_dashboard_v2 as app  # noqa: E402

i18n._lang = "en"
GOOD = "sk-test-Good0123456789abcdef"
ANT = "sk-ant-api03-Good0123456789abcdef"
CALLS = []          # (scenario, path, model) of every chat request the fake provider got


class Fake(BaseHTTPRequestHandler):
    """/<scenario>/... — each scenario is one kind of provider."""
    def log_message(self, *a):
        pass

    def send(self, status, obj, headers=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def bearer_ok(self, key=GOOD):
        return self.headers.get("Authorization") == f"Bearer {key}"

    def anthropic_ok(self):
        return self.headers.get("x-api-key") == ANT and self.headers.get("anthropic-version")

    def do_GET(self):
        scen, path = self.path.split("/", 2)[1], "/" + self.path.split("/", 2)[2].split("?")[0]
        if scen == "html":
            return self.send(200, None, raw=b"<!doctype html><html><body>Welcome to our site</body></html>", ctype="text/html")
        if scen == "down":
            return self.send(503, {"error": {"message": "upstream unavailable"}})
        if scen in ("openai", "nocredit") and path == "/v1/models":
            if not self.bearer_ok():
                return self.send(401, {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}})
            names = ["gpt-good", "fast-mini", "gone-model", "limited-model", "swap-model", "needs16", "text-embed-x"]
            return self.send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in names]})
        if scen in ("anthropic", "fakeclaude") and path == "/v1/models":
            if not self.anthropic_ok():
                return self.send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            return self.send(200, {"data": [{"type": "model", "id": m, "display_name": m} for m in ("claude-haiku-9", "claude-gone-1")],
                                   "has_more": False, "first_id": "claude-haiku-9", "last_id": "claude-gone-1"})
        self.send(404, None, raw=b"Not Found", ctype="text/plain")

    def do_POST(self):
        scen, path = self.path.split("/", 2)[1], "/" + self.path.split("/", 2)[2]
        req = self.body()
        model = req.get("model")
        CALLS.append((scen, path, model))
        if scen in ("openai", "nocredit") and path == "/v1/chat/completions":
            if not self.bearer_ok():
                return self.send(401, {"error": {"message": "Incorrect API key provided"}})
            if scen == "nocredit":
                return self.send(429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                                                 "type": "insufficient_quota"}})
            if model == "gone-model":
                return self.send(404, {"error": {"message": f"The model `{model}` does not exist", "code": "model_not_found"}})
            if model == "limited-model":
                return self.send(429, {"error": {"message": "Rate limit reached"}}, {"Retry-After": "0"})
            if model == "text-embed-x":
                return self.send(400, {"error": {"message": "This is not a chat model"}})
            if model == "needs16" and (req.get("max_tokens") or req.get("max_completion_tokens") or 0) < 16:
                return self.send(400, {"error": {"message": "max_tokens must be at least 16"}})
            served = "totally-different-model" if model == "swap-model" else model + "-2025-01-01"
            return self.send(200, {"id": "chatcmpl-1", "object": "chat.completion", "model": served,
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": "p"}, "finish_reason": "length"}],
                                   "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})
        if scen in ("anthropic", "fakeclaude") and path == "/v1/messages":
            if not self.anthropic_ok():
                return self.send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            if model == "claude-gone-1":
                return self.send(404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {model}"}})
            if scen == "fakeclaude":   # claims Claude, but neither the shape nor the headers are Anthropic's
                return self.send(200, {"id": "chatcmpl-9", "type": "message", "role": "assistant", "model": model,
                                       "content": [{"type": "text", "text": "p"}], "stop_reason": "length",
                                       "usage": {"prompt_tokens": 3}})
            return self.send(200, {"id": "msg_01AbC", "type": "message", "role": "assistant", "model": model,
                                   "content": [{"type": "text", "text": "p"}], "stop_reason": "max_tokens",
                                   "stop_sequence": None, "usage": {"input_tokens": 8, "output_tokens": 1}},
                             {"request-id": "req_01XyZ", "anthropic-ratelimit-requests-limit": "50"})
        self.send(404, None, raw=b"Not Found", ctype="text/plain")


def run(**params):
    events = []
    app.run_pipeline({"timeout": 5, "retries": 1, "max_tokens": 1, **params}, lambda kind, data: events.append((kind, data)))
    return events


def first(events, kind):
    return next((d for k, d in events if k == kind), None)


def results(events):
    return {d["model"]: d for k, d in events if k == "model_result"}


class FakeProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        CALLS.clear()

    def test_openai_deep_check_labels_each_model(self):
        ev = run(base_url=f"{self.base}/openai/v1/", api_key=GOOD, mode="deep")
        r, s = results(ev), first(ev, "summary")
        self.assertEqual(s["key_status"], "VALID")
        self.assertEqual(s["format"], "openai")
        self.assertEqual(r["gpt-good"]["verdict"], "working")
        self.assertEqual(r["gpt-good"]["genuine"]["badge"], "verified")      # dated variant of the same name
        self.assertEqual(r["gone-model"]["verdict"], "broken")               # listed, but not working
        self.assertEqual(r["limited-model"]["verdict"], "limited")           # 429: unverified, not broken
        self.assertEqual(r["text-embed-x"]["verdict"], "broken")
        self.assertEqual(r["needs16"]["verdict"], "working")                 # 1 token refused -> retried with 16
        self.assertEqual(r["swap-model"]["genuine"]["badge"], "suspicious")  # answered as another model
        self.assertIn("totally-different-model", " ".join(r["swap-model"]["genuine"]["reasons"]))
        self.assertEqual(s["counts"], {"working": 4, "broken": 2, "limited": 1, "unverified": 0})
        limited_calls = [c for c in CALLS if c[2] == "limited-model"]
        self.assertEqual(len(limited_calls), 2)                              # retried once, then left unverified
        self.assertEqual({c[2] for c in CALLS if c[2] in ("gpt-good",)}, {"gpt-good"})
        self.assertTrue(all(c[1] == "/v1/chat/completions" for c in CALLS))

    def test_quick_check_is_one_real_request_on_a_cheap_model(self):
        ev = run(base_url=f"{self.base}/openai/v1", api_key=GOOD, mode="quick")
        self.assertEqual(first(ev, "summary")["key_status"], "VALID")
        self.assertEqual(first(ev, "models_found")["count"], 7)
        self.assertEqual([c[2] for c in CALLS], ["fast-mini"])

    def test_list_alone_never_makes_a_key_valid(self):
        ev = run(base_url=f"{self.base}/nocredit/v1", api_key=GOOD, mode="deep")
        s = first(ev, "summary")
        self.assertEqual(s["key_status"], "NO_CREDIT")
        self.assertEqual(len(CALLS), 1)                                      # no credit: the other models are skipped
        self.assertTrue(all(d["status"] == "SKIPPED" for d in results(ev).values() if d["model"] != CALLS[0][2]))

    def test_rejected_key_fails_fast_without_chat_calls(self):
        ev = run(base_url=f"{self.base}/openai/v1", api_key="sk-test-Wrong0123456789abcdef")
        err = first(ev, "error")
        self.assertEqual(err["key_status"], "INVALID")
        self.assertEqual(CALLS, [])

    def test_anthropic_detected_from_a_partial_url(self):
        ev = run(base_url=f"127.0.0.1:{self.port}/anthropic//", api_key=ANT, mode="deep")   # no scheme, doubled slash
        s, r = first(ev, "summary"), results(ev)
        self.assertEqual(s["format"], "anthropic")
        self.assertEqual(s["key_status"], "VALID")
        self.assertEqual(r["claude-haiku-9"]["genuine"]["badge"], "verified")
        self.assertEqual(r["claude-gone-1"]["verdict"], "broken")
        self.assertTrue(all(c[1] == "/v1/messages" for c in CALLS))
        self.assertEqual(s["config"]["provider"]["custom_provider"]["npm"], "@ai-sdk/anthropic")

    def test_anthropic_found_even_when_auto_tries_openai_first(self):
        ev = run(base_url=f"{self.base}/anthropic", api_key="sk-test-Good0123456789abcdef-x", mode="quick",
                 prefer_format="openai")
        # A non-Anthropic key here is refused in Anthropic's own error shape: reported as invalid.
        self.assertEqual(first(ev, "error")["key_status"], "INVALID")
        ev = run(base_url=f"{self.base}/anthropic", api_key=ANT, mode="quick", prefer_format="openai")
        self.assertEqual(first(ev, "summary")["format"], "anthropic")
        self.assertTrue(first(ev, "detected")["note"])                       # "Bearer refused, using Anthropic"

    def test_type_set_to_openai_on_an_anthropic_endpoint(self):
        ev = run(base_url=f"{self.base}/anthropic", api_key=ANT, format="openai")
        err = first(ev, "error")
        self.assertEqual(err["key_status"], "WRONG_ENDPOINT")
        self.assertIn("Anthropic", err["message"])
        self.assertEqual(CALLS, [])

    def test_anthropic_key_on_an_openai_only_url(self):
        ev = run(base_url=f"{self.base}/openai/v1", api_key=ANT)
        err = first(ev, "error")
        self.assertEqual(err["key_status"], "WRONG_ENDPOINT")
        self.assertIn("sk-ant-", err["message"])
        self.assertEqual(err["format"], "openai")

    def test_openai_key_on_an_anthropic_url(self):
        ev = run(base_url=f"{self.base}/anthropic", api_key="sk-proj-Abc0123456789defghij")
        self.assertEqual(first(ev, "error")["key_status"], "WRONG_ENDPOINT")

    def test_fake_claude_is_suspicious(self):
        ev = run(base_url=f"{self.base}/fakeclaude", api_key=ANT, format="anthropic", mode="deep")
        g = results(ev)["claude-haiku-9"]["genuine"]
        self.assertEqual(g["badge"], "suspicious")
        self.assertTrue(any("msg_" in x for x in g["reasons"]))

    def test_web_page_instead_of_api(self):
        ev = run(base_url=f"{self.base}/html", api_key=GOOD)
        self.assertEqual(first(ev, "error")["key_status"], "WRONG_ENDPOINT")
        self.assertEqual(CALLS, [])

    def test_provider_down(self):
        ev = run(base_url=f"{self.base}/down/v1", api_key=GOOD, retries=0)
        self.assertEqual(first(ev, "error")["key_status"], "DOWN")

    def test_dead_host_fails_before_any_request(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ev = run(base_url=f"http://127.0.0.1:{port}/v1", api_key=GOOD)
        err = first(ev, "error")
        self.assertEqual(err["key_status"], "DOWN")
        self.assertIn("refused", err["message"])

    def test_placeholder_and_wrong_provider_keys(self):
        # On a public host these fail before any request (on 127.0.0.1 any key is allowed, like Ollama).
        for key in ("YOUR_API_KEY", "sk-xxxxxxxxxxxxxxxx", "sk-••••••••abcd", "short", "<api key>", "https://x.ai"):
            self.assertEqual(first(run(base_url="https://api.example.com/v1", api_key=key), "error")["key_status"], "INVALID", key)
        self.assertEqual(detect.key_problem("https://api.openai.com/v1", ANT)[0], "WRONG_ENDPOINT")
        self.assertEqual(detect.key_problem("https://openrouter.ai/api/v1", "gsk_abcdefghijklmnop123")[0], "WRONG_ENDPOINT")
        self.assertEqual(detect.key_problem("https://api.anthropic.com", "abcdefghijklmnop123")[0], "INVALID")
        self.assertIsNone(detect.key_problem("https://api.anthropic.com", ANT))
        self.assertIsNone(detect.key_problem("https://my-proxy.example/v1", GOOD))
        self.assertIsNone(detect.key_problem("http://localhost:11434/v1", "ollama"))     # local servers take any key
        self.assertIsNone(detect.key_problem("http://192.168.1.5:1234/v1", "lm-studio"))
        self.assertEqual(CALLS, [])

    def test_gateway_key_test_uses_the_real_check(self):
        old = gateway.STORE
        gateway.STORE = None
        try:
            pid = gateway.save_provider({"name": "fake-ant", "base_url": f"127.0.0.1:{self.port}/anthropic/", "format": "anthropic"})
            gateway.add_keys(pid, ANT)
            self.assertEqual(gateway.store().provider(pid)["base_url"], f"http://127.0.0.1:{self.port}/anthropic")
            res = gateway.test_provider_keys(pid, app.TESTER)
            self.assertEqual(res[0]["status"], "ok")
            self.assertEqual(gateway.store().provider(pid)["models"], ["claude-haiku-9", "claude-gone-1"])
            gateway.save_provider({"id": pid, "name": "fake-ant", "base_url": f"{self.base}/anthropic", "format": "openai"})
            res = gateway.test_provider_keys(pid, app.TESTER)
            self.assertEqual(res[0]["status"], "invalid")
            self.assertIn("Anthropic", res[0]["error"])
        finally:
            gateway.STORE = old


# (input, expected) — the Python and the page's JS normalizer must agree on every one.
URL_CASES = [
    ("api.openai.com", "https://api.openai.com"),
    ("  https://api.openai.com/v1/  ", "https://api.openai.com/v1"),
    ("api.example.com/v1/v1", "https://api.example.com/v1"),
    ("https://openrouter.ai/api/v1/chat/completions", "https://openrouter.ai/api/v1"),
    ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1"),
    ("HTTPS://API.Anthropic.COM//", "https://api.anthropic.com"),
    ("localhost:11434/v1", "http://localhost:11434/v1"),
    ("192.168.1.20:8080", "http://192.168.1.20:8080"),
    ("myserver:8080/v1/", "http://myserver:8080/v1"),
    ("example.com:443/v1", "https://example.com:443/v1"),
    ("https:/api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
    ("//api.mistral.ai/v1", "https://api.mistral.ai/v1"),
    ("\"https://api.deepseek.com\"", "https://api.deepseek.com"),
    ("https://api.x.ai/v1/models?x=1#top", "https://api.x.ai/v1"),
    ("https://generativelanguage.googleapis.com/v1beta/openai/", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("", ""),
]
BAD_URLS = ["openai", "ftp://files.example.com", "https://", "http://exa mple..com", "host:99999"]


class Normalize(unittest.TestCase):
    def test_python(self):
        for raw, want in URL_CASES:
            self.assertEqual(detect.normalize_base_url(raw), want, raw)
        for raw in BAD_URLS:
            with self.assertRaises(ValueError, msg=raw):
                detect.normalize_base_url(raw)

    def test_js_matches_python(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        page = app.render_index("en").decode("utf-8")
        js = re.search(r"/\*norm-start\*/(.*?)/\*norm-end\*/", page, re.S).group(1)
        script = ("const T = (k) => k;\n" + js + "\nconst out = {};\n"
                  f"for (const raw of {json.dumps([c[0] for c in URL_CASES])}) out[raw] = normalizeBaseUrl(raw);\n"
                  f"for (const raw of {json.dumps(BAD_URLS)}) {{ try {{ out[raw] = normalizeBaseUrl(raw); }} catch (e) {{ out[raw] = 'ERROR'; }} }}\n"
                  "console.log(JSON.stringify(out));")
        res = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        for raw, want in URL_CASES:
            self.assertEqual(out[raw], want, f"JS: {raw!r}")
        for raw in BAD_URLS:
            self.assertEqual(out[raw], "ERROR", f"JS should reject {raw!r}")


class Genuine(unittest.TestCase):
    def test_same_model(self):
        same = [("gpt-4o", "gpt-4o-2024-08-06"), ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022"),
                ("openai/gpt-4o-mini", "gpt-4o-mini"), ("meta-llama/Llama-3.3-70B-Instruct", "llama-3.3-70b-instruct"),
                ("deepseek/deepseek-chat:free", "deepseek-chat"), ("gemini-1.5-pro", "gemini-1.5-pro-002"),
                ("gpt-5@high", "gpt-5")]
        differ = [("gpt-4o", "gpt-4o-mini"), ("claude-opus-4", "claude-haiku-4"), ("gpt-4o", "llama-3-8b"),
                  ("claude-sonnet-4-5", "claude-sonnet-4")]
        for a, b in same:
            self.assertTrue(detect.same_model(a, b), (a, b))
        for a, b in differ:
            self.assertFalse(detect.same_model(a, b), (a, b))

    def test_cheap_order(self):
        order = detect.cheap_order(["claude-opus-4", "text-embedding-3", "gpt-4o", "gpt-4o-mini", "o3"])
        self.assertEqual(order[0], "gpt-4o-mini")
        self.assertEqual(order[-1], "text-embedding-3")

    def test_router_alias_is_unknown_not_suspicious(self):
        g = detect.genuine("openai", "openrouter/auto", {"model": "gpt-4o", "usage": {}}, {}, "https://openrouter.ai/api/v1")
        self.assertEqual(g["badge"], "unknown")

    def test_claude_via_third_party_without_headers_is_unknown(self):
        res = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5", "content": [],
               "stop_reason": "max_tokens", "usage": {"input_tokens": 1, "output_tokens": 1}}
        self.assertEqual(detect.genuine("anthropic", "claude-haiku-4-5", res, {}, "https://reseller.example")["badge"], "unknown")
        self.assertEqual(detect.genuine("anthropic", "claude-haiku-4-5", res, {}, "https://api.anthropic.com")["badge"], "suspicious")
        self.assertEqual(detect.genuine("anthropic", "claude-haiku-4-5", res, {"request-id": "req_1"},
                                        "https://api.anthropic.com")["badge"], "verified")


if __name__ == "__main__":
    unittest.main()

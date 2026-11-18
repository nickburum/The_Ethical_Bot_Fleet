import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from ethical_bot_fleet import Fleet, LLMError, Policy, ask
from ethical_bot_fleet.cli import main
from ethical_bot_fleet.text import html_to_text

SITE = {
    "/robots.txt": b"User-agent: *\nDisallow: /private/\n",
    "/": b"<html><head><title>Tea Shop</title><style>p{}</style></head>"
         b"<body><script>track()</script><h1>Welcome</h1><p>We sell <b>oolong</b>.</p></body></html>",
    "/more": b"<p>Opening hours: 9 to 5.</p>",
    "/private/x": b"<p>secret</p>",
}


class Handler(BaseHTTPRequestHandler):
    """A tiny website plus a fake OpenAI-compatible and Anthropic-compatible model server."""

    chats = []  # request bodies the "model" received
    tools_supported = True
    odd = None  # a strange reply for /odd

    def log_message(self, *args):
        pass

    def _send(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send(200, {"data": [{"id": "tiny-local-model"}]})
        if self.path.startswith("/elsewhere"):
            Handler.chats.append((self.path, dict(self.headers), None))
            return self._send(200, {"data": [{"id": "stolen"}]})
        if self.path in SITE:
            return self._send(200, SITE[self.path], "text/html")
        self._send(404, b"gone", "text/plain")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.chats.append((self.path, dict(self.headers), body))
        base = "http://127.0.0.1:%d" % self.server.server_port
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", base + "/elsewhere/chat/completions")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if self.path == "/echo/v1/chat/completions":
            return self._send(401, {"error": "bad key: " + self.headers.get("Authorization", "")})
        if self.path == "/odd/v1/chat/completions":
            return self._send(200, {"choices": [{"message": Handler.odd}]})
        if self.path == "/v1/chat/completions":
            if "tools" in body and not Handler.tools_supported:
                return self._send(400, {"error": "this model does not support tools"})
            results = [m for m in body["messages"] if m["role"] == "tool"]
            if "tools" in body and not results:
                calls = [{"id": "c%d" % i, "type": "function",
                          "function": {"name": "fetch_page", "arguments": json.dumps({"url": base + path})}}
                         for i, path in enumerate(("/more", "/private/x"))]
                return self._send(200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}}]})
            seen = " | ".join(m["content"] for m in body["messages"] if m["role"] in ("user", "tool"))
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "ANSWER from: " + seen}}]})
        if self.path.startswith("/v1/messages"):
            results = [b for m in body["messages"] if isinstance(m["content"], list)
                       for b in m["content"] if b.get("type") == "tool_result"]
            if not results:
                content = [{"type": "tool_use", "id": "toolu_1", "name": "fetch_page", "input": {"url": base + "/more"}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "CLAUDE ANSWER from: " + str(results[0]["content"])}]
                stop = "end_turn"
            return self._send(200, {
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        self._send(404, {"error": "no such endpoint"})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.chats.clear()
        Handler.tools_supported = True
        self.bot = Fleet(Policy(block_private_networks=False, min_delay=0)).bot(
            "AcmeBot", "https://acme.example/bot", "ai-input")


class TextTests(unittest.TestCase):
    def test_html_to_text(self):
        text = html_to_text(SITE["/"].decode())
        self.assertEqual(text, "Tea Shop\n\nWelcome\n\nWe sell oolong.")

    def test_main_content_is_preferred_over_menus(self):
        article = "<p>" + "The real article text. " * 12 + "</p>"
        page = "<body><nav><a>Home</a></nav><div>Jump to content</div><main>%s</main><footer>Legal</footer></body>" % article
        text = html_to_text(page)
        self.assertTrue(text.startswith("The real article text."))
        for clutter in ("Home", "Jump to content", "Legal"):
            self.assertNotIn(clutter, text)
        self.assertIn("Legal", html_to_text("<main><p>tiny</p></main><footer>Legal</footer>"))  # too little: keep all

    def test_unclosed_head_and_table_cells(self):
        self.assertEqual(html_to_text("<html><head><title>T</title><body><p>BODY</p>"), "T\n\nBODY")
        self.assertEqual(html_to_text("<table><tr><td>Basic</td><td>$5/month</td></tr></table>"), "Basic $5/month")


class AskTests(Base):
    def test_local_model_is_detected_and_reads_through_the_fleet(self):
        answer = ask(self.bot, "What do they sell?", [self.base + "/"], model="custom", base_url=self.base + "/v1")
        self.assertIn("We sell oolong.", answer)          # the page the user supplied
        self.assertIn("Opening hours: 9 to 5.", answer)    # a page the model asked for
        self.assertIn("Not fetched: robots.txt disallows", answer)  # a page the fleet refused
        self.assertNotIn("secret", answer)

    def test_local_preset_picks_the_loaded_model(self):
        with mock.patch.dict(os.environ, {"EBF_MODEL": "ollama", "EBF_BASE_URL": self.base + "/v1"}):
            ask(self.bot, "hi")
        self.assertEqual(Handler.chats[0][2]["model"], "tiny-local-model")

    def test_model_without_tool_support_still_answers(self):
        Handler.tools_supported = False
        answer = ask(self.bot, "What do they sell?", [self.base + "/"], model="ollama", base_url=self.base + "/v1")
        self.assertIn("We sell oolong.", answer)
        self.assertNotIn("tools", Handler.chats[-1][2])

    def test_api_key_goes_only_in_the_authorization_header(self):
        with mock.patch.dict(os.environ, {"EBF_API_KEY": "sk-test-secret"}):
            ask(self.bot, "hi", model="custom:some-model", base_url=self.base + "/v1")
        path, headers, body = Handler.chats[0]
        self.assertEqual(headers["Authorization"], "Bearer sk-test-secret")
        self.assertNotIn("sk-test-secret", json.dumps(body))

    def test_key_never_follows_a_redirect(self):
        with mock.patch.dict(os.environ, {"EBF_API_KEY": "sk-test-secret"}):
            with self.assertRaises(LLMError) as caught:
                ask(self.bot, "hi", model="custom:m", base_url=self.base + "/redirect/v1")
        self.assertIn("redirect", str(caught.exception))
        self.assertFalse([c for c in Handler.chats if c[0].startswith("/elsewhere")])

    def test_provider_key_only_goes_to_the_provider(self):
        env = {"OPENAI_API_KEY": "sk-openai-secret", "EBF_BASE_URL": self.base + "/v1"}
        with mock.patch.dict(os.environ, env):
            ask(self.bot, "hi", model="openai:m", base_url=self.base + "/v1")  # explicitly moved: no provider key
            self.assertNotIn("Authorization", Handler.chats[-1][1])
            Handler.chats.clear()
            with self.assertRaises(LLMError):  # EBF_BASE_URL alone must not move a cloud preset
                ask(self.bot, "hi", model="openai:m")
            self.assertEqual(Handler.chats, [])

    def test_key_echoed_by_the_server_is_hidden(self):
        with mock.patch.dict(os.environ, {"EBF_API_KEY": "sk-test-secret"}):
            with self.assertRaises(LLMError) as caught:
                ask(self.bot, "hi", model="custom:m", base_url=self.base + "/echo/v1")
        self.assertNotIn("sk-test-secret", str(caught.exception))

    def test_strange_replies_are_clean_errors(self):
        for odd in (None, "text", {"content": 5}, {"content": None}, {"tool_calls": {"a": 1}},
                    {"tool_calls": ["x"]}, {"tool_calls": [{"function": "f"}]}):
            Handler.odd = odd
            with self.assertRaises(LLMError, msg=repr(odd)):
                ask(self.bot, "hi", model="custom:m", base_url=self.base + "/odd/v1", max_steps=2)
        Handler.odd = {"content": [{"type": "text", "text": "in parts"}]}
        self.assertEqual(ask(self.bot, "hi", model="custom:m", base_url=self.base + "/odd/v1"), "in parts")

    def test_claude_refuses_base_url(self):
        with self.assertRaises(LLMError) as caught:
            ask(self.bot, "hi", model="claude", base_url=self.base)
        self.assertIn("ANTHROPIC_BASE_URL", str(caught.exception))

    def test_claude_without_credentials_is_a_clean_error(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            self.skipTest("anthropic SDK not installed")
        with mock.patch.dict(os.environ, {"HOME": "/nonexistent"}, clear=True):
            with self.assertRaises(LLMError) as caught:
                ask(self.bot, "hi", model="claude")
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))

    def test_helpful_errors(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for kwargs, expected in (
                ({}, "No model chosen"),
                ({"model": "nope"}, "Unknown model preset"),
                ({"model": "openai"}, "Name a model"),
                ({"model": "openai:some-model"}, "OPENAI_API_KEY"),
                ({"model": "custom:m"}, "--base-url"),
                ({"model": "ollama", "base_url": "http://127.0.0.1:9/v1"}, "Could not reach"),
            ):
                with self.assertRaises(LLMError) as caught:
                    ask(self.bot, "hi", **kwargs)
                self.assertIn(expected, str(caught.exception))

    def test_claude_preset_uses_the_sdk(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            self.skipTest("anthropic SDK not installed")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": self.base}
        with mock.patch.dict(os.environ, env):
            answer = ask(self.bot, "When are they open?", model="claude")
        self.assertEqual(answer, "CLAUDE ANSWER from: Opening hours: 9 to 5.")
        path, headers, body = Handler.chats[0]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["tools"][0]["name"], "fetch_page")


class CliTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_read_needs_no_model(self):
        code, out, _ = self.run_cli("read", "--allow-private", self.base + "/")
        self.assertEqual((code, out), (0, "Tea Shop\n\nWelcome\n\nWe sell oolong.\n"))

    def test_ask(self):
        code, out, err = self.run_cli("ask", "What do they sell?", self.base + "/", "--allow-private", "--delay", "0",
                                      "--model", "ollama", "--base-url", self.base + "/v1")
        self.assertEqual(code, 0)
        self.assertIn("We sell oolong.", out)
        self.assertIn("asking Ollama (tiny-local-model)", err)

    def test_ask_without_a_model_explains_what_to_do(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = self.run_cli("ask", "hi")
        self.assertEqual(code, 2)
        self.assertIn("ebf models", err)

    def test_models_lists_presets(self):
        code, out, _ = self.run_cli("models")
        self.assertEqual(code, 0)
        for name in ("claude", "openai", "gemini", "ollama", "lmstudio", "custom"):
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main()

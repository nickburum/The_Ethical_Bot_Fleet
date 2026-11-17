import contextlib
import io
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ethical_bot_fleet.cli import main

PAGES = {
    "/robots.txt": (200, b"User-agent: *\nDisallow: /private/\n"),
    "/": (200, b"<p>home</p>"),
    "/private/x": (200, b"secret"),
}


class Handler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get("User-Agent", "")))
        status, body = PAGES.get(self.path, (404, b"gone"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CliTests(unittest.TestCase):
    """End to end over real sockets, using the default urllib transport."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(args))
        return code, out.getvalue()

    def test_check(self):
        self.assertEqual(self.run_cli("check", self.base + "/")[0], 3)  # localhost is refused by default
        code, out = self.run_cli("check", "--allow-private", self.base + "/", self.base + "/private/x")
        self.assertEqual(code, 3)
        self.assertIn("ALLOW  " + self.base + "/\n", out)
        self.assertIn("rule: Disallow: /private/ (line 2)", out)

    def test_links_handles_placeholders_brackets_and_dead_hosts(self):
        with tempfile.TemporaryDirectory() as folder:
            readme = os.path.join(folder, "README.md")
            with open(readme, "w") as handle:
                handle.write("(see %s/wiki_(page)) and http://host:port/v1 and http://127.0.0.1:9/dead\n" % self.base)
            code, out = self.run_cli("links", "-v", "--allow-private", "--delay", "0", readme)
            self.assertEqual(code, 1)
            self.assertIn("%s/wiki_(page)  (404)" % self.base, out)
            self.assertIn("BROKEN   http://host:port/v1", out)
            self.assertIn("BROKEN   http://127.0.0.1:9/dead", out)
            self.assertIn("3 links: 0 ok, 3 broken, 0 skipped", out)

    def test_bad_input_never_shows_a_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            junk = os.path.join(folder, "junk.txt")
            with open(junk, "w") as handle:
                handle.write("hello\n")
            self.assertEqual(self.run_cli("audit", "summary", junk)[0], 1)
            self.assertEqual(self.run_cli("audit", "verify", junk)[0], 1)
            self.assertEqual(self.run_cli("check", "--audit-log", junk, self.base + "/")[0], 1)
        self.assertEqual(self.run_cli("read", "--allow-private", self.base + "/a b\u00e9")[0], 1)  # a clean 404
        self.assertEqual(self.run_cli("check", "http://host:port/")[0], 3)

    def test_links_and_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            readme, log = os.path.join(folder, "README.md"), os.path.join(folder, "audit.jsonl")
            with open(readme, "w") as handle:
                handle.write("[home](%s/) and [dead](%s/missing), see %s/private/x.\n" % ((self.base,) * 3))
            code, out = self.run_cli("links", "--allow-private", "--delay", "0", "--audit-log", log, readme)
            self.assertEqual(code, 1)
            self.assertIn("BROKEN   %s/missing  (404)" % self.base, out)
            self.assertIn("skipped       %s/private/x  (robots_disallow)" % self.base, out)
            self.assertIn("3 links: 1 ok, 1 broken, 1 skipped", out)
            self.assertNotIn("/private/x", [path for path, _ in Handler.seen])
            self.assertTrue(all(agent.startswith("EthicalFleetCLI/") for _, agent in Handler.seen))
            self.assertEqual(self.run_cli("audit", "verify", log)[0], 0)
            self.assertIn("robots_disallow", self.run_cli("audit", "summary", log)[1])


if __name__ == "__main__":
    unittest.main()

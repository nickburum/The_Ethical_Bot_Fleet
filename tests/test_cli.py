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

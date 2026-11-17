import os
import subprocess
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class PrivacyTests(unittest.TestCase):
    def test_commands_leave_no_files_behind(self):
        """Run the CLI with an empty home and working directory; nothing may be written."""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "USERPROFILE": home,
                   "XDG_CONFIG_HOME": os.path.join(home, ".config"), "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                   "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
            for command in (
                ["models"],
                ["check", "http://127.0.0.1:9/"],
                ["read", "--allow-private", "http://127.0.0.1:9/"],
                ["ask", "hi", "--model", "ollama", "--base-url", "http://127.0.0.1:9/v1"],
            ):
                subprocess.run([sys.executable, "-m", "ethical_bot_fleet"] + command, cwd=cwd, env=env,
                               capture_output=True, timeout=60)
            self.assertEqual(os.listdir(home), [])
            self.assertEqual(os.listdir(cwd), [])

    def test_source_only_names_the_documented_hosts(self):
        """Every hard-coded address in the package is a model preset or the project page."""
        import re

        allowed = {
            "api.openai.com", "generativelanguage.googleapis.com", "api.mistral.ai", "api.groq.com",
            "api.deepseek.com", "api.x.ai", "openrouter.ai", "api.together.xyz", "localhost", "github.com",
        }
        package = os.path.join(SRC, "ethical_bot_fleet")
        found = set()
        for name in os.listdir(package):
            if name.endswith(".py"):
                with open(os.path.join(package, name), encoding="utf-8") as handle:
                    found |= set(re.findall(r"https?://([A-Za-z0-9.-]+)", handle.read()))
        found -= {"host", "example.com"}  # placeholders in help text
        self.assertEqual(found - allowed, set())


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest

from ethical_bot_fleet import Blocked, FetchError, Fleet, IdentityError, Policy, verify_audit_log
from ethical_bot_fleet.transport import RawResponse


class FakeWeb:
    """An in-memory internet: {url: (status, headers, body)} or a list of them."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, url, headers, timeout, max_bytes):
        self.requests.append((url, headers))
        page = self.pages.get(url)
        if page is None:
            return RawResponse(404, {}, b"")
        if isinstance(page, list):
            page = page.pop(0) if len(page) > 1 else page[0]
        if isinstance(page, Exception):
            raise page
        status, head, body = page
        return RawResponse(status, dict(head), body)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def make(pages, audit_log=None, **policy):
    policy.setdefault("block_private_networks", False)
    web, clock = FakeWeb(pages), Clock()
    fleet = Fleet(Policy(**policy), audit_log=audit_log, transport=web, clock=clock, sleep=clock.sleep)
    return fleet, web, clock


ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 3\n"
HTML = {"content-type": "text/html; charset=utf-8"}


class IdentityTests(unittest.TestCase):
    def test_user_agent_is_honest(self):
        fleet, _, _ = make({})
        bot = fleet.bot("AcmeBot", "https://acme.example/bot", "ai-input", version="2.1")
        self.assertTrue(bot.user_agent.startswith("AcmeBot/2.1 (+https://acme.example/bot; purpose=ai-input)"))

    def test_rejects_dishonest_identities(self):
        fleet, _, _ = make({})
        for name, contact, purpose in (
            ("Mozilla", "https://a.example", "other"),
            ("MyGooglebot", "https://a.example", "other"),
            ("Acme Bot", "https://a.example", "other"),
            ("AcmeBot", "nobody", "other"),
            ("AcmeBot", "https://a.example", "world-domination"),
        ):
            with self.assertRaises(IdentityError):
                fleet.bot(name, contact, purpose)

    def test_email_contact_is_accepted(self):
        fleet, _, _ = make({})
        fleet.bot("AcmeBot", "bots@acme.example")


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.fleet, self.web, self.clock = make({
            "https://site.example/robots.txt": (200, {}, ROBOTS),
            "https://site.example/": (200, HTML, b"<p>hello</p>"),
            "https://site.example/private/x": (200, HTML, b"secret"),
        })
        self.bot = self.fleet.bot("AcmeBot", "https://acme.example/bot")

    def test_allowed_page_is_fetched_with_our_user_agent(self):
        response = self.bot.get("https://site.example/")
        self.assertEqual(response.text, "<p>hello</p>")
        self.assertTrue(all(h["User-Agent"] == self.bot.user_agent for _, h in self.web.requests))

    def test_disallowed_page_is_never_requested(self):
        with self.assertRaises(Blocked) as caught:
            self.bot.get("https://site.example/private/x")
        self.assertEqual(caught.exception.decision.reason, "robots_disallow")
        self.assertNotIn("https://site.example/private/x", [u for u, _ in self.web.requests])

    def test_robots_is_fetched_once(self):
        self.bot.check("https://site.example/")
        self.bot.check("https://site.example/private/x")
        self.assertEqual(len(self.web.requests), 1)

    def test_bad_urls(self):
        for url in ("ftp://site.example/", "file:///etc/passwd", "https://user:pw@site.example/"):
            self.assertEqual(self.bot.check(url).reason, "bad_url")

    def test_check_does_not_fetch_the_page(self):
        self.assertTrue(self.bot.check("https://site.example/"))
        self.assertEqual([u for u, _ in self.web.requests], ["https://site.example/robots.txt"])


class RobotsAvailabilityTests(unittest.TestCase):
    def test_missing_robots_means_allowed(self):
        fleet, _, _ = make({"https://a.example/": (200, {}, b"ok")})
        self.assertTrue(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/"))

    def test_server_error_on_robots_means_off_limits(self):
        fleet, _, _ = make({"https://a.example/robots.txt": (503, {}, b"")})
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/").reason, "robots_unreachable")

    def test_network_failure_on_robots_means_off_limits(self):
        fleet, _, _ = make({"https://a.example/robots.txt": OSError("connection refused")})
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/").reason, "robots_unreachable")

    def test_robots_redirect_is_followed(self):
        fleet, _, _ = make({
            "https://a.example/robots.txt": (301, {"location": "https://www.a.example/robots.txt"}, b""),
            "https://www.a.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /\n"),
        })
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/x").reason, "robots_disallow")


class PolicyTests(unittest.TestCase):
    def test_domain_lists(self):
        fleet, _, _ = make({}, allowed_domains=["good.example"], blocked_domains=["bad.good.example"])
        bot = fleet.bot("AcmeBot", "https://a.example")
        self.assertTrue(bot.check("https://www.good.example/"))
        self.assertEqual(bot.check("https://other.example/").reason, "domain_not_allowed")
        self.assertEqual(bot.check("https://bad.good.example/").reason, "domain_blocked")
        self.assertEqual(bot.check("https://notgood.example/").reason, "domain_not_allowed")

    def test_private_addresses_are_refused(self):
        web = FakeWeb({})
        fleet = Fleet(transport=web, resolver=lambda host: ["10.0.0.5"] if host == "intranet.example" else ["93.184.216.34"])
        bot = fleet.bot("AcmeBot", "https://a.example")
        for url in ("http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data", "http://intranet.example/"):
            self.assertEqual(bot.check(url).reason, "private_network", url)
        self.assertEqual(web.requests, [])
        self.assertTrue(bot.check("https://public.example/"))

    def test_trusted_domain_skips_robots_and_is_audited(self):
        fleet, web, _ = make({
            "https://mine.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /\n"),
            "https://mine.example/x": (200, {}, b"ok"),
        }, trusted_domains=["mine.example"])
        self.assertTrue(fleet.bot("AcmeBot", "https://a.example").get("https://mine.example/x").ok)
        self.assertEqual(len(web.requests), 1)
        self.assertTrue(fleet.audit.records[0]["trusted"])

    def test_host_budget(self):
        fleet, _, _ = make({"https://a.example/1": (200, {}, b"1"), "https://a.example/2": (200, {}, b"2")},
                           max_requests_per_host=1)
        bot = fleet.bot("AcmeBot", "https://a.example")
        bot.get("https://a.example/1")
        with self.assertRaises(Blocked) as caught:
            bot.get("https://a.example/2")
        self.assertEqual(caught.exception.decision.reason, "host_budget")

    def test_excessive_crawl_delay_skips_the_host(self):
        fleet, _, _ = make({"https://a.example/robots.txt": (200, {}, b"User-agent: *\nCrawl-delay: 600\n")})
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/").reason, "crawl_delay_too_long")


class PolitenessTests(unittest.TestCase):
    def test_crawl_delay_is_shared_across_bots(self):
        fleet, _, clock = make({
            "https://site.example/robots.txt": (200, {}, ROBOTS),
            "https://site.example/a": (200, {}, b"a"),
            "https://site.example/b": (200, {}, b"b"),
            "https://other.example/": (200, {}, b"o"),
        })
        one = fleet.bot("AcmeBot", "https://a.example")
        two = fleet.bot("OtherBot", "https://a.example")
        one.get("https://site.example/a")
        two.get("https://other.example/")  # a different host does not wait
        self.assertEqual(clock.sleeps, [])
        two.get("https://site.example/b")  # the same host waits for Crawl-delay: 3
        self.assertEqual(clock.sleeps, [3.0])

    def test_retry_after_is_honoured(self):
        fleet, web, clock = make({
            "https://a.example/": [(429, {"retry-after": "7"}, b""), (200, {}, b"ok")],
        })
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").get("https://a.example/").body, b"ok")
        self.assertEqual(clock.sleeps, [7.0])

    def test_long_retry_after_backs_off(self):
        fleet, web, clock = make({"https://a.example/": (503, {"retry-after": "86400"}, b"")})
        with self.assertRaises(Blocked) as caught:
            fleet.bot("AcmeBot", "https://a.example").get("https://a.example/")
        self.assertEqual(caught.exception.decision.reason, "retry_after_too_long")
        self.assertEqual(clock.sleeps, [])

    def test_retries_are_bounded(self):
        fleet, web, _ = make({"https://a.example/": (503, {}, b"")}, max_retries=2)
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").get("https://a.example/").status, 503)
        self.assertEqual(len([u for u, _ in web.requests if u == "https://a.example/"]), 3)

    def test_network_failure_raises_fetch_error(self):
        fleet, _, _ = make({"https://a.example/": OSError("timed out")})
        with self.assertRaises(FetchError):
            fleet.bot("AcmeBot", "https://a.example").get("https://a.example/")


class RedirectTests(unittest.TestCase):
    def test_redirect_target_is_checked_against_its_own_robots(self):
        fleet, web, _ = make({
            "https://a.example/go": (302, {"location": "https://b.example/secret"}, b""),
            "https://b.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /secret\n"),
            "https://b.example/secret": (200, {}, b"nope"),
        })
        with self.assertRaises(Blocked) as caught:
            fleet.bot("AcmeBot", "https://a.example").get("https://a.example/go")
        self.assertEqual(caught.exception.decision.reason, "robots_disallow")
        self.assertNotIn("https://b.example/secret", [u for u, _ in web.requests])

    def test_redirect_history_and_relative_location(self):
        fleet, _, _ = make({
            "https://a.example/old": (301, {"location": "/new"}, b""),
            "https://a.example/new": (200, {}, b"new"),
        })
        response = fleet.bot("AcmeBot", "https://a.example").get("https://a.example/old")
        self.assertEqual((response.url, response.history), ("https://a.example/new", ["https://a.example/old"]))

    def test_redirect_loops_end(self):
        fleet, _, _ = make({"https://a.example/loop": (302, {"location": "/loop"}, b"")}, min_delay=0)
        with self.assertRaises(Blocked) as caught:
            fleet.bot("AcmeBot", "https://a.example").get("https://a.example/loop")
        self.assertEqual(caught.exception.decision.reason, "too_many_redirects")


class AiSignalTests(unittest.TestCase):
    PAGES = {
        "https://a.example/robots.txt": (200, {}, b"User-agent: *\nContent-Signal: search=yes, ai-train=no\nAllow: /\n"),
        "https://a.example/": (200, HTML, b"<p>hi</p>"),
        "https://b.example/header": (200, {"x-robots-tag": "noai, noindex"}, b"x"),
        "https://b.example/meta": (200, HTML, b'<html><head><meta name="robots" content="NoAI"></head></html>'),
        "https://b.example/tdm": (200, {"tdm-reservation": "1"}, b"x"),
        "https://b.example/scoped": (200, {"x-robots-tag": "otherbot: noai"}, b"x"),
        "https://b.example/mine": (200, {"x-robots-tag": "acmebot: noai"}, b"x"),
    }

    def bot(self, purpose):
        fleet, web, _ = make(dict(self.PAGES), min_delay=0)
        return fleet.bot("AcmeBot", "https://a.example", purpose), web

    def test_content_signal_blocks_training_before_any_fetch(self):
        bot, web = self.bot("ai-train")
        self.assertEqual(bot.check("https://a.example/").reason, "content_signal")
        self.assertTrue(self.bot("search")[0].check("https://a.example/"))
        self.assertTrue(self.bot("ai-input")[0].check("https://a.example/"))

    def test_noai_blocks_ai_purposes_only(self):
        for path in ("header", "meta"):
            for purpose in ("ai-train", "ai-input"):
                with self.assertRaises(Blocked) as caught:
                    self.bot(purpose)[0].get("https://b.example/" + path)
                self.assertEqual(caught.exception.decision.reason, "page_opt_out")
        response = self.bot("search")[0].get("https://b.example/header")
        self.assertEqual(response.directives, {"noai", "noindex"})

    def test_tdm_reservation_blocks_training(self):
        with self.assertRaises(Blocked):
            self.bot("ai-train")[0].get("https://b.example/tdm")
        self.assertTrue(self.bot("ai-input")[0].get("https://b.example/tdm").ok)

    def test_agent_scoped_header(self):
        self.assertTrue(self.bot("ai-train")[0].get("https://b.example/scoped").ok)
        with self.assertRaises(Blocked):
            self.bot("ai-train")[0].get("https://b.example/mine")


class CrawlTests(unittest.TestCase):
    def test_crawl_reports_every_url_once(self):
        fleet, _, _ = make({
            "https://a.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /no\n"),
            "https://a.example/1": (200, {}, b"1"),
            "https://a.example/no": (200, {}, b"x"),
            "https://b.example/": OSError("down"),
            "https://b.example/robots.txt": (404, {}, b""),
        }, min_delay=0)
        urls = ["https://a.example/1", "https://a.example/no", "https://b.example/", "https://a.example/1"]
        results = {r.url: r for r in fleet.bot("AcmeBot", "https://a.example").crawl(urls, workers=4)}
        self.assertEqual(len(results), 3)
        self.assertEqual(results["https://a.example/1"].response.body, b"1")
        self.assertTrue(results["https://a.example/no"].blocked)
        self.assertIsInstance(results["https://b.example/"].error, FetchError)


class AuditTests(unittest.TestCase):
    def test_log_is_chained_and_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            pages = {"https://a.example/robots.txt": (200, {}, ROBOTS), "https://a.example/": (200, {}, b"ok")}
            for _ in range(2):  # a second fleet continues the same chain
                fleet, _, _ = make(dict(pages), audit_log=path)
                bot = fleet.bot("AcmeBot", "https://a.example")
                bot.get("https://a.example/")
                bot.check("https://a.example/private/x")
            ok, count, _ = verify_audit_log(path)
            self.assertEqual((ok, count), (True, 6))

            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            self.assertEqual([json.loads(line)["event"] for line in lines[:3]], ["allow", "fetch", "block"])

            edited = json.loads(lines[2])
            edited["event"] = "allow"
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines[:2] + [json.dumps(edited) + "\n"] + lines[3:])
            ok, _, message = verify_audit_log(path)
            self.assertFalse(ok)
            self.assertIn("line 3", message)

            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines[:1] + lines[2:])
            self.assertFalse(verify_audit_log(path)[0])


if __name__ == "__main__":
    unittest.main()

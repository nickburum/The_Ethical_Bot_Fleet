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
            ("Chrome", "https://a.example", "other"),
            ("curl-8", "https://a.example", "other"),
            ("Acme Bot", "https://a.example", "other"),
            ("AcmeBot", "nobody", "other"),
            ("AcmeBot", "https://a.example", "world-domination"),
        ):
            with self.assertRaises(IdentityError):
                fleet.bot(name, contact, purpose)

    def test_other_crawlers_and_smuggled_user_agents_are_refused(self):
        fleet, _, _ = make({})
        for name in ("GoogleOther", "Applebot", "PerplexityBot", "ChatGPT-User", "Claude-Web", "facebookexternalhit"):
            with self.assertRaises(IdentityError, msg=name):
                fleet.bot(name, "https://a.example")
        with self.assertRaises(IdentityError):
            fleet.bot("AcmeBot", "https://a.example", version="5.0 (Windows NT 10.0) Chrome/120 Googlebot/2.1")
        with self.assertRaises(IdentityError):
            fleet.bot("AcmeBot", "https://x.example) Mozilla/5.0 (X11")
        with self.assertRaises(IdentityError):
            fleet.bot("AcmeBot", "https://a.example", version="1.0\r\nX-Evil: 1")

    def test_innocent_names_are_accepted(self):
        fleet, _, _ = make({})
        for name in ("KnowledgeBot", "OperationsBot", "LedgerBot", "ParkGuideBot"):
            fleet.bot(name, "https://a.example")
        with self.assertRaises(IdentityError):
            fleet.bot("AcmeBot", "http://")

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

    def test_malformed_urls_are_refused_not_crashed_on(self):
        for url in ("http://host:port/v1", "example.com", "http://[bad/"):
            self.assertEqual(self.bot.check(url).reason, "bad_url", url)
        self.assertIn("https://example.com", self.bot.check("example.com").detail)

    def test_spaces_and_accents_are_encoded(self):
        self.web.pages["https://site.example/caf%C3%A9%20menu"] = (200, HTML, b"ok")
        self.assertEqual(self.bot.get("https://site.example/caf\u00e9 menu").body, b"ok")

    def test_transport_rejecting_a_url_is_a_fetch_error(self):
        import http.client
        self.web.pages["https://site.example/odd"] = http.client.InvalidURL("nope")
        with self.assertRaises(FetchError):
            self.bot.get("https://site.example/odd")

    def test_path_tricks_do_not_get_past_robots(self):
        for path in ("/a/../private/x", "/./private/x", "//private/x", "/a/%2e%2e/private/x", "/a/%2E%2E//private/x"):
            self.assertEqual(self.bot.check("https://site.example" + path).reason, "robots_disallow", path)
        self.assertTrue(self.bot.check("https://site.example/a/../public"))

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

    def test_dead_site_is_reported_as_unreachable_not_as_a_refusal(self):
        fleet, _, _ = make({"https://a.example/robots.txt": OSError("connection refused")})
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/").reason, "site_unreachable")

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

    def test_one_host_spelled_many_ways_is_one_host(self):
        spellings = ["http://127.0.0.1/a", "http://127.0.0.1:80/b", "http://127.0.0.1:080/c", "http://127.1/d",
                     "http://2130706433/e", "http://0x7f.0.0.1/f", "http://127.0.0.1./g"]
        pages = {url: (200, {}, b"x") for url in spellings}
        fleet, web, _ = make(pages, max_requests_per_host=2, min_delay=0)
        results = list(fleet.bot("AcmeBot", "https://a.example").crawl(spellings, workers=8))
        self.assertEqual(sum(1 for r in results if r.response), 2)
        self.assertEqual(len([u for u, _ in web.requests if u.endswith("robots.txt")]), 1)

    def test_blocked_domain_with_trailing_dot(self):
        fleet, web, _ = make({}, blocked_domains=["bad.example"])
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("http://bad.example./x").reason, "domain_blocked")
        self.assertEqual(web.requests, [])

    def test_policy_rejects_nonsense_numbers(self):
        for bad in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                Policy(min_delay=bad)

    def test_host_budget_holds_under_concurrency(self):
        pages = {"https://a.example/%d" % i: (200, {}, b"x") for i in range(40)}
        fleet, web, _ = make(pages, max_requests_per_host=5, min_delay=0)
        results = list(fleet.bot("AcmeBot", "https://a.example").crawl(pages, workers=16))
        self.assertEqual(sum(1 for r in results if r.response), 5)
        self.assertEqual(sum(1 for r in results if r.blocked), 35)
        self.assertEqual(len([u for u, _ in web.requests if not u.endswith("robots.txt")]), 5)

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
        two.get("https://other.example/")  # a different host does not wait for site.example
        # each first page waits one polite second after that site's robots.txt request
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        two.get("https://site.example/b")  # the same host waits for Crawl-delay: 3
        self.assertEqual(clock.sleeps, [1.0, 1.0, 2.0])  # 3 seconds after the last request to that host

    def test_retry_after_is_honoured(self):
        fleet, web, clock = make({
            "https://a.example/": [(429, {"retry-after": "7"}, b""), (200, {}, b"ok")],
        })
        self.assertEqual(fleet.bot("AcmeBot", "https://a.example").get("https://a.example/").body, b"ok")
        self.assertEqual(clock.sleeps, [1.0, 7.0])

    def test_long_retry_after_backs_off(self):
        fleet, web, clock = make({"https://a.example/": (503, {"retry-after": "86400"}, b"")})
        with self.assertRaises(Blocked) as caught:
            fleet.bot("AcmeBot", "https://a.example").get("https://a.example/")
        self.assertEqual(caught.exception.decision.reason, "retry_after_too_long")
        self.assertEqual(clock.sleeps, [1.0])

    def test_retry_after_binds_the_host_even_without_a_retry(self):
        fleet, _, clock = make({"https://a.example/1": (429, {"retry-after": "9"}, b""),
                                "https://a.example/2": (200, {}, b"ok")}, max_retries=0)
        bot = fleet.bot("AcmeBot", "https://a.example")
        self.assertEqual(bot.get("https://a.example/1").status, 429)
        bot.get("https://a.example/2")
        self.assertEqual(clock.sleeps, [1.0, 9.0])

    def test_long_break_puts_the_whole_site_off_limits(self):
        fleet, web, clock = make({"https://a.example/1": (503, {"retry-after": "3600"}, b""),
                                  "https://a.example/2": (200, {}, b"ok")})
        bot, other = fleet.bot("AcmeBot", "https://a.example"), fleet.bot("OtherBot", "https://a.example")
        with self.assertRaises(Blocked):
            bot.get("https://a.example/1")
        self.assertEqual(other.check("https://a.example/2").reason, "host_backoff")
        self.assertNotIn("https://a.example/2", [u for u, _ in web.requests])
        clock.now += 3601
        self.assertTrue(other.get("https://a.example/2").ok)

    def test_slow_drip_server_cannot_hold_a_bot(self):
        import threading
        release = threading.Event()

        def stuck(url, headers, timeout, max_bytes):
            release.wait(30)
            return RawResponse(200, {}, b"")

        fleet = Fleet(Policy(block_private_networks=False, max_time=0.2), transport=stuck)
        bot = fleet.bot("AcmeBot", "https://a.example")
        try:
            self.assertEqual(bot.check("https://a.example/").reason, "site_unreachable")
        finally:
            release.set()

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

    def test_robots_redirect_gets_the_same_checks_as_any_address(self):
        for target in ("https://blocked.example/robots.txt", "ftp://a.example/robots.txt", "file:///etc/hostname"):
            fleet, web, _ = make({"https://a.example/robots.txt": (302, {"location": target}, b"")},
                                 blocked_domains=["blocked.example"])
            self.assertEqual(fleet.bot("AcmeBot", "https://a.example").check("https://a.example/").reason,
                             "robots_unreachable", target)
            self.assertEqual([u for u, _ in web.requests], ["https://a.example/robots.txt"])

    def test_odd_redirect_targets_are_handled(self):
        fleet, _, _ = make({"https://a.example/1": (302, {"location": "/caf\u00e9 x"}, b""),
                            "https://a.example/caf%C3%A9%20x": (200, {}, b"ok"),
                            "https://a.example/2": (302, {"location": "http://h:notaport/"}, b"")}, min_delay=0)
        bot = fleet.bot("AcmeBot", "https://a.example")
        self.assertEqual(bot.get("https://a.example/1").body, b"ok")
        with self.assertRaises(Blocked) as caught:
            bot.get("https://a.example/2")
        self.assertEqual(caught.exception.decision.reason, "bad_url")

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

    def test_noai_meta_is_found_however_the_page_is_served(self):
        tag = '<html><head><meta name="robots" content="noai"></head><body>text</body></html>'
        pages = {
            "https://b.example/no-type": (200, {}, tag.encode()),
            "https://b.example/utf16": (200, {"content-type": "text/html; charset=utf-16"}, tag.encode("utf-16")),
            "https://b.example/late": (200, HTML, b"<html><body>" + b"<p>filler</p>" * 40000 + tag.encode()),
            "https://b.example/xml": (200, {"content-type": "application/xhtml+xml"}, tag.encode()),
        }
        for url in pages:
            fleet, _, _ = make(dict(pages), min_delay=0)
            with self.assertRaises(Blocked, msg=url):
                fleet.bot("AcmeBot", "https://a.example", "ai-input").get(url)

    def test_opt_out_spellings(self):
        cases = {
            "two-lines": {"x-robots-tag": "googlebot: noindex\nnoai"},
            "spaces": {"x-robots-tag": "noai noimageai"},
            "semicolon": {"x-robots-tag": "noai;"},
        }
        for name, headers in cases.items():
            fleet, _, _ = make({"https://b.example/x": (200, headers, b"x")}, min_delay=0)
            with self.assertRaises(Blocked, msg=name):
                fleet.bot("AcmeBot", "https://a.example", "ai-input").get("https://b.example/x")
        for value in ("1, 1", '"1"'):
            fleet, _, _ = make({"https://b.example/x": (200, {"tdm-reservation": value}, b"x")}, min_delay=0)
            with self.assertRaises(Blocked, msg=value):
                fleet.bot("AcmeBot", "https://a.example", "ai-train").get("https://b.example/x")
        page = b'<meta name="robots" content="noai noimageai"><meta name="robots" content="index" content="noai">'
        fleet, _, _ = make({"https://b.example/x": (200, HTML, page)}, min_delay=0)
        with self.assertRaises(Blocked):
            fleet.bot("AcmeBot", "https://a.example", "ai-train").get("https://b.example/x")

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
            self.assertEqual((ok, count), (True, 8))  # robots, allow, fetch, block, once per fleet

            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            self.assertEqual([json.loads(line)["event"] for line in lines[:4]], ["robots", "allow", "fetch", "block"])

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

            # Cutting records off the end leaves a valid chain; the saved last hash catches it.
            head = json.loads(lines[-1])["hash"]
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
            self.assertTrue(verify_audit_log(path, head)[0])
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines[:-2])
            self.assertTrue(verify_audit_log(path)[0])
            self.assertFalse(verify_audit_log(path, head)[0])
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o077, 0)

    def test_two_fleets_can_share_one_log(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            one, _, _ = make({}, audit_log=path)
            two, _, _ = make({}, audit_log=path)
            for fleet in (one, two, one, two, one):
                fleet.bot("AcmeBot", "https://a.example").check("ftp://nope")
            self.assertEqual(verify_audit_log(path)[:2], (True, 5))

    def test_damaged_logs_give_clear_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            with open(path, "w") as handle:
                handle.write("[1, 2]\nnot json\n")
            self.assertFalse(verify_audit_log(path)[0])
            with self.assertRaises(ValueError):
                Fleet(audit_log=path)


if __name__ == "__main__":
    unittest.main()

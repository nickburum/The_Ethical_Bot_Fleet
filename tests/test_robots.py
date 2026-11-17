import unittest

from ethical_bot_fleet import RobotsFile


def allowed(text, path, agent="AcmeBot"):
    return RobotsFile.parse(text).evaluate(agent, "https://site.example" + path).allowed


class RobotsTests(unittest.TestCase):
    def test_empty_file_allows_everything(self):
        self.assertTrue(allowed("", "/anything"))

    def test_longest_match_wins(self):
        text = "User-agent: *\nDisallow: /shop\nAllow: /shop/public\nDisallow: /shop/public/hidden\n"
        self.assertFalse(allowed(text, "/shop/cart"))
        self.assertTrue(allowed(text, "/shop/public/item"))
        self.assertFalse(allowed(text, "/shop/public/hidden/x"))

    def test_allow_wins_a_tie(self):
        self.assertTrue(allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page"))

    def test_wildcards_and_end_anchor(self):
        text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*/cache\n"
        self.assertFalse(allowed(text, "/docs/report.pdf"))
        self.assertTrue(allowed(text, "/docs/report.pdf?download=1"))
        self.assertTrue(allowed(text, "/docs/report.pdfx"))
        self.assertFalse(allowed(text, "/tmp123/cache/x"))

    def test_query_strings_are_matched(self):
        text = "User-agent: *\nDisallow: /search?q=\n"
        self.assertFalse(allowed(text, "/search?q=cats"))
        self.assertTrue(allowed(text, "/search"))

    def test_specific_group_replaces_the_wildcard_group(self):
        text = "User-agent: *\nDisallow: /\n\nUser-agent: acmebot\nDisallow: /admin\n"
        self.assertTrue(allowed(text, "/page"))
        self.assertFalse(allowed(text, "/admin"))
        self.assertFalse(allowed(text, "/page", agent="SomeoneElse"))

    def test_agent_match_is_case_insensitive(self):
        self.assertFalse(allowed("User-Agent: ACMEBOT\nDisallow: /\n", "/"))

    def test_shared_and_repeated_groups(self):
        text = "User-agent: a\nUser-agent: acmebot\nDisallow: /one\n\nUser-agent: acmebot\nDisallow: /two\n"
        self.assertFalse(allowed(text, "/one"))
        self.assertFalse(allowed(text, "/two"))
        self.assertTrue(allowed(text, "/three"))

    def test_empty_disallow_allows(self):
        self.assertTrue(allowed("User-agent: *\nDisallow:\n", "/x"))

    def test_comments_bom_and_junk_lines(self):
        text = "﻿# hello\nUser-agent: * # everyone\nthis is not a rule\nDisallow: /x # no\n"
        self.assertFalse(allowed(text, "/x"))

    def test_rules_before_any_group_are_ignored(self):
        self.assertTrue(allowed("Disallow: /\n", "/x"))

    def test_robots_txt_itself_is_always_allowed(self):
        self.assertTrue(allowed("User-agent: *\nDisallow: /\n", "/robots.txt"))

    def test_percent_encoding_is_normalised(self):
        text = "User-agent: *\nDisallow: /caf%c3%a9\nDisallow: /%7Euser\n"
        self.assertFalse(allowed(text, "/caf%C3%A9"))
        self.assertFalse(allowed(text, "/café"))
        self.assertFalse(allowed(text, "/~user/x"))

    def test_regex_characters_are_literal(self):
        text = "User-agent: *\nDisallow: /a.b(c)\n"
        self.assertFalse(allowed(text, "/a.b(c)"))
        self.assertTrue(allowed(text, "/aXb(c)"))

    def test_hostile_pattern_cannot_stall_matching(self):
        import time
        text = "User-agent: *\nDisallow: /" + "*a" * 30 + "$\n"
        started = time.monotonic()
        self.assertTrue(allowed(text, "/" + "a" * 5000 + "b"))
        self.assertFalse(allowed(text, "/" + "a" * 5000))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_more_edge_cases(self):
        self.assertFalse(allowed("User-agent: AcmeBot/1.0\nDisallow: /x\n", "/x"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /sp ace\n", "/sp%20ace"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /pa\nAllow: /%70\n", "/page"))
        raw = b"User-agent: *\nDisallow: /caf\xe9\n".decode("utf-8", "surrogateescape")
        self.assertFalse(allowed(raw, "/caf%E9"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /a*b$\n", "/a-b"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /a*b$\n", "/a-bc"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /*.php\n", "/x/index.php?id=1"))

    def test_content_signal_reaches_every_bot(self):
        own_group = RobotsFile.parse("User-agent: *\nContent-Signal: ai-train=no\n\nUser-agent: acmebot\nAllow: /\n")
        self.assertIs(own_group.evaluate("AcmeBot", "https://s.example/").signals.get("ai-train"), False)
        site_wide = RobotsFile.parse("Content-Signal: ai-train=no; ai-input = no\nUser-agent: *\nAllow: /\n")
        self.assertEqual(site_wide.evaluate("AcmeBot", "https://s.example/").signals, {"ai-train": False, "ai-input": False})
        tabbed = RobotsFile.parse("User-agent: *\nContent-Signal: /p\tai-train=no\n")
        self.assertIs(tabbed.evaluate("AcmeBot", "https://s.example/p/1").signals.get("ai-train"), False)

    def test_crawl_delay_sitemaps_and_signals(self):
        robots = RobotsFile.parse(
            "Sitemap: https://site.example/sitemap.xml\n"
            "User-agent: *\nCrawl-delay: 2.5\nContent-Signal: search=yes, ai-train=no\n"
            "Content-Signal: /blog/ ai-train=yes\nAllow: /\n"
        )
        verdict = robots.evaluate("AcmeBot", "https://site.example/x")
        self.assertEqual(verdict.crawl_delay, 2.5)
        self.assertEqual(verdict.signals, {"search": True, "ai-train": False})
        self.assertTrue(robots.evaluate("AcmeBot", "https://site.example/blog/1").signals["ai-train"])
        self.assertEqual(robots.sitemaps, ["https://site.example/sitemap.xml"])

    def test_bad_crawl_delay_is_ignored(self):
        robots = RobotsFile.parse("User-agent: *\nCrawl-delay: soon\nCrawl-delay: -4\n")
        self.assertIsNone(robots.evaluate("AcmeBot", "https://site.example/").crawl_delay)


if __name__ == "__main__":
    unittest.main()

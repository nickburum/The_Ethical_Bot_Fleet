# 🤖 The Ethical Bot Fleet

**Bots with manners.** Run one crawler or a thousand AI agents, and let the fleet say "no" for you.

[![tests](https://github.com/nickburum/The_Ethical_Bot_Fleet/actions/workflows/tests.yml/badge.svg)](https://github.com/nickburum/The_Ethical_Bot_Fleet/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

Every scraper tutorial ends with "remember to respect robots.txt and rate limits". Almost no code does.
This library does it for you, and it will not let you forget:

```python
from ethical_bot_fleet import Fleet, Blocked

fleet = Fleet(audit_log="audit.jsonl")
bot = fleet.bot("AcmeResearchBot", contact="https://acme.example/bot", purpose="ai-input")

try:
    page = bot.get("https://example.com/article")
    print(page.text)
except Blocked as no:
    print("The fleet said no:", no)
```

```text
$ ebf check --purpose ai-train https://example.com/ https://example.com/private/x
ALLOW  https://example.com/
       ok: allowed by robots.txt
BLOCK  https://example.com/private/x
       robots_disallow: robots.txt disallows this path for EthicalFleetCLI
       rule: Disallow: /private/ (line 2)
```

Pure Python standard library. No dependencies. Python 3.9+.

## Install

```bash
pip install git+https://github.com/nickburum/The_Ethical_Bot_Fleet
```

## Try it in ten seconds: find the dead links in your README

```bash
ebf links README.md
```

```text
BROKEN   https://example.com/old-docs  (404)
skipped       https://example.org/members  (robots_disallow)
41 links: 39 ok, 1 broken, 1 skipped because the site said no
```

It exits non-zero when a link is broken, so it drops straight into CI, and it will not get your CI
runner banned for hammering someone's server.

## What people use it for

- **Research agents.** An LLM that reads the web to answer questions, and only reads what it is allowed to.
- **Search and RAG indexing.** Build an index of public docs while honouring `noai` and crawl delays.
- **Dead-link and uptime checks.** For your docs, your awesome-list, or your own sites.
- **Training-data collection with a paper trail.** Skip everything that opted out, and keep the log that proves it.
- **Checking your own site.** `ebf check --purpose ai-train https://your.site/` shows what a well-behaved AI crawler would conclude from your robots.txt.

## What the fleet does for you

| | What happens |
|---|---|
| **robots.txt, properly** | A parser that follows [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309): `*` and `$` wildcards, longest match wins, per-agent groups. If robots.txt is unreachable or returns a server error, the site is treated as off limits. Python's built-in `urllib.robotparser` does none of that. |
| **AI opt-outs** | Bots declare a purpose (`search`, `ai-input`, `ai-train`, `other`). The fleet honours `Content-Signal` in robots.txt, `X-Robots-Tag: noai`, `<meta name="robots" content="noai">` and `tdm-reservation: 1`. If a page opts out, you never receive its content. |
| **Politeness, fleet-wide** | One request at a time per host, with at least `Crawl-delay` (or one second) between them, shared by every bot in the fleet. A thousand bots still look like one considerate visitor. |
| **Backing off** | `429` and `503` are retried only after `Retry-After`. If a server asks for a long break, the fleet stops instead of hammering it. |
| **Honest identity** | Every request carries `YourBot/1.0 (+contact; purpose=…)`. Bots without contact details, or with names like `Mozilla` or `Googlebot`, refuse to start. |
| **Checked redirects** | Each redirect hop is checked against the robots.txt of the site it lands on. |
| **No inside jobs** | Requests to localhost, private networks and cloud metadata addresses are refused by default, which matters when an LLM chooses the URLs. |
| **An audit log you can prove** | Every allow, block, fetch and retry is appended to a hash-chained JSONL file. Edit or delete a line and `ebf audit verify` tells you which one. |

There is no `ignore_robots=True`. For sites you own, list them in `trusted_domains`; that choice goes in the audit log too.

## A fleet

```python
from ethical_bot_fleet import Fleet, Policy

fleet = Fleet(
    Policy(min_delay=2.0, max_requests_per_host=500, blocked_domains=["example.org"]),
    audit_log="audit.jsonl",
)
searcher = fleet.bot("AcmeSearchBot", contact="bots@acme.example", purpose="search")
reader = fleet.bot("AcmeReaderBot", contact="bots@acme.example", purpose="ai-input")

for result in searcher.crawl(urls, workers=32):
    if result.response:
        index(result.url, result.response.text)
    elif result.blocked:
        print("skipped", result.url, result.error.decision.reason)
```

Both bots share the same per-host budget, so adding bots never adds pressure on any one site.

### Give it to your LLM agent

Hand your agent `bot.get` as its only way to reach the web. Whatever URL the model comes up with, it
goes through the same checks, and a refusal comes back as a plain sentence the model can read:

```python
def fetch_tool(url: str) -> str:
    try:
        return bot.get(url).text[:20_000]
    except Blocked as no:
        return f"Not fetched: {no}"
```

See [`examples/`](examples) for runnable scripts.

## Command line

```bash
ebf check https://example.com/page
```

```bash
ebf fetch --agent AcmeBot --contact https://acme.example/bot https://example.com/page
```

```bash
ebf links README.md docs/*.md
```

```bash
ebf audit verify audit.jsonl
```

```bash
ebf audit summary audit.jsonl
```

`ebf check` exits with status 3 when a URL is blocked, so it works in scripts and CI. Add `--json` for machine-readable output.

## Reasons the fleet says no

| `decision.reason` | Meaning |
|---|---|
| `robots_disallow` | robots.txt disallows the path for this bot |
| `robots_unreachable` | robots.txt could not be read, so the site is off limits for now |
| `content_signal` | robots.txt opts out of this bot's purpose |
| `page_opt_out` | the page itself opted out (`noai`, `tdm-reservation`); content discarded |
| `crawl_delay_too_long` | the site wants a longer gap than your policy allows, so it is skipped |
| `retry_after_too_long` | the server asked for a long break |
| `host_budget` | your own `max_requests_per_host` was reached |
| `domain_blocked` / `domain_not_allowed` | your own allow and block lists |
| `private_network` | the address is not on the public internet |
| `bad_url` / `too_many_redirects` | what they say |

## What this is not

It is not a tool for fake accounts, fake reviews, fake views, CAPTCHA solving or getting around blocks, and
it will not grow those features. It only reads public pages (`GET`), as itself, when the site allows it.
The [Bot Code of Conduct](docs/BOT_CODE_OF_CONDUCT.md) is the whole philosophy on one page.

## Limits worth knowing

- Opt-out standards for AI are still settling. `Content-Signal` and `noai` are supported on a best-effort basis and will track the standards as they change.
- The private-network check resolves the hostname before the request, so it does not defend against DNS rebinding. Use a network-level egress policy as well if that matters to you.
- Concurrency uses threads. An asyncio API is on the roadmap.
- It follows published rules; it cannot read a site's terms of service for you.

## Contributing

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests with:

```bash
python -m unittest discover -s tests
```

## License

[MIT](LICENSE)

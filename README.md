# 🤖 The Ethical Bot Fleet

**Bots with manners.** Run one crawler or a thousand AI agents, and let the fleet say "no" for you.

[![tests](https://github.com/nickburum/The_Ethical_Bot_Fleet/actions/workflows/tests.yml/badge.svg)](https://github.com/nickburum/The_Ethical_Bot_Fleet/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

Every scraper tutorial ends with "remember to respect robots.txt and rate limits". Almost no code does.
This does it for you, and it will not let you forget.

Free and open source (MIT). No accounts, no telemetry, nothing stored about you. Python 3.9+, no dependencies.

## Start in one minute

```bash
pip install git+https://github.com/nickburum/The_Ethical_Bot_Fleet
```

**1. No AI needed.** Read any public page as clean text, check a URL, or find dead links:

```bash
ebf read https://example.com/
```

```bash
ebf check https://example.com/private/page
```

```bash
ebf links README.md
```

**2. With an AI model on your own computer.** Nothing leaves your machine except the page requests:

```bash
ebf ask "What is this page about?" https://example.com/ --model ollama
```

**3. With a cloud model.** Set that provider's key in your environment, then pick it by name:

```bash
ebf ask "Summarise the pricing" https://example.com/pricing --model claude
```

`ebf models` shows every preset and which ones are ready on your machine:

| Runs on your machine | Cloud |
|---|---|
| `ollama`, `lmstudio`, `llamacpp`, `vllm`, `jan` | `claude`, `openai`, `gemini`, `mistral`, `groq`, `deepseek`, `xai`, `openrouter`, `together` |

Anything else that speaks the OpenAI-compatible API works with `--model custom --base-url http://localhost:5000/v1`.
Step-by-step setup for each one is in **[docs/MODELS.md](docs/MODELS.md)**.

When a site says no, you see why, and so does the model:

```text
$ ebf check https://example.com/ https://example.com/private/x
ALLOW  https://example.com/
       ok: allowed by robots.txt
BLOCK  https://example.com/private/x
       robots_disallow: robots.txt disallows this path for EthicalFleetCLI
       rule: Disallow: /private/ (line 2)
```

## Your privacy

- **No accounts, no sign-up, no telemetry.** The software never contacts its authors or anyone else on its own.
- **Nothing is stored.** No config files, no history, no cache on disk. The one exception is the audit log, which exists only if you ask for it with `--audit-log` and stays on your machine.
- **API keys stay in your environment.** They are read when needed and never written anywhere. A provider's key is sent only to that provider's own address, and never follows a redirect.
- **Local means local.** A model on your own machine is contacted directly, never through a proxy.
- **You never give your name.** A bot always says it is a bot and gives a contact address, but that can be a project page or a shared mailbox. The command line uses a generic identity out of the box.

What it does not do is disguise bots as people or hide them from the sites they visit. Details in [docs/PRIVACY.md](docs/PRIVACY.md).

## In Python

```python
from ethical_bot_fleet import Fleet, Blocked, ask, readable

fleet = Fleet()
bot = fleet.bot("AcmeResearchBot", contact="https://acme.example/bot", purpose="ai-input")

try:
    page = bot.get("https://example.com/article")
    print(readable(page))   # clean text; page.text is the raw HTML
except Blocked as no:
    print("The fleet said no:", no)

print(ask(bot, "What is this about?", ["https://example.com/article"], model="ollama"))
```

## What the fleet does for you

| | What happens |
|---|---|
| **robots.txt, properly** | A parser that follows [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309): `*` and `$` wildcards, longest match wins, per-agent groups. Path tricks such as `/a/../private/` or `//private/` are resolved before the rules are applied. If robots.txt is unreachable or returns a server error, the site is treated as off limits. Python's built-in `urllib.robotparser` does none of that. |
| **AI opt-outs** | Bots declare a purpose (`search`, `ai-input`, `ai-train`, `other`). The fleet honours `Content-Signal` in robots.txt, `X-Robots-Tag: noai` and `<meta name="robots" content="noai">` for both AI purposes, and `tdm-reservation: 1` for `ai-train`. If a page opts out of your bot's purpose, you never receive its content. This relies on you declaring the purpose truthfully. |
| **Politeness, fleet-wide** | One request at a time per host, with at least `Crawl-delay` (or one second, unless you change it) between them, shared by every bot in the fleet. `127.0.0.1`, `127.1` and `127.0.0.1:80` count as the same host. A thousand bots still look like one considerate visitor. |
| **Backing off** | `429` and `503` are retried at most twice, after waiting for `Retry-After` (or with growing pauses if the server gives none). If a server asks for a long break, the whole site is off limits to every bot in the fleet until the break is over. A server that trickles its reply cannot hold a bot longer than `max_time` (60 seconds). |
| **Honest identity** | Every request carries `YourBot/1.0 (+contact; purpose=…)`. Bots without contact details, or with names like `Mozilla` or `Googlebot`, refuse to start. |
| **Checked redirects** | Each redirect hop is checked against the robots.txt of the site it lands on. |
| **No inside jobs** | Requests to localhost, private networks and cloud metadata addresses are refused by default, which matters when an LLM chooses the URLs. |
| **An audit log you can prove** | Every robots.txt request, allow, block, fetch, redirect and retry is appended to a hash-chained JSONL file that several fleets can share. Edit, reorder or remove a record and `ebf audit verify` tells you where. To also catch a log that was cut short, keep the last hash it prints and pass it back with `--head`. |

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

urls = ["https://example.com/", "https://example.com/about"]
for result in searcher.crawl(urls, workers=32):
    if result.response:
        print(result.url, len(result.response.text))
    elif result.blocked:
        print("skipped", result.url, result.error.decision.reason)
```

Both bots share the same per-host budget, so adding bots never adds pressure on any one site.

### Give it to your own LLM agent

`ask` covers the common case. If you already have an agent framework, hand it `bot.get` as its only way to reach the web. Whatever URL the model comes up with, it
goes through the same checks, and a refusal comes back as a plain sentence the model can read:

```python
from ethical_bot_fleet import FleetError, readable

def fetch_tool(url: str) -> str:
    try:
        return readable(bot.get(url))
    except FleetError as no:   # Blocked, or a network failure
        return f"Not fetched: {no}"
```

See [`examples/`](examples) for runnable scripts.

## Command line

```bash
ebf read https://example.com/page
```

```bash
ebf ask "your question" https://example.com/page --model ollama
```

```bash
ebf models
```

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
| `robots_unreachable` | the site answered, but its robots.txt returned an error, so it is off limits for now |
| `site_unreachable` | the site could not be contacted at all (dead domain, server down) |
| `content_signal` | robots.txt opts out of this bot's purpose |
| `page_opt_out` | the page itself opted out (`noai`, `tdm-reservation`); content discarded |
| `crawl_delay_too_long` | the site wants a longer gap than your policy allows, so it is skipped |
| `retry_after_too_long` | the server asked for a long break |
| `host_backoff` | the site is still inside a break it asked for earlier |
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
- The private-network check resolves the hostname before the request, so it does not defend against DNS rebinding, and behind an HTTP proxy the proxy does the resolving where this check cannot see. Use a network-level egress policy as well if that matters to you.
- The audit log shows accidental damage and casual edits. It is not proof against someone who can rewrite the whole file, unless you kept the last hash somewhere they cannot reach (`ebf audit verify --head`).
- The name check stops the obvious impersonations (browsers and well-known crawlers). It cannot know every crawler name.
- Concurrency uses threads. An asyncio API is on the roadmap.
- It follows published rules; it cannot read a site's terms of service for you.

## Contributing

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests with:

```bash
python -m unittest discover -s tests
```

## License

[MIT](LICENSE)

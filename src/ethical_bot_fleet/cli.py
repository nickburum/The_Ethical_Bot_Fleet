"""The ``ebf`` command line tool."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from typing import List, Optional
from urllib.parse import urlsplit

from . import __version__
from .audit import read, verify
from .fleet import Blocked, Fleet, FleetError, Policy
from .signals import PURPOSES

PROJECT_URL = "https://github.com/nickburum/The_Ethical_Bot_Fleet"
EXIT_BLOCKED = 3
_LINK = re.compile(r"https?://[^\s<>\"'`\]]+")
_HINTS = {"private_network": "add --allow-private if you really mean to reach this address"}

EPILOG = """\
examples:
  ebf read https://example.com/            read a page as plain text (no AI needed)
  ebf check https://example.com/page       may a bot fetch this?
  ebf links README.md                      find dead links
  ebf models                               which AI models are ready to use
  ebf ask "What is this page about?" https://example.com/ --model ollama

privacy: no accounts, no telemetry, nothing stored unless you pass --audit-log.
"""


def _url(value: str) -> str:
    """Let people type example.com instead of https://example.com."""
    return value if "://" in value else "https://" + value


def _hint(decision) -> None:
    if decision.reason in _HINTS:
        print("       hint: %s" % _HINTS[decision.reason], file=sys.stderr)


def _trim(link: str) -> str:
    link = link.rstrip(".,;:!?")
    while link.endswith(")") and link.count(")") > link.count("("):
        link = link[:-1].rstrip(".,;:!?")
    return link


def _bot(args: argparse.Namespace, purpose: Optional[str] = None):
    policy = Policy(block_private_networks=not args.allow_private, min_delay=args.delay)
    return Fleet(policy, audit_log=args.audit_log).bot(args.agent, args.contact, purpose or args.purpose)


def _options(parser: argparse.ArgumentParser, purpose: str = "other") -> None:
    group = parser.add_argument_group("bot options (the defaults are fine)")
    group.add_argument("--agent", default="EthicalFleetCLI", help="the bot's name (default: %(default)s)")
    group.add_argument("--contact", default=PROJECT_URL, help="URL or email where site owners can reach the operator")
    group.add_argument("--purpose", default=purpose, choices=PURPOSES, help="why the bot is reading (default: %(default)s)")
    group.add_argument("--audit-log", metavar="FILE", help="append every decision to this file (off by default)")
    group.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one site")
    group.add_argument("--allow-private", action="store_true", help="allow localhost and private addresses")


def _check(args: argparse.Namespace) -> int:
    bot = _bot(args)
    worst = 0
    for url in map(_url, args.url):
        decision = bot.check(url)
        if args.json:
            print(json.dumps(decision.__dict__, sort_keys=True))
        else:
            print("%s  %s" % ("ALLOW" if decision.allowed else "BLOCK", url))
            print("       %s: %s" % (decision.reason, decision.detail))
            if decision.rule:
                print("       rule: %s" % decision.rule)
            if decision.crawl_delay:
                print("       crawl-delay: %gs" % decision.crawl_delay)
        if not decision.allowed:
            _hint(decision)
            worst = EXIT_BLOCKED
    return worst


def _get(args: argparse.Namespace, as_text: bool) -> int:
    from .text import readable

    bot = _bot(args)
    worst = 0
    for url in map(_url, args.url):
        try:
            response = bot.get(url)
        except Blocked as error:
            print("BLOCK  %s\n       %s" % (url, error), file=sys.stderr)
            _hint(error.decision)
            worst = EXIT_BLOCKED
            continue
        except FleetError as error:
            print("ERROR  %s" % error, file=sys.stderr)
            worst = worst or 1
            continue
        print("%d  %s  (%d bytes%s)" % (
            response.status, response.url, len(response.body),
            ", cut off at the size limit" if response.truncated else ""), file=sys.stderr)
        if as_text and not response.ok:
            print("       the server answered with an error page; not printed", file=sys.stderr)
        elif as_text:
            print(readable(response))
        else:
            sys.stdout.flush()
            sys.stdout.buffer.write(response.body)
        if not response.ok:
            worst = worst or 1
    return worst


def _links(args: argparse.Namespace) -> int:
    bot = _bot(args)
    urls: List[str] = []
    for source in args.url:
        if source.startswith(("http://", "https://")):
            text = bot.get(source).text
        else:
            with open(source, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        urls += [_trim(match) for match in _LINK.findall(text)]
    urls = list(dict.fromkeys(urls))
    broken = skipped = 0
    for result in bot.crawl(urls, workers=args.workers):
        if result.response is not None and result.response.status < 400:
            if args.verbose:
                print("ok       %d  %s" % (result.response.status, result.url))
        elif result.blocked and result.error.decision.reason in ("site_unreachable", "bad_url"):
            broken += 1
            print("BROKEN   %s  (%s)" % (result.url, result.error.decision.detail))
        elif result.blocked:
            skipped += 1
            print("skipped       %s  (%s)" % (result.url, result.error.decision.reason))
        else:
            broken += 1
            why = result.response.status if result.response is not None else result.error
            print("BROKEN   %s  (%s)" % (result.url, why))
    print("%d links: %d ok, %d broken, %d skipped because the site said no" % (
        len(urls), len(urls) - broken - skipped, broken, skipped))
    return 1 if broken else 0


def _ask(args: argparse.Namespace) -> int:
    from .llm import ask

    question, urls = args.question, list(args.url)
    if question.startswith(("http://", "https://")) and urls and " " in urls[-1]:
        question, urls = urls[-1], [question] + urls[:-1]  # the question and a URL were typed the other way round
    answer = ask(
        _bot(args), question, [_url(u) for u in urls], model=args.model, base_url=args.base_url,
        on_event=lambda message: print("· " + message, file=sys.stderr),
    )
    print(answer)
    return 0


def _models(args: argparse.Namespace) -> int:
    from .llm import status

    rows = status()
    for local in (False, True):
        print("on your machine (private, no key):" if local else "cloud:")
        for name, preset, ready, note in rows:
            if preset.local == local:
                print("  %s %-11s %-30s %s" % ("✓" if ready else "·", name, preset.label, note))
    print('\nuse one with:  ebf ask "your question" URL --model NAME        (or NAME:MODEL_ID)')
    return 0


def _audit_verify(args: argparse.Namespace) -> int:
    ok, count, message = verify(args.file, args.head)
    print("%s  %d records, %s" % ("OK    " if ok else "BROKEN", count, message))
    return 0 if ok else 1


def _audit_summary(args: argparse.Namespace) -> int:
    records = read(args.file)
    events = Counter(r["event"] for r in records)
    reasons = Counter(r.get("reason", "?") for r in records if r["event"] == "block")
    hosts = Counter(urlsplit(r["url"]).hostname for r in records if r["event"] == "fetch")
    print("%d records" % len(records))
    for name, count in events.most_common():
        print("  %-6s %d" % (name, count))
    if reasons:
        print("blocked because")
        for name, count in reasons.most_common():
            print("  %-22s %d" % (name, count))
    if hosts:
        print("fetches per host")
        for name, count in hosts.most_common(10):
            print("  %-30s %d" % (name, count))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ebf", description="The Ethical Bot Fleet: bots with manners.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="ethical-bot-fleet " + __version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub = commands.add_parser("read", help="read a page as plain text (no AI needed)")
    sub.add_argument("url", nargs="+")
    _options(sub)
    sub.set_defaults(run=lambda args: _get(args, True))

    sub = commands.add_parser("ask", help="ask an AI model a question about web pages")
    sub.add_argument("question")
    sub.add_argument("url", nargs="*", help="pages to read first (the model can ask for more)")
    sub.add_argument("--model", help="preset name, or NAME:MODEL_ID (see `ebf models`); default: $EBF_MODEL")
    sub.add_argument("--base-url", help="address of an OpenAI-compatible server, e.g. http://localhost:11434/v1")
    _options(sub, purpose="ai-input")
    sub.set_defaults(run=_ask)

    sub = commands.add_parser("models", help="list AI model presets and which are ready")
    sub.set_defaults(run=_models)

    sub = commands.add_parser("check", help="may a bot fetch this URL? (reads robots.txt only)")
    sub.add_argument("url", nargs="+")
    sub.add_argument("--json", action="store_true", help="one JSON object per URL")
    _options(sub)
    sub.set_defaults(run=_check)

    sub = commands.add_parser("links", help="find dead links in files or pages")
    sub.add_argument("url", nargs="+", metavar="file_or_url")
    sub.add_argument("--workers", type=int, default=16)
    sub.add_argument("-v", "--verbose", action="store_true", help="also list working links")
    _options(sub)
    sub.set_defaults(run=_links)

    sub = commands.add_parser("fetch", help="fetch a URL and print the raw body")
    sub.add_argument("url", nargs="+")
    _options(sub)
    sub.set_defaults(run=lambda args: _get(args, False))

    audit = commands.add_parser("audit", help="inspect an audit log").add_subparsers(dest="action", required=True)
    for name, run, text in (
        ("verify", _audit_verify, "check that no record was edited or removed"),
        ("summary", _audit_summary, "count what the fleet did"),
    ):
        sub = audit.add_parser(name, help=text)
        sub.add_argument("file")
        if name == "verify":
            sub.add_argument("--head", metavar="HASH", help="the last record's hash from an earlier verify; "
                             "detects records removed from the end")
        sub.set_defaults(run=run)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except FleetError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

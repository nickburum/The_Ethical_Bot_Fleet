"""The ``ebf`` command line tool."""

from __future__ import annotations

import argparse
import json
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


def _fleet(args: argparse.Namespace) -> Fleet:
    policy = Policy(block_private_networks=not args.allow_private, min_delay=args.delay)
    return Fleet(policy, audit_log=args.audit_log)


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", nargs="+")
    parser.add_argument("--agent", default="EthicalFleetCLI", help="bot name to check as (default: %(default)s)")
    parser.add_argument("--contact", default=PROJECT_URL, help="URL or email where site owners can reach you")
    parser.add_argument("--purpose", default="other", choices=PURPOSES)
    parser.add_argument("--audit-log", metavar="FILE", help="append decisions to this audit log")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host")
    parser.add_argument("--allow-private", action="store_true", help="allow localhost and private addresses")


def _check(args: argparse.Namespace) -> int:
    bot = _fleet(args).bot(args.agent, args.contact, args.purpose)
    worst = 0
    for url in args.url:
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
            worst = EXIT_BLOCKED
    return worst


def _fetch(args: argparse.Namespace) -> int:
    bot = _fleet(args).bot(args.agent, args.contact, args.purpose)
    worst = 0
    for url in args.url:
        try:
            response = bot.get(url)
        except Blocked as error:
            print("BLOCK  %s\n       %s" % (url, error), file=sys.stderr)
            worst = EXIT_BLOCKED
            continue
        except FleetError as error:
            print("ERROR  %s" % error, file=sys.stderr)
            worst = worst or 1
            continue
        print("%d  %s  (%d bytes)" % (response.status, response.url, len(response.body)), file=sys.stderr)
        sys.stdout.buffer.write(response.body)
    return worst


def _audit_verify(args: argparse.Namespace) -> int:
    ok, count, message = verify(args.file)
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
    parser = argparse.ArgumentParser(prog="ebf", description="The Ethical Bot Fleet: bots that behave themselves.")
    parser.add_argument("--version", action="version", version="ethical-bot-fleet " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="may a bot fetch this URL? (reads robots.txt only)")
    _add_identity(check)
    check.add_argument("--json", action="store_true", help="one JSON object per URL")
    check.set_defaults(run=_check)

    fetch = commands.add_parser("fetch", help="fetch a URL politely and print the body")
    _add_identity(fetch)
    fetch.set_defaults(run=_fetch)

    audit = commands.add_parser("audit", help="inspect an audit log").add_subparsers(dest="action", required=True)
    for name, run, text in (
        ("verify", _audit_verify, "check that no record was edited or removed"),
        ("summary", _audit_summary, "count what the fleet did"),
    ):
        sub = audit.add_parser(name, help=text)
        sub.add_argument("file")
        sub.set_defaults(run=run)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except FleetError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    except OSError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

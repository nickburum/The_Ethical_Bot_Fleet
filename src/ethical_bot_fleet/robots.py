"""A robots.txt parser that follows RFC 9309.

The standard library's ``urllib.robotparser`` predates the RFC: it has no
``*`` / ``$`` wildcards and uses first-match instead of longest-match. This
module implements the RFC rules, plus two widely used extensions
(``Crawl-delay`` and ``Content-Signal``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = frozenset(b"0123456789abcdefABCDEF")


def normalize_path(value: str) -> str:
    """Normalise percent-encoding so that equivalent paths compare equal."""
    raw = value.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x25 and i + 2 < len(raw) + 0 and raw[i + 1] in _HEX and raw[i + 2] in _HEX:
            v = int(raw[i + 1 : i + 3], 16)
            if v in _UNRESERVED:
                out.append(v)
            else:
                out += b"%%%02X" % v
            i += 3
        elif c >= 0x80:
            out += b"%%%02X" % c
            i += 1
        else:
            out.append(c)
            i += 1
    return out.decode("ascii")


def _compile(pattern: str) -> "re.Pattern[str]":
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(normalize_path(part)) for part in pattern.split("*"))
    return re.compile(body + ("$" if anchored else ""))


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    line: int
    _regex: "re.Pattern[str]" = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    def matches(self, path: str) -> bool:
        return self._regex.match(path) is not None

    def __str__(self) -> str:
        return "%s: %s (line %d)" % ("Allow" if self.allow else "Disallow", self.pattern, self.line)


@dataclass
class Group:
    rules: List[Rule] = field(default_factory=list)
    crawl_delay: Optional[float] = None
    # (path prefix, {"ai-train": False, ...})
    signals: List[Tuple[str, Dict[str, bool]]] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: Optional[Rule] = None
    crawl_delay: Optional[float] = None
    signals: Dict[str, bool] = field(default_factory=dict)
    note: str = ""


def _parse_signals(value: str) -> Optional[Tuple[str, Dict[str, bool]]]:
    prefix = "/"
    value = value.strip()
    if value.startswith("/"):
        head, _, value = value.partition(" ")
        prefix = head
    parsed: Dict[str, bool] = {}
    for item in value.split(","):
        key, sep, val = item.partition("=")
        key, val = key.strip().lower(), val.strip().lower()
        if sep and key and val in ("yes", "no"):
            parsed[key] = val == "yes"
    return (prefix, parsed) if parsed else None


class RobotsFile:
    """A parsed robots.txt file."""

    def __init__(self, groups: Dict[str, Group], sitemaps: List[str], mode: str = "parsed", note: str = ""):
        self.groups = groups
        self.sitemaps = sitemaps
        self.mode = mode  # "parsed" | "allow_all" | "disallow_all"
        self.note = note

    @classmethod
    def allow_all(cls, note: str = "") -> "RobotsFile":
        return cls({}, [], "allow_all", note)

    @classmethod
    def disallow_all(cls, note: str = "") -> "RobotsFile":
        return cls({}, [], "disallow_all", note)

    @classmethod
    def parse(cls, text: str) -> "RobotsFile":
        groups: Dict[str, Group] = {}
        sitemaps: List[str] = []
        current: List[str] = []
        in_rules = False
        for number, line in enumerate(text.lstrip("﻿").splitlines(), start=1):
            line = line.split("#", 1)[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    current, in_rules = [], False
                agent = value.lower()
                current.append(agent)
                groups.setdefault(agent, Group())
            elif key == "sitemap":
                if value:
                    sitemaps.append(value)
            elif key in ("allow", "disallow", "crawl-delay", "content-signal"):
                if not current:
                    continue
                in_rules = True
                for agent in current:
                    group = groups[agent]
                    if key in ("allow", "disallow"):
                        if value:  # an empty Disallow means "no restriction"
                            group.rules.append(Rule(key == "allow", value, number, _compile(value)))
                    elif key == "crawl-delay":
                        try:
                            delay = float(value)
                        except ValueError:
                            continue
                        if delay >= 0:
                            group.crawl_delay = delay
                    else:
                        parsed = _parse_signals(value)
                        if parsed:
                            group.signals.append(parsed)
        return cls(groups, sitemaps)

    def group_for(self, agent: str) -> Optional[Group]:
        return self.groups.get(agent.lower()) or self.groups.get("*")

    def evaluate(self, agent: str, url: str) -> Verdict:
        """Decide whether ``agent`` may fetch ``url`` (RFC 9309 section 2.2)."""
        parts = urlsplit(url)
        path = normalize_path(parts.path or "/")
        if parts.query:
            path += "?" + normalize_path(parts.query)
        if path == "/robots.txt" or self.mode == "allow_all":
            return Verdict(True, note=self.note)
        if self.mode == "disallow_all":
            return Verdict(False, note=self.note)
        group = self.group_for(agent)
        if group is None:
            return Verdict(True)
        best: Optional[Rule] = None
        for rule in group.rules:
            if not rule.matches(path):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        signals: Dict[str, bool] = {}
        for prefix, values in sorted(group.signals, key=lambda s: len(s[0])):
            if path.startswith(normalize_path(prefix)):
                signals.update(values)
        return Verdict(best is None or best.allow, best, group.crawl_delay, signals)

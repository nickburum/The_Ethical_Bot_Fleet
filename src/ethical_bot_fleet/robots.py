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


_LITERAL = frozenset(b"!$&'()*+,;=:@/?[]")  # reserved characters that keep their meaning unencoded


def normalize_path(value: str) -> str:
    """Normalise percent-encoding so that equivalent paths compare equal."""
    raw = value.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x25 and i + 2 < len(raw) and raw[i + 1] in _HEX and raw[i + 2] in _HEX:
            v = int(raw[i + 1 : i + 3], 16)
            if v in _UNRESERVED:
                out.append(v)
            else:
                out += b"%%%02X" % v
            i += 3
        elif c not in _UNRESERVED and c not in _LITERAL and c != 0x25:
            out += b"%%%02X" % c  # spaces, quotes, non-ASCII bytes and the like
            i += 1
        else:
            out.append(c)
            i += 1
    return out.decode("ascii")


class _Glob:
    """A robots.txt pattern. Matching is linear in the path, so no pattern can stall the bot."""

    def __init__(self, pattern: str):
        self.anchored = pattern.endswith("$")
        if self.anchored:
            pattern = pattern[:-1]
        self.parts = [normalize_path(part) for part in re.sub(r"\*+", "*", pattern).split("*")]
        self.size = sum(len(part) for part in self.parts) + len(self.parts) - 1 + self.anchored

    def match(self, path: str) -> bool:
        parts = self.parts
        if not path.startswith(parts[0]):
            return False
        if len(parts) == 1:
            return not self.anchored or path == parts[0]
        position = len(parts[0])
        for part in parts[1:-1] if self.anchored else parts[1:]:
            found = path.find(part, position)
            if found < 0:
                return False
            position = found + len(part)
        if self.anchored:
            return len(path) - len(parts[-1]) >= position and path.endswith(parts[-1])
        return True


def _compile(pattern: str) -> _Glob:
    return _Glob(pattern)


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    line: int
    _regex: _Glob = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    def matches(self, path: str) -> bool:
        return self._regex.match(path)

    @property
    def size(self) -> int:
        return self._regex.size

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
        pieces = value.split(None, 1)
        prefix, value = pieces[0], pieces[1] if len(pieces) > 1 else ""
    parsed: Dict[str, bool] = {}
    for item in re.split(r"[,;\s]+", re.sub(r"\s*=\s*", "=", value)):
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
        self.site_signals: List[Tuple[str, Dict[str, bool]]] = []  # Content-Signal lines outside any group
        self.mode = mode  # "parsed" | "allow_all" | "disallow_all" | "unreachable"
        self.note = note

    @classmethod
    def allow_all(cls, note: str = "") -> "RobotsFile":
        return cls({}, [], "allow_all", note)

    @classmethod
    def disallow_all(cls, note: str = "") -> "RobotsFile":
        return cls({}, [], "disallow_all", note)

    @classmethod
    def unreachable(cls, note: str = "") -> "RobotsFile":
        """The site could not be contacted at all, so nothing may be fetched."""
        return cls({}, [], "unreachable", note)

    @classmethod
    def parse(cls, text: str) -> "RobotsFile":
        groups: Dict[str, Group] = {}
        sitemaps: List[str] = []
        site_signals: List[Tuple[str, Dict[str, bool]]] = []
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
                agent = value.split("/")[0].strip().lower()  # "AcmeBot/1.0" names the bot AcmeBot
                current.append(agent)
                groups.setdefault(agent, Group())
            elif key == "sitemap":
                if value:
                    sitemaps.append(value)
            elif key in ("allow", "disallow", "crawl-delay", "content-signal"):
                if not current:
                    if key == "content-signal":
                        parsed = _parse_signals(value)
                        if parsed:
                            site_signals.append(parsed)
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
        robots = cls(groups, sitemaps)
        robots.site_signals = site_signals
        return robots

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
        if self.mode in ("disallow_all", "unreachable"):
            return Verdict(False, note=self.note)
        verdict = self._evaluate(agent, path)
        # Most servers treat "//private/x" as "/private/x", so the stricter reading wins.
        collapsed = re.sub(r"/{2,}", "/", path.split("?")[0]) + path[len(path.split("?")[0]):]
        if verdict.allowed and collapsed != path:
            other = self._evaluate(agent, collapsed)
            if not other.allowed:
                return other
        return verdict

    def _evaluate(self, agent: str, path: str) -> Verdict:
        group = self.group_for(agent)
        own = self.groups.get(agent.lower())
        best: Optional[Rule] = None
        for rule in group.rules if group else ():
            if not rule.matches(path):
                continue
            if best is None or rule.size > best.size or (rule.size == best.size and rule.allow and not best.allow):
                best = rule
        # Opt-out signals: site-wide lines, then the "*" group, then the bot's own group (most specific last).
        layers = list(self.site_signals)
        for layer in (self.groups.get("*"), own):
            if layer is not None:
                layers += sorted(layer.signals, key=lambda s: len(s[0]))
        signals: Dict[str, bool] = {}
        for prefix, values in layers:
            if path.startswith(normalize_path(prefix)):
                signals.update(values)
        return Verdict(best is None or best.allow, best, group.crawl_delay if group else None, signals)

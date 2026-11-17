"""Page-level opt-out signals: ``X-Robots-Tag``, ``<meta name="robots">`` and
``tdm-reservation``.

These arrive with the response, so they are checked after the fetch and
before the body is handed to the caller.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Dict, Optional, Set

PURPOSES = ("search", "ai-input", "ai-train", "other")
AI_PURPOSES = ("ai-input", "ai-train")

_SCAN_BYTES = 256 * 1024


class _MetaScanner(HTMLParser):
    def __init__(self, agent: str):
        super().__init__(convert_charrefs=True)
        self.agent = agent
        self.directives: Set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        if attr.get("name", "").strip().lower() in ("robots", self.agent):
            self.directives.update(_split(attr.get("content", "")))


def _split(value: str) -> Set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _header_directives(value: str, agent: str) -> Set[str]:
    """Parse X-Robots-Tag, which may be scoped as ``botname: directive``."""
    found: Set[str] = set()
    # Multiple header lines are joined with ", " so scoping is tracked per item.
    scope: Optional[str] = None
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        head, sep, rest = item.partition(":")
        if sep and head.strip() != "unavailable_after" and " " not in head.strip():
            scope, item = head.strip(), rest.strip()
        if scope in (None, agent) and item:
            found.add(item)
    return found


def page_directives(headers: Dict[str, str], body: bytes, agent: str) -> Set[str]:
    """Collect every robots directive that applies to ``agent`` for this page."""
    agent = agent.lower()
    found = _header_directives(headers.get("x-robots-tag", ""), agent)
    if headers.get("tdm-reservation", "").strip() == "1":
        found.add("tdm-reservation")
    if "html" in headers.get("content-type", "").lower():
        scanner = _MetaScanner(agent)
        try:
            scanner.feed(body[:_SCAN_BYTES].decode("utf-8", "replace"))
        except Exception:  # malformed markup must never crash a crawl
            pass
        found |= scanner.directives
    return found


def violation(purpose: str, directives: Set[str], content_type: str = "") -> Optional[str]:
    """Return the directive that forbids ``purpose``, if any."""
    if purpose in AI_PURPOSES:
        if "noai" in directives:
            return "noai"
        if "noimageai" in directives and content_type.lower().startswith("image/"):
            return "noimageai"
    if purpose == "ai-train" and "tdm-reservation" in directives:
        return "tdm-reservation"
    return None

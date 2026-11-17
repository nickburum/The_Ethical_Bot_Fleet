"""Turn an HTML page into readable plain text. No AI involved."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List

_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
    "nav", "aside", "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "form",
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was read
        pass
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    title = " ".join(parser.title.split())
    return (title + "\n\n" + body).strip() if title else body


def readable(response) -> str:
    """Plain text for a fleet ``Response``: HTML is stripped, other text is returned as is."""
    kind = response.headers.get("content-type", "").lower()
    if "html" in kind or (not kind and response.text.lstrip()[:1] == "<"):
        return html_to_text(response.text)
    return response.text

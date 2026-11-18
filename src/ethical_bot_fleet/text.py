"""Turn an HTML page into readable plain text. No AI involved."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List

_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "button", "select"}
_MAIN = {"main", "article"}  # where a page keeps its real content
_CELL = {"td", "th"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
    "nav", "aside", "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "form",
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.main: List[str] = []  # the same text, but only from inside <main> or <article>
        self._in_main = 0
        self.title = ""
        self._skip = 0
        self._in_title = False

    def _add(self, text: str) -> None:
        self.parts.append(text)
        if self._in_main:
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        elif tag in _MAIN:
            self._in_main += 1
            self._add("\n")
        elif tag in _BLOCK:
            self._add("\n")
        elif tag in _CELL:
            self._add("  ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _MAIN:
            self._add("\n")
            self._in_main = max(0, self._in_main - 1)
        elif tag in _BLOCK:
            self._add("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._add(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was read
        pass
    def tidy(parts: List[str]) -> str:
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    body = tidy(parser.main)
    if len(body) < 200:  # no marked main content, or too little to trust: use the whole page
        body = tidy(parser.parts)
    title = " ".join(parser.title.split())
    return (title + "\n\n" + body).strip() if title else body


def readable(response) -> str:
    """Plain text for a fleet ``Response``: HTML is stripped, other text is returned as is."""
    kind = response.headers.get("content-type", "").lower()
    textual = not kind or kind.startswith("text/") or any(word in kind for word in ("json", "xml", "html", "javascript"))
    if not textual or b"\x00" in response.body[:1024]:
        return "[This is not a text page (%s, %d bytes), so there is nothing to read.]" % (
            kind.split(";")[0] or "unknown type", len(response.body))
    if "html" in kind or (not kind and response.text.lstrip()[:1] == "<"):
        return html_to_text(response.text)
    return response.text

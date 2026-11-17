"""The HTTP layer. Swap it for requests/httpx by passing ``transport=`` to Fleet."""

from __future__ import annotations

import gzip
import io
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict


@dataclass
class RawResponse:
    status: int
    headers: Dict[str, str]  # lower-case names; repeated headers joined with ", "
    body: bytes
    truncated: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Redirects are followed by the fleet so every hop is policy-checked.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _pack(handle, max_bytes: int) -> RawResponse:
    headers: Dict[str, str] = {}
    for name, value in handle.headers.items():
        name = name.lower()
        # X-Robots-Tag lines stay separate: a "botname:" prefix only covers its own line.
        joiner = "\n" if name == "x-robots-tag" else ", "
        headers[name] = headers[name] + joiner + value if name in headers else value
    body = handle.read(max_bytes + 1)
    truncated = len(body) > max_bytes
    expected = headers.get("content-length", "")
    if not truncated and expected.isdigit() and len(body) < int(expected):
        raise OSError("the connection closed after %d of %s bytes" % (len(body), expected))
    body = body[:max_bytes]
    if "gzip" in headers.get("content-encoding", "").lower():  # sent although we asked for plain
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as packed:
                unpacked = packed.read(max_bytes + 1)
            body, truncated = unpacked[:max_bytes], truncated or len(unpacked) > max_bytes
        except (OSError, EOFError):
            raise OSError("the response was compressed in a way that could not be read") from None
    return RawResponse(handle.status, headers, body, truncated)


def urllib_transport(url: str, headers: Dict[str, str], timeout: float, max_bytes: int) -> RawResponse:
    """GET ``url`` without following redirects. Raises OSError on network failure."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http and https can be requested")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener.open(request, timeout=timeout) as handle:
            return _pack(handle, max_bytes)
    except urllib.error.HTTPError as error:
        with error:
            return _pack(error, max_bytes)

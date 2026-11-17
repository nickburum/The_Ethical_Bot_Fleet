"""The HTTP layer. Swap it for requests/httpx by passing ``transport=`` to Fleet."""

from __future__ import annotations

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
        headers[name] = headers[name] + ", " + value if name in headers else value
    body = handle.read(max_bytes + 1)
    return RawResponse(handle.status, headers, body[:max_bytes], len(body) > max_bytes)


def urllib_transport(url: str, headers: Dict[str, str], timeout: float, max_bytes: int) -> RawResponse:
    """GET ``url`` without following redirects. Raises OSError on network failure."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener.open(request, timeout=timeout) as handle:
            return _pack(handle, max_bytes)
    except urllib.error.HTTPError as error:
        with error:
            return _pack(error, max_bytes)

"""Fleet, Bot and Policy: the public API."""

from __future__ import annotations

import email.utils
import http.client
import ipaddress
import math
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from . import __version__
from .audit import AuditLog
from .limiter import BudgetExceeded, HostLimiter
from .robots import RobotsFile
from .signals import PURPOSES, decode, page_directives, violation
from .transport import RawResponse, urllib_transport

_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Names a bot may not use, because they belong to browsers or to someone else's crawler.
# Distinctive names are refused anywhere in the bot's name; short common words only at the start.
_IMPERSONATION_ANYWHERE = (
    "mozilla", "applewebkit", "gecko", "trident", "msie", "python-requests", "python-urllib",
    "google", "bing", "msnbot", "gptbot", "chatgpt", "openai", "oai-", "claude", "anthropic", "duckduck",
    "yandexbot", "baidu", "applebot", "bytespider", "perplexity", "amazonbot", "ahrefs", "semrush",
    "facebook", "meta-external", "twitterbot", "slurp", "petalbot",
)
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_CONTACT_URL = re.compile(r"^https?://[^\s()<>\"';]+$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # maps onto IPv4, including private IPv4
_IMPERSONATION_WORD = re.compile(r"^(chrome|chromium|safari|firefox|edge|opera|curl|wget|ccbot|yandex)(bot)?([^a-z]|$)")
# Errors the HTTP layer can raise: network failures and URLs it cannot send.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException, ValueError)
_ROBOTS_MAX_BYTES = 500 * 1024  # RFC 9309 asks parsers to handle at least 500 KiB
_ROBOTS_MAX_TTL = 24 * 3600  # ...and not to cache for more than 24 hours


class FleetError(Exception):
    """Base class for every error this library raises."""


class IdentityError(FleetError, ValueError):
    """The bot's name or contact details are not honest enough to run."""


class FetchError(FleetError):
    """The network request failed."""


class Blocked(FleetError):
    """The fleet refused the request. ``decision`` says why."""

    def __init__(self, decision: "Decision"):
        super().__init__("%s: %s" % (decision.reason, decision.detail))
        self.decision = decision


@dataclass(frozen=True)
class Policy:
    """The rules every bot in a fleet follows.

    There is no switch to ignore robots.txt. For sites you own, list them in
    ``trusted_domains``; that choice is written to the audit log.
    """

    min_delay: float = 1.0  # seconds between requests to one host
    max_crawl_delay: float = 60.0  # hosts asking for more are skipped, not hammered
    max_requests_per_host: Optional[int] = None
    allowed_domains: Optional[Sequence[str]] = None
    blocked_domains: Sequence[str] = ()
    trusted_domains: Sequence[str] = ()
    block_private_networks: bool = True
    max_retries: int = 2
    max_retry_after: float = 120.0
    max_redirects: int = 5
    timeout: float = 20.0  # seconds of silence before a connection is abandoned
    max_time: float = 60.0  # seconds allowed for one whole request, however slowly it trickles in
    max_bytes: int = 10 * 1024 * 1024
    robots_ttl: float = 3600.0

    def __post_init__(self) -> None:
        for name in ("min_delay", "max_crawl_delay", "max_retry_after", "timeout", "max_time", "robots_ttl"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("%s must be a number of seconds, zero or more (got %r)" % (name, value))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    url: str
    reason: str  # machine-readable, e.g. "robots_disallow"
    detail: str  # human-readable
    rule: Optional[str] = None
    crawl_delay: Optional[float] = None
    trusted: bool = False

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Response:
    url: str
    status: int
    headers: Dict[str, str]
    body: bytes
    directives: Set[str] = field(default_factory=set)  # e.g. {"noindex", "nofollow"}
    history: List[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return decode(self.headers, self.body)


@dataclass
class Result:
    """One entry from ``Bot.crawl``: either ``response`` or ``error`` is set."""

    url: str
    response: Optional[Response] = None
    error: Optional[FleetError] = None

    @property
    def blocked(self) -> bool:
        return isinstance(self.error, Blocked)


def _domain_in(host: str, domains: Iterable[str]) -> bool:
    return any(host == d.lower().rstrip(".") or host.endswith("." + d.lower().rstrip(".")) for d in domains)


def _remove_dots(path: str) -> str:
    """Resolve "." and ".." segments (also when written %2e), as a web server would."""
    segments = path.split("/")
    out: List[str] = []
    for index, segment in enumerate(segments):
        plain = segment.replace("%2e", ".").replace("%2E", ".")
        if plain in (".", ".."):
            if plain == ".." and len(out) > 1:
                out.pop()
            if index == len(segments) - 1:
                out.append("")
            continue
        out.append(segment)
    return "/".join(out) or "/"


def clean_url(url: str) -> str:
    """Put a URL in the form that is checked and sent: encoded, without dot segments or fragment."""
    url = quote(url.strip(), safe="%/:=&?~#+!$,;'@()*[]")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url  # refused later as a bad URL
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return urlunsplit((parts.scheme.lower(), parts.netloc, _remove_dots(parts.path or "/"), parts.query, ""))
    return url


def _canonical_host(host: str) -> str:
    """One spelling per host: 127.1, 0x7f.0.0.1 and 2130706433 are all 127.0.0.1."""
    host = host.lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(host.strip("[]").split("%")[0]))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fx.]+", host):
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            pass
    return host


def _origin(url: str) -> str:
    """The key for everything that is per-site: politeness, budget and the robots.txt cache."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = _canonical_host(parts.hostname or "")
    return "%s://%s:%d" % (scheme, "[%s]" % host if ":" in host else host, parts.port or (443 if scheme == "https" else 80))


def _location(base: str, value: str) -> str:
    """The absolute, cleaned target of a redirect."""
    try:  # header bytes arrive decoded as Latin-1; real-world non-ASCII targets are UTF-8
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return clean_url(urljoin(base, value))


def _resolve(host: str) -> List[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _retry_after(value: str, now: float) -> Optional[float]:
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError):
        return None


class Bot:
    """One honest bot. Create it with ``Fleet.bot(...)``."""

    def __init__(self, fleet: "Fleet", name: str, contact: str, purpose: str, version: str):
        if not _TOKEN.match(name):
            raise IdentityError("bot name must be a single token such as 'AcmeResearchBot', got %r" % name)
        lowered = name.lower()
        if any(word in lowered for word in _IMPERSONATION_ANYWHERE) or _IMPERSONATION_WORD.match(lowered):
            raise IdentityError("bot name %r imitates a browser or another crawler; pick your own name" % name)
        is_url = bool(_CONTACT_URL.match(contact)) and "." in (urlsplit(contact).hostname or "")
        if not (is_url or _EMAIL.match(contact)):
            raise IdentityError("contact must be a URL or an email address where site owners can reach you")
        if not _VERSION.match(version):
            raise IdentityError("version must look like 1.0 or 2024.1-beta, got %r" % version)
        if purpose not in PURPOSES:
            raise IdentityError("purpose must be one of %s, got %r" % (", ".join(PURPOSES), purpose))
        self.fleet = fleet
        self.name = name
        self.contact = contact
        self.purpose = purpose
        self.version = version
        self.user_agent = "%s/%s (+%s; purpose=%s) ethical-bot-fleet/%s" % (
            name, version, contact, purpose, __version__,
        )

    def check(self, url: str) -> Decision:
        """Ask whether this bot may fetch ``url``. Reads robots.txt, never the page."""
        return self.fleet._check(self, clean_url(url))

    def get(self, url: str) -> Response:
        """Fetch ``url``, or raise ``Blocked`` / ``FetchError``."""
        return self.fleet._get(self, clean_url(url))

    def crawl(self, urls: Iterable[str], workers: int = 8) -> Iterator[Result]:
        """Fetch many URLs concurrently, yielding results as they finish."""
        unique = list(dict.fromkeys(urls))

        def one(url: str) -> Result:
            try:
                return Result(url, response=self.get(url))
            except FleetError as error:
                return Result(url, error=error)
            except Exception as error:  # one odd URL must never end the whole crawl
                return Result(url, error=FetchError("%s: %s" % (url, error)))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for future in as_completed([pool.submit(one, url) for url in unique]):
                yield future.result()

    def __repr__(self) -> str:
        return "Bot(%r, purpose=%r)" % (self.name, self.purpose)


class Fleet:
    """A group of bots that share one policy, one politeness budget and one audit log."""

    def __init__(
        self,
        policy: Optional[Policy] = None,
        audit_log: Optional[str] = None,
        transport: Callable[[str, Dict[str, str], float, int], RawResponse] = urllib_transport,
        resolver: Callable[[str], List[str]] = _resolve,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.policy = policy or Policy()
        self.audit = AuditLog(audit_log)
        self.bots: List[Bot] = []
        self._transport = transport
        self._resolver = resolver
        self._clock = clock
        self._sleep = sleep
        self._limiter = HostLimiter(clock, sleep)
        self._robots: Dict[str, Tuple[float, RobotsFile]] = {}
        self._robots_locks: Dict[str, threading.Lock] = {}
        self._cooldown: Dict[str, float] = {}  # sites that asked this fleet to stay away, and until when
        self._lock = threading.Lock()

    def bot(self, name: str, contact: str, purpose: str = "other", version: str = "1.0") -> Bot:
        bot = Bot(self, name, contact, purpose, version)
        self.bots.append(bot)
        return bot

    # -- robots.txt -------------------------------------------------------

    def robots(self, bot: Bot, url: str) -> RobotsFile:
        """Return the (cached) robots.txt that governs ``url``."""
        origin = _origin(url)
        with self._lock:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:
            cached = self._robots.get(origin)
            if cached and cached[0] > self._clock():
                return cached[1]
            parts = urlsplit(url)
            # The robots.txt request is a visit like any other: it takes its turn in the same queue.
            with self._limiter.slot(origin, self.policy.min_delay, count=False):
                parsed, hold = self._fetch_robots(bot, "%s://%s/robots.txt" % (parts.scheme.lower(), parts.netloc.lower()))
            ttl = min(self.policy.robots_ttl, _ROBOTS_MAX_TTL)
            if parsed.mode in ("disallow_all", "unreachable"):
                # Look again in a minute, or when the server said to; never sooner.
                ttl = min(max(60.0, hold), _ROBOTS_MAX_TTL)
            self._robots[origin] = (self._clock() + ttl, parsed)
            return parsed

    def _request(self, bot: Bot, url: str, max_bytes: int) -> RawResponse:
        """One HTTP request with a hard overall time limit, so a trickling server cannot hold a bot."""
        box: Dict[str, object] = {}

        def work() -> None:
            try:
                box["raw"] = self._transport(url, {"User-Agent": bot.user_agent}, self.policy.timeout, max_bytes)
            except BaseException as error:  # handed back to the calling thread
                box["error"] = error

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(self.policy.max_time)
        if worker.is_alive():
            raise TimeoutError("no complete response within %g seconds" % self.policy.max_time)
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["raw"]  # type: ignore[return-value]

    def _fetch_robots(self, bot: Bot, url: str) -> Tuple[RobotsFile, float]:
        host = urlsplit(url).hostname
        try:
            for _ in range(6):
                raw = self._request(bot, url, _ROBOTS_MAX_BYTES)
                self.audit.write("robots", bot=bot.name, url=url, status=raw.status)
                location = raw.headers.get("location")
                if raw.status in (301, 302, 303, 307, 308) and location:
                    url = _location(url, location)
                    refusal = self._static(url)  # the same checks as any other address
                    if refusal:
                        return RobotsFile.disallow_all("robots.txt redirects somewhere this fleet may not go (%s)" % refusal[0]), 0.0
                    continue
                break
            else:
                return RobotsFile.disallow_all("robots.txt redirected too many times"), 0.0
        except _TRANSPORT_ERRORS as error:
            return RobotsFile.unreachable("could not connect to %s (%s)" % (host, error)), 0.0
        if 200 <= raw.status < 300:
            return RobotsFile.parse(raw.body.decode("utf-8", "surrogateescape")), 0.0
        if raw.status == 429 or raw.status >= 500:
            # RFC 9309: if the file is unreachable, assume complete disallow.
            hold = _retry_after(raw.headers.get("retry-after", ""), time.time()) or 0.0
            return RobotsFile.disallow_all("robots.txt returned HTTP %d" % raw.status), hold
        return RobotsFile.allow_all("no robots.txt (HTTP %d)" % raw.status), 0.0

    # -- decisions --------------------------------------------------------

    def _private(self, host: str) -> Optional[str]:
        if not self.policy.block_private_networks:
            return None
        try:
            addresses = [host.strip("[]")] if _is_ip(host) else self._resolver(host)
        except OSError:
            return None  # let the fetch itself report the DNS failure
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%")[0])
            if not ip.is_global or (ip.version == 6 and ip in _NAT64):
                return address
        return None

    def _static(self, url: str) -> Optional[Tuple[str, str]]:
        """Checks that need no request: is this an address the fleet may contact at all?"""
        policy = self.policy
        try:
            parts = urlsplit(url)
            parts.port  # raises ValueError for a port that is not a number
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return "bad_url", "this is not a valid URL"
        if "://" not in url:
            return "bad_url", "the address needs to start with https:// (did you mean https://%s ?)" % url
        if parts.scheme not in ("http", "https") or not host or any(c.isspace() or ord(c) < 32 for c in url):
            return "bad_url", "only http and https URLs can be fetched"
        if parts.username or parts.password:
            return "bad_url", "URLs with embedded credentials are not fetched"
        if ".." in host or any(len(label) > 63 for label in host.split(".")) or len(host) > 253:
            return "bad_url", "this is not a valid host name"
        if _domain_in(host, policy.blocked_domains):
            return "domain_blocked", "%s is on this fleet's blocked list" % host
        if policy.allowed_domains is not None and not _domain_in(host, policy.allowed_domains):
            return "domain_not_allowed", "%s is not on this fleet's allowed list" % host
        private = self._private(host)
        if private:
            return "private_network", "%s resolves to %s, which is not a public address" % (host, private)
        return None

    def _check(self, bot: Bot, url: str) -> Decision:
        decision = self._decide(bot, url)
        self.audit.write(
            "allow" if decision.allowed else "block",
            bot=bot.name, purpose=bot.purpose, url=url, reason=decision.reason,
            detail=decision.detail, rule=decision.rule, trusted=decision.trusted or None,
        )
        return decision

    def _decide(self, bot: Bot, url: str) -> Decision:
        policy = self.policy

        def no(reason: str, detail: str, rule: Optional[str] = None) -> Decision:
            return Decision(False, url, reason, detail, rule)

        refusal = self._static(url)
        if refusal:
            return no(*refusal)
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        away = self._cooldown.get(_origin(url), 0.0) - self._clock()
        if away > 0:
            return no("host_backoff", "%s asked this fleet to stay away; %d more seconds" % (host, math.ceil(away)))
        limit = policy.max_requests_per_host
        if limit is not None and self._limiter.count(_origin(url)) >= limit:
            return no("host_budget", "this fleet already made %d requests to %s" % (limit, host))
        if _domain_in(host, policy.trusted_domains):
            return Decision(True, url, "trusted", "%s is a trusted domain; robots.txt not consulted" % host, trusted=True)

        robots = self.robots(bot, url)
        verdict = robots.evaluate(bot.name, url)
        rule = str(verdict.rule) if verdict.rule else None
        if not verdict.allowed:
            if robots.mode == "unreachable":
                return no("site_unreachable", verdict.note)
            if robots.mode == "disallow_all":
                return no("robots_unreachable", verdict.note + "; treating the site as off limits")
            return no("robots_disallow", "robots.txt disallows this path for %s" % bot.name, rule)
        if verdict.signals.get(bot.purpose) is False:
            return no(
                "content_signal",
                "robots.txt says Content-Signal: %s=no" % bot.purpose,
                "Content-Signal: %s=no" % bot.purpose,
            )
        delay = verdict.crawl_delay
        if delay is not None and delay > policy.max_crawl_delay:
            return no(
                "crawl_delay_too_long",
                "site asks for %gs between requests, above this fleet's %gs limit" % (delay, policy.max_crawl_delay),
            )
        return Decision(True, url, "ok", verdict.note or "allowed by robots.txt", rule, delay)

    # -- fetching ---------------------------------------------------------

    def _get(self, bot: Bot, url: str) -> Response:
        history: List[str] = []
        for _ in range(self.policy.max_redirects + 1):
            decision = self._check(bot, url)
            if not decision.allowed:
                raise Blocked(decision)
            raw = self._send(bot, url, decision)
            location = raw.headers.get("location")
            if raw.status in (301, 302, 303, 307, 308) and location:
                self.audit.write("redirect", bot=bot.name, purpose=bot.purpose, url=url, status=raw.status)
                history.append(url)
                url = _location(url, location)  # the next hop is checked like any other URL
                continue
            directives: Set[str] = set()
            if not decision.trusted:
                directives = page_directives(raw.headers, raw.body, bot.name)
                broken = violation(bot.purpose, directives, raw.headers.get("content-type", ""))
                if broken:
                    blocked = Decision(
                        False, url, "page_opt_out",
                        "the page opts out of %s use (%s); content discarded" % (bot.purpose, broken), broken,
                    )
                    self.audit.write(
                        "block", bot=bot.name, purpose=bot.purpose, url=url,
                        reason=blocked.reason, detail=blocked.detail, rule=broken,
                    )
                    raise Blocked(blocked)
            self.audit.write(
                "fetch", bot=bot.name, purpose=bot.purpose, url=url, status=raw.status, bytes=len(raw.body),
            )
            return Response(url, raw.status, raw.headers, raw.body, directives, history, raw.truncated)
        blocked = Decision(False, url, "too_many_redirects", "gave up after %d redirects" % self.policy.max_redirects)
        self.audit.write("block", bot=bot.name, purpose=bot.purpose, url=url, reason=blocked.reason, detail=blocked.detail)
        raise Blocked(blocked)

    def _send(self, bot: Bot, url: str, decision: Decision) -> RawResponse:
        policy = self.policy
        host = _origin(url)
        delay = max(policy.min_delay, decision.crawl_delay or 0.0)
        attempt = 0
        while True:
            try:
                with self._limiter.slot(host, delay, policy.max_requests_per_host):
                    raw = self._request(bot, url, policy.max_bytes)
            except BudgetExceeded:
                blocked = Decision(
                    False, url, "host_budget",
                    "this fleet already made %d requests to %s" % (policy.max_requests_per_host, urlsplit(url).hostname),
                )
                self.audit.write("block", bot=bot.name, purpose=bot.purpose, url=url, reason=blocked.reason, detail=blocked.detail)
                raise Blocked(blocked)
            except _TRANSPORT_ERRORS as error:
                self.audit.write("error", bot=bot.name, purpose=bot.purpose, url=url, detail=str(error))
                raise FetchError("%s: %s" % (url, error)) from error
            if raw.status not in (429, 503):
                return raw
            asked = _retry_after(raw.headers.get("retry-after", ""), time.time())
            if asked is not None and asked > policy.max_retry_after:
                # A long break means stop: the whole site is off limits to every bot until it passes.
                self._cooldown[host] = self._clock() + min(asked, 24 * 3600.0)
                blocked = Decision(
                    False, url, "retry_after_too_long",
                    "server asked us to wait %gs (HTTP %d); leaving the site alone until then" % (asked, raw.status),
                )
                self.audit.write("block", bot=bot.name, purpose=bot.purpose, url=url, reason=blocked.reason, detail=blocked.detail)
                raise Blocked(blocked)
            attempt += 1
            wait = asked if asked is not None else min(delay * (2 ** attempt), policy.max_retry_after)
            self._limiter.penalize(host, wait)  # honoured whether or not this URL is tried again
            if attempt > policy.max_retries:
                return raw
            self.audit.write("retry", bot=bot.name, purpose=bot.purpose, url=url, status=raw.status, wait=round(wait, 3))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%")[0])
        return True
    except ValueError:
        return False

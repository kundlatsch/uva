"""Reading a URL: a web page, a PDF, plain text or JSON, as clipped text.

Every request goes through an SSRF guard: only http(s), and the host must
resolve to public addresses only. Loopback, private (RFC 1918), link-local,
CGNAT, multicast, reserved, IPv6 ULA/link-local, ``localhost``,
``host.docker.internal`` and single-label names (the compose services:
``mongo``, ``redis``, ``mcp``, ``dashboard``, ``shell``) are refused. The
address checked is the address used: the request goes to that IP (the
hostname stays in the Host header and TLS SNI), so a DNS answer can't change
between the check and the connection. Each redirect hop is checked again.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15
CACHE_SECONDS = 3600
CACHE_PREFIX = "webfetch:"
USER_AGENT = "Mozilla/5.0 (compatible; UVA personal assistant)"
BLOCKED_NAMES = frozenset({"localhost", "host.docker.internal", "gateway.docker.internal", "metadata.google.internal"})
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
# Not "global" in ipaddress terms but listed for clarity: CGNAT.
CGNAT = ipaddress.ip_network("100.64.0.0/10")
REDIRECTS = (301, 302, 303, 307, 308)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(RuntimeError):
    """The URL could not be read; ``code`` picks the message shown."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class BlockedURL(FetchError):
    """Refused by the SSRF guard."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str  # as asked
    final_url: str  # after redirects
    status: int
    title: str
    text: str
    truncated: bool
    content_type: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> FetchResult:
        return cls(**json.loads(raw))


# --- the guard ------------------------------------------------------------------------


def blocked_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_private
        or ip.is_reserved or ip.is_unspecified or (ip.version == 4 and ip in CGNAT)
    )


def check_host(host: str | None) -> str:
    """The host name, lowercased, or BlockedURL for names that are never public."""
    if not host:
        raise BlockedURL("bad_url")
    name = host.strip("[]").rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        if name in BLOCKED_NAMES or name.endswith(BLOCKED_SUFFIXES) or "." not in name:
            raise BlockedURL("private_host", name) from None
    return name


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def safe_address(host: str, port: int, resolver: Resolver | None = None) -> str:
    """The address to connect to: every address the name resolves to must be public."""
    name = check_host(host)
    try:
        ipaddress.ip_address(name)
        addresses = [name]
    except ValueError:
        try:
            addresses = await (resolver or _resolve)(name, port)
        except (OSError, UnicodeError) as exc:
            raise FetchError("dns", name) from exc
    if not addresses:
        raise FetchError("dns", name)
    for address in addresses:
        if blocked_ip(address.split("%", 1)[0]):
            raise BlockedURL("private_address", f"{name} -> {address}")
    return addresses[0]


def check_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an http(s) URL; BlockedURL otherwise."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise BlockedURL("bad_url", url[:100]) from exc
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("scheme", parts.scheme or "?")
    if parts.username or parts.password:
        raise BlockedURL("credentials")
    host = check_host(parts.hostname)
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


# --- reading ------------------------------------------------------------------------------


async def _read(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            return b"".join(chunks)[:limit], True
    return b"".join(chunks), False


async def _get(client: httpx.AsyncClient, url: str, resolver: Resolver | None) -> tuple[str, httpx.Response]:
    """GET with every redirect hop checked; returns (final URL, open streaming response)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port = check_url(current)
        address = await safe_address(host, port, resolver)
        parts = urlsplit(current)
        netloc = f"[{address}]" if ":" in address else address
        default_port = port == (443 if scheme == "https" else 80)
        target = urlunsplit((scheme, netloc if default_port else f"{netloc}:{port}", parts.path or "/",
                             parts.query, ""))
        headers = {"Host": host if default_port else f"{host}:{port}", "User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,text/plain,application/json,application/pdf;q=0.9,*/*;q=0.5"}
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        request = client.build_request("GET", target, headers=headers, extensions=extensions)
        response = await client.send(request, stream=True)
        location = response.headers.get("location")
        if response.status_code in REDIRECTS and location:
            await response.aclose()
            current = urljoin(current, location)
            continue
        return current, response
    raise FetchError("redirects", str(MAX_REDIRECTS))


class _TextParser(HTMLParser):
    """Fallback HTML -> text: drops scripts, styles and navigation."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)


def html_to_text(page: str, url: str = "") -> tuple[str, str]:
    """(title, Markdown-ish text) of an HTML page."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    title = html.unescape(" ".join(title_match.group(1).split())) if title_match else ""
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            page, url=url or None, output_format="markdown", include_links=False, include_comments=False,
            favor_recall=True,
        ) or ""
    except Exception:
        text = ""
    if not text.strip():
        parser = _TextParser()
        parser.feed(page)
        text = "".join(parser.parts)
        title = title or " ".join(parser.title.split())
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return title, re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(body: bytes, response: httpx.Response) -> str:
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch(
    url: str, *, max_chars: int = 12000, resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None, cache=None, timeout: float = TIMEOUT_SECONDS,
) -> FetchResult:
    """Read ``url`` (raises FetchError / BlockedURL). ``cache`` is a Redis client."""
    key = CACHE_PREFIX + hashlib.sha256(f"{url}|{max_chars}".encode()).hexdigest()
    if cache is not None:
        try:
            if raw := await cache.get(key):
                return FetchResult.from_json(raw)
        except Exception:
            pass
    check_url(url)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport, trust_env=False,
    ) as client:
        try:
            final_url, response = await _get(client, url, resolver)
        except httpx.HTTPError as exc:
            raise FetchError("network", type(exc).__name__) from exc
        try:
            body, cut = await _read(response, MAX_BYTES)
        except httpx.HTTPError as exc:
            raise FetchError("network", type(exc).__name__) from exc
        finally:
            await response.aclose()
    if response.status_code >= 400:
        raise FetchError("status", str(response.status_code))
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    title = ""
    if content_type in ("text/html", "application/xhtml+xml") or (not content_type and b"<html" in body[:2000].lower()):
        title, text = await asyncio.to_thread(html_to_text, _decode(body, response), final_url)
    elif content_type == "application/pdf" or final_url.lower().endswith(".pdf"):
        from uva.documents import DocumentError, extract

        if cut:
            raise FetchError("too_big", f"{MAX_BYTES // (1024 * 1024)} MB")
        try:
            text = (await asyncio.to_thread(extract, "page.pdf", body, max_chars=max_chars + 1)).text
        except DocumentError as exc:
            raise FetchError("unreadable", str(exc)) from exc
    elif content_type.startswith("text/") or content_type in ("application/json", "application/ld+json") or content_type.endswith("+json"):
        text = _decode(body, response)
        if "json" in content_type:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except ValueError:
                pass
    else:
        raise FetchError("content_type", content_type or "?")
    text = text.strip()
    truncated = cut or len(text) > max_chars
    result = FetchResult(url, final_url, response.status_code, title, text[:max_chars], truncated, content_type)
    if cache is not None:
        try:
            await cache.set(key, result.to_json(), ex=CACHE_SECONDS)
        except Exception:
            pass
    return result


# --- links in a message ------------------------------------------------------------------

# A bare link: not inside a Markdown link "[x](url)" or an angle-bracket <url>.
_LINK = re.compile(r"(?<![\(<\[])\bhttps?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)", re.IGNORECASE)


def bare_links(text: str, limit: int = 2) -> list[str]:
    """Up to ``limit`` distinct bare http(s) links of ``text``, in order."""
    text = _MARKDOWN_LINK.sub(" ", text)
    out: list[str] = []
    for match in _LINK.finditer(text):
        link = match.group(0).rstrip(".,;:!?…")
        if link not in out:
            out.append(link)
        if len(out) >= limit:
            break
    return out


async def fetch_for(services, url: str) -> FetchResult:
    """fetch() with UVA's settings and the Redis cache (when there is one)."""
    cache = services.busy.redis if services.busy is not None else None
    return await fetch(url, max_chars=services.settings.web_fetch_max_chars, cache=cache)


def describe_error(exc: FetchError) -> str:
    from uva.i18n import t

    return t(f"web.errors.{exc.code}", detail=exc.detail)


def for_llm(result: FetchResult) -> str:
    """A read page as data for a prompt (lines that look like orders removed)."""
    from uva.safety import as_data, sanitize_untrusted

    text, _ = sanitize_untrusted(result.text)
    head = f"{result.title}\n" if result.title else ""
    cut = " (clipped)" if result.truncated else ""
    return as_data(f"page {result.final_url}{cut}", f"{head}{text}")

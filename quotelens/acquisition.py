"""Bounded public-web acquisition. All returned page contents are untrusted data.

HTTP connections are pinned to a checked public address. The browser never owns
network access: its read-only requests are fulfilled by the same HTTPS transport.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

from .source_documents import classify_page


USER_AGENT = "QuoteLensResearch/1.0"
MAX_BYTES = 1_500_000
MAX_TEXT = 500_000
MAX_LINKS = 80
MAX_REDIRECTS = 5
MAX_BROWSER_REQUESTS = 40
MAX_BROWSER_BYTES = 6_000_000
MAX_IMAGE_BYTES = 4_000_000
_DNS = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="quotelens-dns")
_SECRET_QUERY = re.compile(r"^(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|authorization|password|passwd|secret|client[_-]?secret|session(?:id)?|signature|sig)$", re.I)


class AcquisitionError(RuntimeError):
    """A source could not be read within the public-source policy."""
    def __init__(self, message, *, code="source_error", retryable=False, http_status=None):
        super().__init__(message)
        self.code, self.retryable, self.http_status = code, retryable, http_status


@dataclass
class _Response:
    url: str
    status: int
    body: bytes
    headers: dict

    @property
    def mime(self):
        return self.headers.get("content-type", "text/html").split(";", 1)[0].strip().lower()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcquisitionError("Истёк лимит времени чтения источника.")
    return remaining


def _domain(value):
    value = str(value).strip().lower().rstrip(".")
    if "://" in value:
        value = urllib.parse.urlsplit(value).hostname or ""
    try:
        return value.encode("idna").decode("ascii").removeprefix("www.")
    except UnicodeError:
        raise AcquisitionError("Некорректный домен источника.") from None


def _is_public_address(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not (address.is_reserved or address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local or getattr(address, "ipv4_mapped", None))


def _normalize_url(url):
    if not isinstance(url, str) or len(url) > 4096 or "\\" in url or re.search(r"[\x00-\x20\x7f]", url):
        raise AcquisitionError("Некорректная ссылка источника.")
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if "%" in host or host.endswith((".localhost", ".local", ".internal", ".onion")) or host == "localhost":
            raise ValueError()
        try:
            if not _is_public_address(host):
                raise AcquisitionError("Локальные и служебные адреса источников запрещены.")
        except ValueError:
            pass  # A DNS hostname is validated by resolving all addresses before I/O.
        if any(_SECRET_QUERY.match(k) for k, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)):
            raise AcquisitionError("Ссылки с учётными данными и секретами не читаются.")
        netloc = "[" + host + "]" if ":" in host else host
        # Preserve the source's query and escaping, but make Unicode paths valid HTTP.
        path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(parsed.query, safe="%/:@!$&'()*+,;=?-._~")
        return urllib.parse.urlunsplit(("https", netloc, path, query, ""))
    except (ValueError, UnicodeError):
        raise AcquisitionError("Разрешены только публичные HTTPS-страницы на порту 443 без учётных данных.") from None


def _public_addresses(host, deadline):
    future = None
    try:
        future = _DNS.submit(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
        records = future.result(timeout=min(4, _remaining(deadline)))
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not _is_public_address(addr) for addr in addresses):
            raise AcquisitionError("Локальные и служебные адреса источников запрещены.")
        return addresses
    except AcquisitionError:
        raise
    except (OSError, ValueError, concurrent.futures.TimeoutError):
        if future:
            future.cancel()
        raise AcquisitionError("Нашему серверу не удалось определить публичный адрес сайта (DNS).", code="dns_resolution", retryable=True) from None


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        super().__init__(host, port=443, timeout=timeout, context=context)
        self._address = address

    def connect(self):
        # Never resolve self.host again after validation; retain hostname for SNI/TLS.
        sock = socket.create_connection((self._address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.h1_parts, self.links = [], [], [], []
        self.skip, self.title, self.h1 = 0, False, False
        self.h1_seen = False
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.skip += 1
        if tag == "title":
            self.title = True
        if tag == "h1" and not self.h1_seen:
            self.h1 = True
            self.h1_seen = True
        if tag == "a" and attrs.get("href") and not self.skip:
            self.anchor = [attrs["href"], []]
        if tag in {"p", "div", "tr", "td", "th", "br", "h1", "h2", "li", "dt", "dd", "section"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.title = False
        if tag == "h1":
            self.h1 = False
        if tag == "a" and self.anchor:
            self.links.append((self.anchor[0], _space(" ".join(self.anchor[1]))))
            self.anchor = None

    def handle_data(self, data):
        if self.title:
            self.title_parts.append(data)
        if self.skip:
            return
        self.parts.append(data)
        if self.h1:
            self.h1_parts.append(data)
        if self.anchor:
            self.anchor[1].append(data)


def _space(text):
    return re.sub(r"\s+", " ", text).strip()


def _decode(response):
    match = re.search(r"charset=[\"']?([\w-]+)", response.headers.get("content-type", ""), re.I)
    if not match:
        match = re.search(br"charset\s*=\s*[\"']?([\w-]+)", response.body[:4096], re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", errors="replace")
    try:
        return response.body.decode(charset, errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


class _RobotRules:
    """RFC 9309 wildcard/end matching, longest rule wins, Allow wins ties."""
    def __init__(self, text):
        groups, agents, rules, delay = [], [], [], 0
        seen_rules = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if seen_rules:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, seen_rules = [], [], 0, False
                agents.append(value.lower())
            elif agents and name in {"allow", "disallow", "crawl-delay"}:
                seen_rules = True
                if name == "crawl-delay":
                    try:
                        delay = max(delay, min(3600, max(0, float(value))))
                    except ValueError:
                        pass
                elif value:
                    rules.append((value, name == "allow"))
        if agents:
            groups.append((agents, rules, delay))
        matched = []
        for agents, rules, delay in groups:
            score = max((0 if a == "*" else len(a) for a in agents if a == "*" or a in USER_AGENT.lower()), default=-1)
            if score >= 0:
                matched.append((score, rules, delay))
        best = max((score for score, _, _ in matched), default=-1)
        self.rules = [rule for score, rules, _ in matched if score == best for rule in rules]
        self.delay = max((delay for score, _, delay in matched if score == best), default=0)

    def allowed(self, url):
        parsed = urllib.parse.urlsplit(url)
        target = urllib.parse.unquote(parsed.path or "/") + ("?" + urllib.parse.unquote(parsed.query) if parsed.query else "")
        matched = []
        for rule, allow in self.rules:
            raw = urllib.parse.unquote(rule)
            end = raw.endswith("$")
            if end:
                raw = raw[:-1]
            pattern = "^" + ".*".join(re.escape(part) for part in raw.split("*")) + ("$" if end else "")
            if re.search(pattern, target):
                matched.append((len(raw.replace("*", "")), allow))
        return max(matched, default=(0, True))[1]


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hits, self.anchor, self.snippet = [], None, None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        self.depth += 1 if tag not in {"br", "img", "meta", "input", "link", "hr"} else 0
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and any(c in classes for c in ("result-link", "result__a")):
            self.anchor = [attrs.get("href", ""), []]
        if any(c in classes for c in ("result-snippet", "result__snippet")):
            self.snippet = [self.depth, []]

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            self.hits.append({"url": self.anchor[0], "title": _space(" ".join(self.anchor[1])), "snippet": ""})
            self.anchor = None
        if self.snippet and self.depth == self.snippet[0]:
            if self.hits:
                self.hits[-1]["snippet"] = _space(" ".join(self.snippet[1]))
            self.snippet = None
        if tag not in {"br", "img", "meta", "input", "link", "hr"}:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.anchor:
            self.anchor[1].append(data)
        if self.snippet:
            self.snippet[1].append(data)


def _raster_mime(body):
    """Recognize raster containers; HTML/SVG and oversized canvases fail closed."""
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 45 and body[12:16] == b"IHDR" and body[-8:-4] == b"IEND":
        width, height = int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
        mime = "image/png"
    elif body[:6] in {b"GIF87a", b"GIF89a"} and len(body) >= 14 and body.endswith(b";"):
        width, height = int.from_bytes(body[6:8], "little"), int.from_bytes(body[8:10], "little")
        mime = "image/gif"
    elif body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9"):
        width = height = 0
        cursor = 2
        while cursor + 8 < len(body):
            if body[cursor] != 255:
                break
            marker = body[cursor + 1]
            if marker in {0xD8, 0xD9, 0xDA}:
                break
            size = int.from_bytes(body[cursor + 2:cursor + 4], "big")
            if size < 2 or cursor + size + 2 > len(body):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = int.from_bytes(body[cursor + 5:cursor + 7], "big"), int.from_bytes(body[cursor + 7:cursor + 9], "big")
                break
            cursor += size + 2
        mime = "image/jpeg"
    elif len(body) >= 30 and body[:4] == b"RIFF" and body[8:12] == b"WEBP" and int.from_bytes(body[4:8], "little") + 8 == len(body):
        kind = body[12:16]
        if kind == b"VP8X":
            width, height = 1 + int.from_bytes(body[24:27], "little"), 1 + int.from_bytes(body[27:30], "little")
        elif kind == b"VP8 " and body[23:26] == b"\x9d\x01\x2a":
            width, height = int.from_bytes(body[26:28], "little") & 0x3FFF, int.from_bytes(body[28:30], "little") & 0x3FFF
        elif kind == b"VP8L" and body[20] == 0x2F:
            bits = int.from_bytes(body[21:25], "little")
            width, height = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        else:
            return None
        mime = "image/webp"
    else:
        return None
    return mime if 0 < width <= 16000 and 0 < height <= 16000 and width * height <= 40_000_000 else None


def acquire_image(url, timeout=8):
    """Read a source-bound raster through the same pinned public HTTPS policy.

    This helper accepts no browser session, credentials or arbitrary headers.
    The caller must obtain its URL from a stored primary product observation.
    """
    normalized = _normalize_url(url)
    acquirer = Acquisition(timeout=timeout)
    deadline = time.monotonic() + acquirer.timeout
    # This is an embedded raster requested for viewing a saved product, not a
    # crawl/navigation. Navigation keeps its robots policy; the raster path
    # still pins public DNS and checks every redirect, size and MIME/signature.
    response = acquirer._request(normalized, deadline, robots=False, max_bytes=MAX_IMAGE_BYTES,
                                 accept="image/jpeg,image/png,image/webp,image/gif")
    mime = _raster_mime(response.body)
    if not 200 <= response.status < 300 or not mime or response.mime != mime or len(response.body) > MAX_IMAGE_BYTES:
        raise AcquisitionError("Источник не вернул допустимое растровое изображение товара.")
    return {"body": response.body, "mime": mime, "url": response.url,
            "sha256": hashlib.sha256(response.body).hexdigest()}


class Acquisition:
    def __init__(self, allowed_domains=None, timeout=18):
        self.allowed_domains = None if allowed_domains is None else {_domain(x) for x in allowed_domains}
        self.timeout = max(.1, min(60.0, float(timeout)))
        self._robots = {}
        self._robots_lock = threading.Lock()
        self._last_access = {}

    def _scoped(self, url, scoped=True):
        url = _normalize_url(url)
        host = urllib.parse.urlsplit(url).hostname
        if scoped and self.allowed_domains is not None and not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise AcquisitionError("Домен не входит в выбранные источники.")
        return url

    def _request(self, url, deadline, *, scoped=True, robots=True, max_bytes=MAX_BYTES,
                 accept="text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.2"):
        url = self._scoped(url, scoped)
        for redirect in range(MAX_REDIRECTS + 1):
            if robots:
                self._check_robots(url, deadline, scoped=scoped)
            host = urllib.parse.urlsplit(url).hostname
            addresses = _public_addresses(host, deadline)
            conn, watchdog = None, None
            try:
                last_error = None
                for address in addresses[:4]:
                    conn = _PinnedHTTPS(host, address, min(6, _remaining(deadline)))
                    transport = [None]

                    def expire_connection(connection=conn, sockets=transport):
                        sock = sockets[0] or connection.sock
                        if sock:
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            sock.close()

                    watchdog = threading.Timer(_remaining(deadline), expire_connection)
                    watchdog.daemon = True
                    watchdog.start()
                    try:
                        parsed = urllib.parse.urlsplit(url)
                        conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip, deflate", "Connection": "close"})
                        network_socket = conn.sock
                        transport[0] = network_socket
                        if network_socket:
                            network_socket.settimeout(min(6, _remaining(deadline)))
                        response = conn.getresponse()
                        break
                    except (OSError, http.client.HTTPException) as exc:
                        last_error = exc
                        watchdog.cancel()
                        conn.close()
                else:
                    raise AcquisitionError("Наш сервер не смог установить защищённое HTTPS-соединение с источником.", code="https_connection", retryable=not isinstance(last_error, ssl.SSLCertVerificationError)) from last_error
                headers = {key.lower(): value for key, value in response.getheaders() if key.lower() in {"content-type", "content-length", "content-encoding", "location"}}
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect == MAX_REDIRECTS or not headers.get("location"):
                        raise AcquisitionError("Превышен лимит перенаправлений источника.")
                    url = self._scoped(urllib.parse.urljoin(url, headers["location"]), scoped)
                    continue
                encoding = headers.get("content-encoding", "identity").lower()
                if encoding not in {"", "identity", "gzip", "deflate"}:
                    raise AcquisitionError("Источник вернул неподдерживаемое сжатие.", code="unsupported_encoding")
                try:
                    if int(headers.get("content-length", "0")) > max_bytes:
                        raise AcquisitionError("Страница превышает лимит размера.")
                except ValueError:
                    pass
                chunks, size = [], 0
                while True:
                    if network_socket:
                        try:
                            network_socket.settimeout(min(6, _remaining(deadline)))
                        except OSError:
                            pass  # HTTPResponse may already have closed a consumed body.
                    _remaining(deadline)
                    chunk = response.read1(min(65536, max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise AcquisitionError("Страница превышает лимит размера.")
                body = b"".join(chunks)
                if encoding in {"gzip", "deflate"}:
                    try:
                        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
                        decoded = decoder.decompress(body, max_bytes + 1)
                        if len(decoded) > max_bytes or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
                            raise ValueError("Invalid or oversized compressed response")
                    except (zlib.error, ValueError):
                        raise AcquisitionError("Сжатая страница повреждена или превышает лимит размера.", code="invalid_compression") from None
                    body = decoded
                    headers.pop("content-encoding", None)
                    headers["content-length"] = str(len(body))
                return _Response(url, response.status, body, headers)
            except AcquisitionError:
                raise
            except (OSError, http.client.HTTPException, ValueError):
                raise AcquisitionError("Наш сервер не смог прочитать источник в пределах лимита времени.", code="read_timeout", retryable=True) from None
            finally:
                if watchdog:
                    watchdog.cancel()
                if conn:
                    conn.close()
        raise AcquisitionError("Превышен лимит перенаправлений источника.")

    def _check_robots(self, url, deadline, *, scoped=True):
        parsed = urllib.parse.urlsplit(url)
        origin = "https://" + parsed.netloc
        with self._robots_lock:
            cached = self._robots.get(origin)
        if not cached or time.monotonic() - cached[0] > 1800:
            response = self._request(origin + "/robots.txt", deadline, scoped=scoped, robots=False, max_bytes=256_000)
            if response.status in {404, 410}:
                rules = _RobotRules("")
            elif 200 <= response.status < 300:
                # HTML CAPTCHA pages at robots.txt are not permission to crawl.
                text = _decode(response)
                if "<html" in text[:500].lower():
                    raise AcquisitionError("Не удалось проверить robots.txt источника.")
                rules = _RobotRules(text)
            elif response.status in {401, 403}:
                rules = _RobotRules("User-agent: *\nDisallow: /")
            else:
                raise AcquisitionError("Не удалось проверить robots.txt источника (HTTP %d)." % response.status)
            with self._robots_lock:
                self._robots[origin] = (time.monotonic(), rules)
        else:
            rules = cached[1]
        if not rules.allowed(url):
            raise AcquisitionError("Robots.txt источника запрещает автоматическое чтение этой страницы.")
        with self._robots_lock:
            now = time.monotonic()
            wait = max(0, self._last_access.get(origin, 0) + rules.delay - now)
            if wait >= _remaining(deadline):
                raise AcquisitionError("Задержка из robots.txt превышает оставшийся лимит времени.")
            self._last_access[origin] = now + wait
        if wait:
            time.sleep(wait)

    def _document(self, response, method, *, text=None, primary_title=None, seo_title=None):
        raw = _decode(response)
        parser = _PageParser()
        is_html = response.mime in {"text/html", "application/xhtml+xml"}
        if is_html:
            parser.feed(raw)
        extracted = text if text is not None else _space(" ".join(parser.parts)) if is_html else raw
        primary_title = _space(primary_title if primary_title is not None else " ".join(parser.h1_parts))
        seo_title = _space(seo_title if seo_title is not None else " ".join(parser.title_parts))
        heading = primary_title or seo_title
        links, seen = [], set()
        for href, label in parser.links:
            try:
                target = self._scoped(urllib.parse.urljoin(response.url, href))
                # Links are candidates only. DNS/public-IP is checked on actual reads.
            except AcquisitionError:
                continue
            if target not in seen:
                links.append({"title": label[:240], "url": target})
                seen.add(target)
        doc = {"url": response.url, "title": heading[:500], "primary_title": primary_title, "seo_title": seo_title, "text": extracted[:MAX_TEXT], "sha256": hashlib.sha256(response.body).hexdigest(), "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method, "links": links, "content_type": response.mime, "bytes": len(response.body), "truncated": len(extracted) > MAX_TEXT, "untrusted": True}
        if is_html:
            doc["html"] = raw
        classification = classify_page(doc)
        # Keep the acquisition policy authoritative: HTML classification may
        # discover links beyond the original navigation-heavy return window.
        allowed = {link["url"]: link for link in links}
        product_links = [link for link in classification["product_links"] if link["url"] in allowed]
        product_urls = {link["url"] for link in product_links}
        doc["product_links"] = product_links
        doc["links"] = (product_links + [link for link in links if link["url"] not in product_urls])[:MAX_LINKS]
        doc["page_kind"] = classification["kind"]
        doc["page_kind_reason"] = classification["reason"]
        return doc

    def open(self, url, browser=False):
        deadline = time.monotonic() + self.timeout
        url = self._scoped(url)
        if browser:
            return self._browser(url, deadline)
        response = self._request(url, deadline)
        if not 200 <= response.status < 300:
            raise AcquisitionError("Источник ответил нашему серверу HTTP %d." % response.status, code="http_status", http_status=response.status, retryable=response.status in {408, 429, 500, 502, 503, 504})
        if response.mime not in {"text/html", "application/xhtml+xml", "text/plain", "application/json"}:
            raise AcquisitionError("Формат источника не поддерживается: нужна текстовая страница.")
        return self._document(response, "http")

    def search(self, query, limit=6):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise AcquisitionError("Поисковый запрос должен содержать от 1 до 500 символов.")
        query = _space(query)
        limit = max(1, min(10, int(limit)))
        effective = query
        if self.allowed_domains is not None:
            if not self.allowed_domains:
                return {"query": query, "hits": [], "document": None, "engine": None, "status": "unavailable", "reason": "Не выбраны домены источников."}
            effective += " (" + " OR ".join("site:" + d for d in sorted(self.allowed_domains)) + ")"
        deadline = time.monotonic() + self.timeout
        errors, last_document = [], None
        for engine, base in (("duckduckgo-lite", "https://lite.duckduckgo.com/lite/?q="), ("duckduckgo-html", "https://html.duckduckgo.com/html/?q=")):
            try:
                response = self._request(base + urllib.parse.quote(effective), deadline, scoped=False)
                if response.status != 200:
                    raise AcquisitionError("Поисковик вернул HTTP %d (возможна проверка доступа)." % response.status)
                raw = _decode(response)
                parser = _SearchParser()
                parser.feed(raw)
                last_document = self._document(response, "search")
                hits, seen = [], set()
                for hit in parser.hits:
                    target = urllib.parse.urljoin(response.url, hit["url"])
                    parsed = urllib.parse.urlsplit(target)
                    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                        redirect_url = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
                        if not redirect_url:
                            continue
                        target = redirect_url
                    try:
                        target = self._scoped(target)
                        _public_addresses(urllib.parse.urlsplit(target).hostname, deadline)
                    except AcquisitionError:
                        continue
                    if target in seen:
                        continue
                    hits.append({"title": hit["title"][:300], "url": target, "snippet": hit["snippet"][:1200]})
                    seen.add(target)
                    if len(hits) >= limit:
                        break
                if hits:
                    return {"query": query, "hits": hits, "document": last_document, "engine": engine, "status": "ok"}
                if re.search(r"captcha|anomaly\.js|challenge-form|unusual traffic|verify you are human", raw, re.I):
                    raise AcquisitionError("Поисковик требует CAPTCHA; проверка не обходится.")
                if re.search(r"no (?:more )?results|результат\w* не найден|nothing found", raw, re.I):
                    return {"query": query, "hits": [], "document": last_document, "engine": engine, "status": "empty", "reason": "Поисковик не вернул подходящих публичных HTTPS-ссылок."}
                raise AcquisitionError("В ответе поисковика нет проверяемых ссылок на источники.")
            except AcquisitionError as exc:
                errors.append(engine + ": " + str(exc))
        return {"query": query, "hits": [], "document": last_document, "engine": None, "status": "unavailable", "reason": "; ".join(errors)}

    def _browser(self, url, deadline):
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError:
            raise AcquisitionError("JS-браузер недоступен: на сервере не установлен Playwright.") from None
        # Validate before launching, including robots and public DNS for top-level URL.
        self._check_robots(url, deadline)
        _public_addresses(urllib.parse.urlsplit(url).hostname, deadline)
        counters = {"requests": 0, "bytes": 0, "blocked": 0}
        main_error, main_responses = [], []
        watchdog = None
        with tempfile.TemporaryDirectory(prefix="quotelens-browser-") as profile:
            # Chromium receives no model keys, proxy credentials, or owner's HOME.
            browser_env = {"PATH": os.defpath, "HOME": profile, "TMPDIR": tempfile.gettempdir(), "XDG_CACHE_HOME": profile, "XDG_CONFIG_HOME": profile, "LANG": "C.UTF-8"}
            try:
                with sync_playwright() as playwright:
                    # Playwright's DOM read APIs have no operation timeout. A deadline
                    # kills only this call's driver, closing its browser pipe as well.
                    driver = getattr(playwright._impl_obj._connection._transport, "_proc", None)
                    if driver is None:
                        raise AcquisitionError("JS-браузер не поддерживает обязательный лимит времени.")

                    def expire_browser():
                        try:
                            driver.kill()
                        except (ProcessLookupError, OSError):
                            pass

                    watchdog = threading.Timer(_remaining(deadline), expire_browser)
                    watchdog.daemon = True
                    watchdog.start()
                    executable = os.getenv("BROWSER_EXECUTABLE_PATH")
                    if not executable:
                        executable = next((shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome") if shutil.which(name)), None)
                    browser = playwright.chromium.launch(headless=True, executable_path=executable, env=browser_env, timeout=max(1, _remaining(deadline) * 1000), args=["--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-features=MediaRouter,DnsOverHttps,WebTransport", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>"])
                    try:
                        context = browser.new_context(user_agent=USER_AGENT, service_workers="block", accept_downloads=False, java_script_enabled=True, viewport={"width": 1280, "height": 800})
                        context.clear_cookies()
                        if hasattr(context, "route_web_socket"):
                            context.route_web_socket("**/*", lambda websocket: websocket.close())

                        def route_request(route):
                            request = route.request
                            is_main = request.is_navigation_request() and request.frame == page.main_frame
                            try:
                                if counters["requests"] >= MAX_BROWSER_REQUESTS or counters["bytes"] >= MAX_BROWSER_BYTES:
                                    raise AcquisitionError("Достигнут лимит сетевых ресурсов JS-страницы.")
                                if request.method != "GET" or request.resource_type not in {"document", "script", "stylesheet", "xhr", "fetch"}:
                                    raise AcquisitionError("Браузер разрешает только чтение документов, скриптов и данных.")
                                counters["requests"] += 1
                                resource_deadline = deadline if is_main else min(deadline - 4, time.monotonic() + 2)
                                response = self._request(request.url, resource_deadline, max_bytes=min(MAX_BYTES, MAX_BROWSER_BYTES - counters["bytes"]))
                                counters["bytes"] += len(response.body)
                                if is_main and not 200 <= response.status < 300:
                                    raise AcquisitionError("Источник вернул HTTP %d." % response.status)
                                if is_main:
                                    main_responses.append(response)
                                # Never pass cookies/auth or source-provided cross-origin grants.
                                headers = {"content-type": response.headers.get("content-type", "text/plain"), "cache-control": "no-store"}
                                route.fulfill(status=response.status, body=response.body, headers=headers)
                            except AcquisitionError as exc:
                                counters["blocked"] += 1
                                if is_main:
                                    main_error.append(str(exc))
                                try:
                                    route.abort("blockedbyclient")
                                except Exception:
                                    pass  # The owned deadline watchdog may have closed this driver.
                            except Exception:
                                counters["blocked"] += 1
                                if is_main:
                                    main_error.append("JS-браузер закрыл соединение страницы.")

                        context.route("**/*", route_request)
                        page = context.new_page()
                        page.set_default_timeout(max(1, _remaining(deadline) * 1000))
                        try:
                            try:
                                page.goto(url, wait_until="domcontentloaded", timeout=max(1, (_remaining(deadline) - 3) * 1000))
                            except PlaywrightError:
                                if not main_responses:
                                    raise
                            # A bounded settling interval lets real JS/XHR populate price/specs.
                            page.wait_for_timeout(min(1200, max(0, _remaining(deadline) * 1000 - 300)))
                            if main_error:
                                raise AcquisitionError(main_error[-1])
                            if not main_responses:
                                raise AcquisitionError("JS-браузер не получил документ источника.")
                            raw = page.content()
                            if len(raw.encode("utf-8")) > MAX_BYTES:
                                raise AcquisitionError("DOM JS-страницы превышает лимит размера.")
                            # Fixed read-only DOM operations; model-provided JS is never evaluated.
                            text = page.locator("body").inner_text(timeout=max(1, _remaining(deadline) * 1000))
                            headings = page.locator("h1")
                            primary_title = headings.first.inner_text(timeout=max(1, _remaining(deadline) * 1000)) if headings.count() else ""
                            seo_title = page.title()
                            final = self._scoped(main_responses[-1].url)
                            response = _Response(final, 200, raw.encode("utf-8"), {"content-type": "text/html; charset=utf-8"})
                            document = self._document(response, "browser", text=text, primary_title=primary_title, seo_title=seo_title)
                            document["network"] = counters
                            document["http_sha256"] = hashlib.sha256(main_responses[-1].body).hexdigest()
                            return document
                        finally:
                            context.close()
                    finally:
                        browser.close()
            except AcquisitionError:
                raise
            except Exception:
                if main_error:
                    raise AcquisitionError(main_error[-1]) from None
                raise AcquisitionError("JS-браузер не смог открыть страницу в пределах лимитов; проверьте установку Chromium и доступность источника.") from None
            finally:
                if watchdog:
                    watchdog.cancel()

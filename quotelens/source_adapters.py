"""Trusted server-side source adapters feeding the ordinary evidence pipeline.

An administrator may register ``register_source_adapter('catalog',
('catalog.example',), lambda: CatalogAdapter(server_credentials))`` at server
startup. No API endpoint, environment import, client config, or credential form
registers adapters. There are no bundled private-source/MIPT integrations.

An adapter implements ``discover(query, *, limit, timeout)`` -> list of
{url, title, snippet}, and ``capture(url, *, timeout)`` -> {url, html|text,
title?, primary_title?, retrieved_at?}. Capture must return source content safe
to retain in the job; request headers, credentials and transport responses must
stay on the adapter. Only the documented fields cross this boundary. Discovery
snippets never become product/price facts; captures go through normal extraction
and validate_candidate, with newly computed content hashes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import threading
from urllib.parse import urlsplit

from .acquisition import Acquisition, AcquisitionError, MAX_BYTES, _Response, _domain, _space


@dataclass(frozen=True)
class _Registration:
    source_id: str
    domains: tuple[str, ...]
    factory: object


_REGISTRY = {}
_LOCK = threading.Lock()
_MAX_ADAPTERS = 16


def register_source_adapter(source_id, domains, factory):
    """Register a bounded code-owned adapter; factory closes over server secrets."""
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", source_id) or not callable(factory):
        raise ValueError("Некорректная серверная регистрация источника.")
    if not isinstance(domains, (tuple, list)) or not 1 <= len(domains) <= 10:
        raise ValueError("Для адаптера нужны домены источника.")
    if any(not isinstance(domain, str) or not domain or len(domain) > 253 or re.search(r"[\s/:@?#\\]", domain) for domain in domains):
        raise ValueError("Для адаптера нужны домены без URL, путей и учётных данных.")
    normalized = tuple(dict.fromkeys(_domain(value) for value in domains))
    if any(not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}", domain) for domain in normalized):
        raise ValueError("Некорректный домен адаптера.")
    with _LOCK:
        if source_id in _REGISTRY or len(_REGISTRY) >= _MAX_ADAPTERS:
            raise ValueError("Источник уже зарегистрирован или достигнут лимит адаптеров.")
        if any(a == b or a.endswith('.' + b) or b.endswith('.' + a) for reg in _REGISTRY.values() for a in reg.domains for b in normalized):
            raise ValueError("Домены адаптеров не должны перекрываться.")
        _REGISTRY[source_id] = _Registration(source_id, normalized, factory)


def unregister_source_adapter(source_id):
    """Server lifecycle hook; existing research instances keep their snapshot."""
    with _LOCK:
        _REGISTRY.pop(source_id, None)


def _owns(registration, url):
    host = urlsplit(url).hostname or ""
    return any(host == domain or host.endswith('.' + domain) for domain in registration.domains)


class _AdapterAcquisition(Acquisition):
    def __init__(self, allowed_domains, timeout, registrations):
        super().__init__(allowed_domains, timeout)
        self._adapters = []
        for registration in registrations:
            try:
                adapter = registration.factory()
                if not callable(getattr(adapter, "discover", None)) or not callable(getattr(adapter, "capture", None)):
                    raise TypeError()
            except Exception:
                raise AcquisitionError("Не удалось подключить серверный адаптер источника.") from None
            self._adapters.append((registration, adapter))

    def _route(self, url):
        return next(((reg, adapter) for reg, adapter in self._adapters if _owns(reg, url)), None)

    def open(self, url, browser=False):
        url = self._scoped(url)
        routed = self._route(url)
        if routed is None:
            return super().open(url, browser=browser)
        registration, adapter = routed
        try:
            capture = adapter.capture(url, timeout=self.timeout)
            if not isinstance(capture, dict):
                raise ValueError()
            final_url = self._scoped(capture.get("url", url))
            if not _owns(registration, final_url):
                raise ValueError()
            is_html = isinstance(capture.get("html"), str)
            content = capture.get("html") if is_html else capture.get("text")
            if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > MAX_BYTES:
                raise ValueError()
            text = capture.get("text")
            if text is not None and (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES):
                raise ValueError()
            if any(capture.get(key) is not None and not isinstance(capture[key], str) for key in ("title", "primary_title")):
                raise ValueError()
            # Reparse raw content. Do not copy source hashes, evidence, schema
            # fields, HTTP headers, supplier facts, or caller-selected status.
            response = _Response(final_url, 200, content.encode("utf-8"),
                                 {"content-type": "text/html; charset=utf-8" if is_html else "text/plain; charset=utf-8"})
            document = self._document(response, "source_adapter", text=text if not is_html else None,
                primary_title=_space(capture.get("primary_title") or capture.get("title") or "")[:500] or None)
            retrieved = capture.get("retrieved_at")
            if retrieved is not None:
                if not isinstance(retrieved, str) or len(retrieved) > 40 or datetime.fromisoformat(retrieved.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError()
                document["retrieved_at"] = retrieved
            document["adapter_id"] = registration.source_id
            return document
        except Exception:
            # Adapter exceptions can include private endpoints/auth headers.
            raise AcquisitionError("Серверный адаптер не вернул допустимый снимок источника.") from None

    def search(self, query, limit=6):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise AcquisitionError("Поисковый запрос должен содержать от 1 до 500 символов.")
        query, limit = _space(query), max(1, min(10, int(limit)))
        hits, seen, unavailable = [], set(), False
        for registration, adapter in self._adapters:
            try:
                found = adapter.discover(query, limit=limit, timeout=self.timeout)
                if not isinstance(found, (tuple, list)) or len(found) > 100:
                    raise ValueError()
                for item in found:
                    if not isinstance(item, dict):
                        continue
                    if any(item.get(key) is not None and not isinstance(item[key], str) for key in ("title", "snippet")):
                        continue
                    try:
                        url = self._scoped(item.get("url"))
                    except AcquisitionError:
                        continue
                    if not _owns(registration, url) or url in seen:
                        continue
                    hits.append({"url": url, "title": _space(item.get("title") or "")[:300], "snippet": _space(item.get("snippet") or "")[:1200]})
                    seen.add(url)
                    if len(hits) >= limit:
                        break
            except Exception:
                unavailable = True
            if len(hits) >= limit:
                break
        # A scope entirely served by registered adapters does not send its
        # searches to a public engine. Unregistered web paths retain all
        # original public transport/DNS/robots/browser policy checks.
        public_domains = None if self.allowed_domains is None else [domain for domain in self.allowed_domains if not self._route("https://" + domain + "/")]
        public = None
        if len(hits) < limit and public_domains != []:
            public = Acquisition(public_domains, self.timeout).search(query, limit=limit - len(hits))
            for hit in public.get("hits", []):
                if hit["url"] not in seen:
                    hits.append(hit)
                    seen.add(hit["url"])
        if public is not None and not hits:
            return public
        return {"query": query, "hits": hits[:limit], "document": public.get("document") if public else None,
                "engine": "trusted-source-adapters", "status": "ok" if hits else "unavailable" if unavailable else "empty",
                "reason": "Серверный источник недоступен." if unavailable and not hits else None}


def build_acquisition(allowed_domains=None, timeout=18):
    """Build the safe default, adding only explicitly registered server code."""
    scope = None if allowed_domains is None else {_domain(domain) for domain in allowed_domains}
    with _LOCK:
        registrations = tuple(reg for reg in _REGISTRY.values() if scope is None or any(
            a == b or a.endswith('.' + b) or b.endswith('.' + a) for a in scope for b in reg.domains))
    return _AdapterAcquisition(scope, timeout, registrations) if registrations else Acquisition(scope, timeout)

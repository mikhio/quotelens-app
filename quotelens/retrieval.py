"""Bounded public-source search. A search result is a candidate, never evidence."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser

from .providers import ModelProvider, ProviderError


DEFAULT_LIMITS = {"min_suppliers": 3, "max_pages": 20, "max_queries": 6, "max_seconds": 90, "max_model_calls": 8}
SOURCES = [
    {"id": "prist", "name": "ПриСТ", "domain": "prist.ru", "url": "https://prist.ru/", "kind": "catalog", "search": "https://prist.ru/search/index.php?q={q}", "description": "Каталог измерительных приборов; цены и параметры проверяются на карточке."},
    {"id": "aktakom", "name": "АКТАКОМ", "domain": "aktakom.ru", "url": "https://www.aktakom.ru/", "kind": "catalog", "search": "https://www.aktakom.ru/search/index.php?q={q}", "description": "Приборы и технические характеристики производителя."},
    {"id": "souz", "name": "Союз-Прибор", "domain": "souz-pribor.ru", "url": "https://www.souz-pribor.ru/", "kind": "catalog", "search": "https://www.souz-pribor.ru/search/?q={q}", "description": "Каталог поставщика. Блокировки источника фиксируются отдельно."},
    {"id": "etalon", "name": "Эталонприбор", "domain": "etalonpribor.ru", "url": "https://www.etalonpribor.ru/", "kind": "catalog", "search": "https://www.etalonpribor.ru/search/?q={q}", "description": "Каталог поставщика; предложения требуют проверки комплектации."},
    {"id": "eliz", "name": "Элиз", "domain": "elizpribor.ru", "url": "https://elizpribor.ru/", "kind": "supplier", "description": "Проверяются конкретные карточки приборов; общий поиск каталога пока не подключён."},
    {"id": "eis", "name": "ЕИС Закупки", "domain": "zakupki.gov.ru", "url": "https://zakupki.gov.ru/", "kind": "reference", "description": "Реестр закупок. Специализированный API-коннектор не подключён."},
    {"id": "gisp", "name": "ГИСП", "domain": "gisp.gov.ru", "url": "https://gisp.gov.ru/goods/", "kind": "reference", "description": "Поиск отечественных аналогов. Наличие карточки не подтверждает цену; коннектор не реализован."},
]
_ROBOTS = {}
_ROBOTS_LOCK = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_domain(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


def compact(value):
    return re.sub(r"[^\w]", "", str(value).lower().replace("ё", "е"))


def allowed_domains():
    defaults = [s["domain"] for s in SOURCES] + ["elizpribor.ru", "eliz.ru", "novapribor.ru", "electronpribor.ru", "komus.ru"]
    return set(defaults + [x.strip().lower().removeprefix("www.") for x in os.getenv("SOURCE_ALLOWED_DOMAINS", "").split(",") if x.strip()])


class SourceError(RuntimeError):
    pass


def validate_public_url(url, *, require_allowed=True):
    if not isinstance(url, str) or len(url) > 2048:
        raise SourceError("Некорректная ссылка источника.")
    u = urllib.parse.urlsplit(url)
    try:
        port = u.port
    except ValueError:
        raise SourceError("Некорректный порт источника.") from None
    if u.scheme != "https" or not u.hostname or u.username or u.password or port not in {None, 443}:
        raise SourceError("Источник должен быть публичной HTTPS-страницей без учётных данных.")
    host = u.hostname.lower()
    if require_allowed and host.removeprefix("www.") not in allowed_domains():
        raise SourceError("Домен пока не подключён: добавьте разрешённый источник в настройках сервера.")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise SourceError("Не удалось определить адрес источника.") from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise SourceError("Локальные и служебные адреса не используются как источники.")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = validate_public_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, target)


def _raw_fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; QuoteLensResearch/0.1)", "Accept": "text/html,application/json;q=0.9"})
    try:
        with urllib.request.build_opener(SafeRedirect()).open(req, timeout=max(.1, min(timeout, 10))) as r:
            mime = r.headers.get_content_type()
            if mime not in {"text/html", "text/plain", "application/json", "application/xhtml+xml"}:
                raise SourceError("Формат источника пока не поддерживается.")
            data = r.read(1_500_001)
            if len(data) > 1_500_000:
                raise SourceError("Страница превышает допустимый размер.")
            charset = r.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace"), r.geturl()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"Источник вернул HTTP {exc.code}.") from None
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        raise SourceError("Источник не ответил за отведённое время.") from None


def robots_allowed(url, timeout):
    u = urllib.parse.urlsplit(url)
    origin = f"{u.scheme}://{u.netloc}"
    with _ROBOTS_LOCK:
        old = _ROBOTS.get(origin)
    if old and time.monotonic() - old[0] < 3600:
        rp = old[1]
        return rp is None or rp.can_fetch("QuoteLensResearch", url)
    try:
        text, _ = _raw_fetch(origin + "/robots.txt", min(timeout, 3))
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
    except SourceError as exc:
        if "HTTP 404" in str(exc):
            rp = None
        elif "HTTP 403" in str(exc) or "HTTP 401" in str(exc):
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(["User-agent: *", "Disallow: /"])
        else:
            raise SourceError("Не удалось проверить правила доступа к источнику.") from None
    with _ROBOTS_LOCK:
        _ROBOTS[origin] = (time.monotonic(), rp)
    return rp is None or rp.can_fetch("QuoteLensResearch", url)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []
        self.heading = []
        self.in_h1 = False
        self.title_parts = []
        self.in_title = False
        self.links = []
        self.link = None
        self.json_scripts = []
        self.json_active = False
        self.script = []
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.skip += 1
            self.json_active = a.get("type", "").lower() == "application/ld+json"
            self.script = []
        elif tag in {"style", "noscript", "svg"}:
            self.skip += 1
        if tag == "h1":
            self.in_h1 = True
        if tag == "title":
            self.in_title = True
        if tag == "a" and a.get("href"):
            self.link = [a["href"], []]
        if tag == "meta" and a.get("content"):
            self.meta[a.get("itemprop") or a.get("property") or a.get("name")] = a["content"]
        if tag in {"p", "div", "tr", "td", "th", "br", "h1", "h2", "li", "dt", "dd"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "script":
            if self.json_active:
                try:
                    self.json_scripts.append(json.loads("".join(self.script)))
                except (ValueError, TypeError):
                    pass
            self.json_active = False
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag == "h1":
            self.in_h1 = False
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.link:
            self.links.append((self.link[0], " ".join(self.link[1])))
            self.link = None

    def handle_data(self, data):
        if self.json_active:
            self.script.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.skip:
            return
        if self.in_h1:
            self.heading.append(data)
        self.parts.append(data)
        if self.link:
            self.link[1].append(data)

    def document(self, url):
        text = re.sub(r"\s+", " ", " ".join(self.parts)).strip()
        title = re.sub(r"\s+", " ", " ".join(self.heading or self.title_parts)).strip()
        return {"url": url, "title": title, "text": text, "links": self.links, "jsonld": self.json_scripts, "meta": self.meta, "retrieved_at": utcnow()}


def fetch_page(url, timeout):
    url = validate_public_url(url)
    began = time.monotonic()
    if not robots_allowed(url, timeout):
        raise SourceError("Правила источника не разрешают автоматическое чтение этой страницы.")
    remaining = timeout - (time.monotonic() - began)
    if remaining <= 0:
        raise SourceError("Истёк бюджет времени получения страницы.")
    raw, final = _raw_fetch(url, remaining)
    parser = PageParser()
    parser.feed(raw)
    doc = parser.document(final)
    if len(doc["text"]) < 120 or (re.search(r"captcha|access denied|доступ ограничен|проверка браузера", doc["title"], re.I)):
        raise SourceError("Источник вернул проверку доступа или недостаточно читаемых данных.")
    doc["sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return doc


def number(value):
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    if s.count(".") > 1 or s.count(",") > 0:
        return None
    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def quoted_currency(quote):
    found = set()
    if re.search(r"₽|руб(?:\.|лей|ля)?|\bRUB\b|\d\s*Р\b", str(quote), re.I):
        found.add("RUB")
    if re.search(r"\bUSD\b|доллар|\$", str(quote), re.I):
        found.add("USD")
    if re.search(r"\bEUR\b|евро|€", str(quote), re.I):
        found.add("EUR")
    return next(iter(found)) if len(found) == 1 else None


def extract_unit(focus, price_match, request):
    """Never derive the source sale unit or pack coefficient from the request."""
    context = focus[max(0, price_match.start() - 130):price_match.end() + 100]
    units = r"(коробк[а-я]*|пачк[а-я]*|упаковк[а-я]*|комплект[а-я]*|штук[а-я]*|шт\.?|лист[а-я]*|м[²2]|услуг[а-я]*|час[а-я]*)"
    unit_pattern = r"за\s+" + units
    match = re.search(unit_pattern, context, re.I)
    if not match:
        match = re.search(r"(?:₽|руб\.?)\s*/\s*" + units, context, re.I)
    if not match:
        match = re.search(r"Единиц[аы]\s+(?:измерения|цен[ыа])\s*[:—-]?\s*(коробк[а-я]*|пачк[а-я]*|упаковк[а-я]*|комплект[а-я]*|штук[а-я]*|шт\.?|лист[а-я]*|м[²2]|услуг[а-я]*|час[а-я]*)", focus, re.I)
    if not match:
        return None, None, {}
    raw = match.group(1).lower()
    unit = next((label for prefix, label in [("короб", "коробка"), ("пач", "пачка"), ("упаков", "упаковка"), ("комплект", "комплект"), ("шт", "шт"), ("штук", "шт"), ("лист", "лист"), ("услуг", "услуга"), ("час", "час"), ("м", "м²")] if raw.startswith(prefix)), None)
    evidence = {"unit": {"quote": match.group(0).strip()}}
    pack_size = 1 if unit in {"шт", "лист", "м²", "услуга", "час", "пачка"} else None
    target = request.get("unit")
    if unit in {"коробка", "упаковка"}:
        target_pattern = {"пачка": r"пач(?:ек|ки|ка)", "шт": r"(?:шт\.?|штук[а-я]*)", "лист": r"лист[а-я]*"}.get(target)
        if target_pattern:
            coefficient = re.search(r"(?:коробк[а-я]*|упаковк[а-я]*)[^\d]{0,25}(\d+)\s*" + target_pattern, context, re.I)
            if not coefficient:
                coefficient = re.search(r"(?:в\s+(?:одной\s+)?(?:коробке|упаковке))[^\d]{0,15}(\d+)\s*" + target_pattern, focus, re.I)
            if coefficient:
                pack_size = int(coefficient.group(1))
                evidence["pack_size"] = {"quote": coefficient.group(0).strip()}
    return unit, pack_size, evidence


def extract_offer(page, request):
    """Conservative extraction: one H1 product, a quoted price, quoted attributes."""
    text, title, url = page["text"], page["title"], page["url"]
    if not title or len(title) > 240 or re.search(r"поиск|результаты|каталог|выбрать и заказать", title, re.I):
        return None
    start = text.find(title)
    focus = text[max(0, start):max(0, start) + 17000] if start >= 0 else text[:17000]
    # Explicitly avoid interpreting a category's first recommended item's price.
    cut = re.search(r"Рекомендуем(?:ые| также)|Вас также может|Похожие товары|С этим товаром покупают", focus, re.I)
    if cut:
        focus = focus[:cut.start()]
    ev = {"title": {"quote": title, "url": url}}
    price = None
    match = re.search(r"(?:Розничная\s+)?Цена[^\d]{0,55}(\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*(?:₽|Р\b|руб)", focus, re.I)
    if not match:
        match = re.search(r"(?<![\d\w])(\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*(?:₽|руб\.)", focus)
    if match:
        price = number(match.group(1))
        ev["price"] = {"quote": match.group(0).strip(), "url": url}
    if not price:
        return None
    tax = "unknown"
    tax_context = focus[max(0, match.start() - 35):match.end() + 65] if match else ""
    if match and re.search(r"без\s*НДС", tax_context, re.I):
        tax = "excluded"
        ev["tax"] = {"quote": tax_context.strip(), "url": url}
    elif match and re.search(r"(?:с\s*НДС|вкл\.?\s*НДС)", tax_context, re.I):
        tax = "included"
        ev["tax"] = {"quote": tax_context.strip(), "url": url}
    attrs = {}
    model_match = re.search(r"(?:АКИП|AKIP|ADS|АDS|DS|SDS)[\s-]*\d{3,5}(?:\s*[/.-]\s*[\w]+)?", title, re.I)
    if model_match:
        attrs["model"] = model_match.group(0).strip()
        ev["attributes.model"] = {"quote": model_match.group(0).strip(), "url": url}
    patterns = {
        "channels": r"(?:Число|Количество)\s+каналов\s*:?[\s—-]*(\d+)",
        "bandwidth_mhz": r"Полоса\s+пропускания[^\d]{0,30}(\d+(?:[,.]\d+)?)\s*(МГц|MHz|ГГц|GHz)",
        "density_gsm": r"(?:плотност[ьи]|Плотность бумаги)[^\d]{0,20}(\d+)\s*(?:г/м|г\/кв)",
        "sheets_per_pack": r"(?:Количество\s+листов[^\d]{0,15}|)(\d{2,4})\s*лист(?:ов|а)?",
        "format": r"(?:Формат[^\w]{0,8})([AaАа][345])",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, focus, re.I)
        if m:
            val = m.group(1).upper().replace("А", "A") if key == "format" else number(m.group(1))
            if key == "bandwidth_mhz" and m.lastindex >= 2 and m.group(2).lower() in {"ггц", "ghz"}:
                val *= 1000
            attrs[key] = val
            ev["attributes." + key] = {"quote": m.group(0).strip(), "url": url}
    domain = canonical_domain(url)
    source = next((s for s in SOURCES if s["domain"] == domain), None)
    supplier = source["name"] if source else domain
    unit, pack_size, unit_evidence = extract_unit(focus, match, request)
    ev.update({key: {**value, "url": url} for key, value in unit_evidence.items()})
    currency = quoted_currency(match.group(0))
    if currency:
        ev["currency"] = {"quote": match.group(0).strip(), "url": url}
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:12], "title": title, "supplier": supplier, "domain": domain, "url": url,
            "retrieved_at": page["retrieved_at"], "price": price, "unit": unit, "pack_size": pack_size,
            "currency": currency, "tax": tax, "delivery": None, "attributes": attrs, "evidence": ev,
            "provenance": "live", "source_hash": page["sha256"], "source_text": focus[:22000]}


def validate_model_fields(candidate, page, request):
    """Reject unsupported fields even when they came from the configured model."""
    if not isinstance(candidate, dict):
        return None
    evidence = candidate.get("evidence", {})
    title = candidate.get("title")
    if not isinstance(title, str) or not title or title not in page["text"]:
        return None
    fields = {}
    proven = {}
    for field, item in evidence.items():
        if not isinstance(item, dict):
            continue
        quote = item.get("quote", "")
        if not isinstance(quote, str) or len(quote) < 3 or len(quote) > 800 or quote not in page["text"]:
            continue
        value = candidate.get("attributes", {}).get(field[11:]) if field.startswith("attributes.") else candidate.get(field)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            vals = [number(x) for x in re.findall(r"\d[\d\s\u00a0]*(?:[,.]\d{1,2})?", quote)]
            if not any(x is not None and abs(x - value) < .001 for x in vals):
                continue
        elif field == "currency":
            if value not in {"RUB", "USD", "EUR"} or quoted_currency(quote) != value:
                continue
        elif field == "tax":
            if value == "included" and not re.search(r"(?:с\s*НДС|вкл\.?\s*НДС)", quote, re.I):
                continue
            if value == "excluded" and not re.search(r"без\s*НДС", quote, re.I):
                continue
        elif compact(value) not in compact(quote):
            continue
        fields[field] = value
        proven[field] = {"quote": quote, "url": page["url"]}
    price = fields.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    url = page["url"]
    currency = fields.get("currency") or quoted_currency(proven.get("price", {}).get("quote", ""))
    if currency and "currency" not in proven:
        proven["currency"] = dict(proven["price"])
    domain = canonical_domain(url)
    source = next((s for s in SOURCES if s["domain"] == domain), None)
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:12], "title": title, "supplier": source["name"] if source else domain,
            "domain": domain, "url": url, "retrieved_at": page["retrieved_at"], "price": price,
            "unit": fields.get("unit"), "pack_size": fields.get("pack_size"), "currency": currency, "tax": fields.get("tax", "unknown"),
            "delivery": None, "attributes": {k[11:]: v for k, v in fields.items() if k.startswith("attributes.")},
            "evidence": proven, "provenance": "live", "source_hash": page["sha256"], "source_text": page["text"][:22000]}


def web_search(query, timeout):
    """Optional JSON search service (e.g. an operator-provided SearXNG instance).

    Only discovery metadata is accepted here. Every offer must be fetched again.
    """
    base = os.getenv("SEARCH_API_URL", "")
    if not base:
        return []
    u = urllib.parse.urlsplit(base)
    if u.username or u.password or u.query or u.fragment or not u.hostname:
        raise SourceError("Проверьте адрес поискового сервиса на сервере.")
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "host.docker.internal"}):
        raise SourceError("Для внешнего поискового сервиса требуется HTTPS.")
    endpoint = base + "?" + urllib.parse.urlencode({"q": query, "format": "json"})
    headers = {"Accept": "application/json", "User-Agent": "QuoteLens/0.1"}
    key = os.getenv("SEARCH_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    class DenyRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise SourceError("Поисковый сервис перенаправил запрос; проверьте его адрес.")
    try:
        req = urllib.request.Request(endpoint, headers=headers)
        with urllib.request.build_opener(DenyRedirect()).open(req, timeout=max(.1, min(timeout, 10))) as response:
            raw = response.read(800001)
            if len(raw) > 800000:
                raise SourceError("Слишком большой ответ поискового сервиса.")
            result = json.loads(raw)
        candidates = result.get("results", [])
        return [x for x in candidates if isinstance(x, dict) and isinstance(x.get("url"), str)][:10]
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        raise SourceError(f"Поисковый сервис вернул HTTP {exc.code}.") from None
    except Exception:
        raise SourceError("Поисковый сервис недоступен или вернул неожиданный формат.") from None


class SearchBudget:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.pages = self.queries = self.calls = 0

    @property
    def remaining(self):
        return max(0, self.limits["max_seconds"] - (time.monotonic() - self.started))

    def stop(self):
        if self.remaining <= 0:
            return "time_budget"
        if self.pages >= self.limits["max_pages"]:
            return "page_budget"
        if self.limits["max_model_calls"] > 0 and self.calls >= self.limits["max_model_calls"]:
            return "model_budget"
        return None

    def public(self):
        return {"pages_used": self.pages, "max_pages": self.limits["max_pages"], "queries_used": self.queries,
                "max_queries": self.limits["max_queries"], "model_calls": self.calls, "max_model_calls": self.limits["max_model_calls"],
                "elapsed_seconds": round(time.monotonic() - self.started, 2), "max_seconds": self.limits["max_seconds"]}


STOP_LABELS = {"time_budget": "Достигнут лимит времени", "page_budget": "Достигнут лимит страниц", "query_budget": "Достигнут лимит поисковых запросов",
               "model_budget": "Достигнут лимит обращений к модели", "sufficient_evidence": "Достаточно данных в рамках выбранного профиля",
               "sources_exhausted": "Подключённые источники проверены", "cancelled": "Поиск остановлен пользователем"}


def run_search(request, limits, source_urls, cancel, event, source_ids=None):
    from .engine import analyze_offers
    from .scenarios import list_scenarios, load_scenario
    budget = SearchBudget(limits)
    selected_domains = {s["domain"] for s in SOURCES if s["id"] in source_ids} if source_ids is not None else None
    model = ModelProvider()
    offers, statuses, seen = [], [], set()
    began = utcnow()
    event("Извлечены существенные требования. Ищу в подключённых каталогах.", "info")
    queries = list(dict.fromkeys([request.get("product", ""), *request.get("search_terms", []), request.get("description", "")]))
    queries = [q.strip()[:220] for q in queries if q and len(q.strip()) > 2]
    if model.configured and budget.calls < limits["max_model_calls"] and budget.remaining > 1:
        budget.calls += 1
        try:
            queries = list(dict.fromkeys(model.queries(request, [], timeout=budget.remaining) + queries))
            event("Модель сформировала поисковые запросы; лимиты задаёт приложение.", "info")
        except ProviderError as exc:
            event(str(exc) + " Продолжаю поиск по каталогам.", "warning")
    else:
        event("В этом прогоне модель не используется. Доступны поиск подключённых каталогов и проверяемые правила извлечения.", "warning")

    candidates = list(source_urls)
    # A registered sample supplies discovery URLs, never prices for a live run.
    for meta in list_scenarios():
        if meta.get("kind") == "real_snapshot" and meta.get("category") == request.get("category"):
            sample = load_scenario(meta["id"])
            candidates.extend(o["url"] for o in sample.get("offers", []) if o.get("url") and (selected_domains is None or canonical_domain(o["url"]) in selected_domains))
    candidates = list(dict.fromkeys(candidates))

    def read_candidate(url):
        if cancel.is_set() or budget.stop() or url in seen:
            return
        if selected_domains is not None and canonical_domain(url) not in selected_domains:
            return
        seen.add(url)
        budget.pages += 1
        domain = canonical_domain(url)
        event(f"Проверяю предложение: {domain}", "info")
        try:
            page = fetch_page(url, min(10, budget.remaining))
            offer = extract_offer(page, request)
            if model.configured and budget.calls < limits["max_model_calls"] and budget.remaining > 1:
                budget.calls += 1
                try:
                    extracted = validate_model_fields(model.extract(request, page, timeout=budget.remaining), page, request)
                    if extracted:
                        if offer:
                            for key, value in extracted["attributes"].items():
                                if key not in offer["attributes"]:
                                    offer["attributes"][key] = value
                                    offer["evidence"]["attributes." + key] = extracted["evidence"]["attributes." + key]
                        else:
                            offer = extracted
                except ProviderError as exc:
                    event(str(exc), "warning")
            if offer:
                offers.append(offer)
                statuses.append({"name": offer["supplier"], "domain": domain, "status": "ok", "message": "Цена и характеристики получены с карточки; сохранены подтверждения."})
                event(f"Получено предложение: {offer['title'][:100]}", "success")
            else:
                statuses.append({"name": domain, "domain": domain, "status": "no_data", "message": "Одна подтверждённая цена предложения на странице не установлена."})
        except SourceError as exc:
            statuses.append({"name": domain, "domain": domain, "status": "unavailable", "message": str(exc)})
            event(f"{domain}: {exc}", "warning")
        except Exception:
            statuses.append({"name": domain, "domain": domain, "status": "error", "message": "Не удалось разобрать страницу; значение не использовано."})

    for url in candidates:
        read_candidate(url)
        if budget.stop() or cancel.is_set():
            break
    stale_rounds = 0
    processed_sources = set()
    replanned = False
    for query in queries:
        if budget.stop() or cancel.is_set():
            break
        if os.getenv("SEARCH_API_URL") and budget.queries < limits["max_queries"]:
            budget.queries += 1
            event(f"Ищу дополнительные источники в веб-поиске: {query[:100]}", "info")
            try:
                for candidate in web_search(query, budget.remaining):
                    domain = canonical_domain(candidate["url"])
                    if domain not in allowed_domains():
                        statuses.append({"name": domain, "domain": domain, "status": "candidate", "message": "Найден новый источник. Для получения данных требуется подключить домен и проверить адаптер."})
                        continue
                    read_candidate(candidate["url"])
                    if budget.stop() or cancel.is_set():
                        break
            except SourceError as exc:
                event(str(exc), "warning")
        for source in [s for s in SOURCES if s["kind"] == "catalog" and (selected_domains is None or s["domain"] in selected_domains)]:
            if budget.queries >= limits["max_queries"] or budget.stop() or cancel.is_set():
                break
            budget.queries += 1
            budget.pages += 1
            processed_sources.add(source["domain"])
            event(f"Запрос в {source['name']}: {query[:100]}", "info")
            before = len(offers)
            try:
                doc = fetch_page(source["search"].format(q=urllib.parse.quote(query)), min(8, budget.remaining))
                query_tokens = [compact(x) for x in re.findall(r"[\w/.-]+", query) if len(x) > 3]
                links = []
                for href, label in doc["links"]:
                    full = urllib.parse.urljoin(doc["url"], href)
                    if canonical_domain(full) != source["domain"] or any(x in full for x in ["/search", "login", "?action", "mailto:", "#"]):
                        continue
                    text = compact(label)
                    if not label or not any(tok in text for tok in query_tokens):
                        continue
                    if any(x in full for x in ["/catalog/", "/product/", "ELEMENT_ID="]):
                        links.append(full)
                for link in list(dict.fromkeys(links))[:4]:
                    read_candidate(link)
                    if budget.stop() or cancel.is_set():
                        break
                statuses.append({"name": source["name"], "domain": source["domain"], "status": "searched", "message": f"Каталог проверен, найдено новых предложений: {len(offers) - before}."})
            except SourceError as exc:
                statuses.append({"name": source["name"], "domain": source["domain"], "status": "unavailable", "message": str(exc)})
            stale_rounds = stale_rounds + 1 if len(offers) == before else 0
        current = analyze_offers(request, offers, {"min_suppliers": limits["min_suppliers"]})
        if current["calculation"]["status"] == "ready" and len(processed_sources) >= 2 and stale_rounds >= 2:
            code = "sufficient_evidence"
            break
        if not replanned and model.configured and current["calculation"]["status"] != "ready" and not budget.stop() and budget.queries < limits["max_queries"] and budget.calls < limits["max_model_calls"] and budget.remaining > 1:
            replanned = True
            budget.calls += 1
            try:
                observed = [{"title": o.get("title"), "status": o.get("status"), "reasons": o.get("reasons")} for o in current["offers"]]
                for q in model.queries(request, observed, timeout=budget.remaining):
                    if q not in queries:
                        queries.append(q)
                event("Модель уточнила план по пробелам найденных предложений.", "info")
            except ProviderError as exc:
                event(str(exc), "warning")
    else:
        code = "sources_exhausted"
    if cancel.is_set():
        code = "cancelled"
    elif budget.stop():
        code = budget.stop()
    elif 'code' not in locals() or code != "sufficient_evidence":
        code = "query_budget" if budget.queries >= limits["max_queries"] else "sources_exhausted"
    result = analyze_offers(request, offers, {"min_suppliers": limits["min_suppliers"]})
    result.setdefault("warnings", []).append("Проверена ограниченная выборка подключённых источников. Полнота всего рынка и юридическая независимость продавцов не установлены.")
    result["search"] = {"mode": "live", "started_at": began, "finished_at": utcnow(), "stop": {"code": code, "label": STOP_LABELS[code],
                         "detail": "Остановка относится к заданному бюджету и доступным источникам; это не утверждение, что весь интернет проверен."},
                        "budget": budget.public(), "source_statuses": statuses, "completeness": "bounded_not_exhaustive", "model_used": budget.calls > 0}
    return result

"""Source navigation and search scope. Grouping never changes price eligibility."""
from __future__ import annotations

import copy
import ipaddress
import re
from urllib.parse import urlsplit

from .retrieval import SOURCES

MAX_SCOPE_DOMAINS = 32

GROUPS = {
    "public_records": "Реестры договоров",
    "industry": "Промышленные каталоги",
    "manufacturers": "Сайты брендов и производителей",
    "retail": "Магазины и поставщики",
    "platforms": "Маркетплейсы и ЭТП",
    "other": "Другие открытые сайты",
}
_KNOWN = {
    "zakupki.gov.ru": "public_records", "gisp.gov.ru": "industry",
    "aktakom.ru": "manufacturers", "rigol.com": "manufacturers",
    "tektronix.com": "manufacturers", "keysight.com": "manufacturers",
    "ozon.ru": "platforms", "market.yandex.ru": "platforms",
    "wildberries.ru": "platforms", "b2b-center.ru": "platforms",
    "sberbank-ast.ru": "platforms", "rts-tender.ru": "platforms",
    "roseltorg.ru": "platforms", "etpgpb.ru": "platforms",
    "etp.zakazrf.ru": "platforms", "tektorg.ru": "platforms",
    "fabrikant.ru": "platforms", "lot-online.ru": "platforms",
    "agregatoreat.ru": "platforms", "zakupki.mos.ru": "platforms",
    "etm.ru": "retail", "chipdip.ru": "retail",
    "vseinstrumenti.ru": "retail", "komus.ru": "retail",
    "prist.ru": "retail", "souz-pribor.ru": "retail", "etalonpribor.ru": "retail",
    "elizpribor.ru": "retail", "eliz.ru": "retail", "novapribor.ru": "retail",
    "radioelementy.ru": "retail", "rigol-shop.ru": "retail",
    "terra-kip.ru": "retail", "tectron.ru": "retail",
}


def domain_of(value):
    value = str(value or "").strip().lower()
    try:
        host = urlsplit(value if "://" in value else "https://" + value).hostname or ""
        return host.removeprefix("www.").rstrip(".").encode("idna").decode()
    except (ValueError, UnicodeError):
        return ""


def normalize_domain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 253:
        raise ValueError("Укажите домены сайтов без пути и учётных данных.")
    value = value.strip().lower()
    if any(char in value for char in "/:@?#\\"):
        raise ValueError("Укажите домен сайта без протокола, пути и учётных данных.")
    domain = domain_of(value)
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}", domain):
        raise ValueError("Некорректный домен сайта.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("Укажите публичное доменное имя сайта, не IP-адрес.")


def normalize_scope(scope):
    if not isinstance(scope, dict) or set(scope) - {"mode", "domains"}:
        raise ValueError("Выберите весь интернет или конкретные сайты.")
    mode = scope.get("mode")
    domains = scope.get("domains", [])
    if mode not in {"web", "domains"} or not isinstance(domains, list):
        raise ValueError("Некорректная область поиска.")
    if mode == "web":
        if domains:
            raise ValueError("Для поиска во всём интернете отдельный список сайтов не нужен.")
        return {"mode": "web", "domains": []}
    if not 1 <= len(domains) <= MAX_SCOPE_DOMAINS:
        raise ValueError(f"Укажите от 1 до {MAX_SCOPE_DOMAINS} сайтов.")
    normalized = list(dict.fromkeys(normalize_domain(value) for value in domains))
    return {"mode": "domains", "domains": normalized}


def effective_scope(payload):
    if isinstance(payload.get("source_scope"), dict):
        return normalize_scope(payload["source_scope"])
    ids = payload.get("source_ids")
    if ids is None or "web" in ids:
        return {"mode": "web", "domains": []}
    domains = [source["domain"] for source in SOURCES if source["id"] in ids]
    return {"mode": "domains", "domains": domains}


def source_metadata(domain, *, text="", offer=None):
    host = domain_of(domain)
    group, basis = "other", "Тип сайта не установлен."
    for known, kind in _KNOWN.items():
        if host == known or host.endswith("." + known):
            group, basis = kind, "Тип сайта по его назначению; это не оценка надёжности цены."
            break
    if group == "other":
        body = str(text or "").casefold()
        commerce = sum(bool(re.search(pattern, body)) for pattern in
                       (r"корзин", r"купить|заказать товар", r"доставк", r"цен[аы]|₽"))
        evidence = offer.get("evidence", {}) if isinstance(offer, dict) else {}
        if commerce >= 2 or any(field in evidence for field in ("price", "unit")):
            group, basis = "retail", "Предположительный коммерческий сайт по содержимому страницы."
    return {"source_group": group, "source_group_label": GROUPS[group], "source_group_basis": basis}


def annotate_result(result):
    """Supply grouping to old saved results too, without changing their prices."""
    by_domain = {}
    for offer in result.get("offers", []):
        host = domain_of(offer.get("domain") or offer.get("url"))
        meta = source_metadata(host, offer=offer)
        if offer.get("source_group") in GROUPS:
            meta = {key: offer.get(key, value) for key, value in meta.items()}
        offer.update(meta)
        by_domain[host] = meta
    for page in result.get("discovery_pages", []):
        host = domain_of(page.get("url"))
        if page.get("source_group") not in GROUPS:
            page.update(source_metadata(host, text=page.get("title", "")))
        by_domain.setdefault(host, {key: page[key] for key in ("source_group", "source_group_label", "source_group_basis")})
    for source in result.get("search", {}).get("source_statuses", []):
        host = domain_of(source.get("domain") or source.get("url"))
        if host in by_domain:
            source.update(by_domain[host])
        elif source.get("source_group") not in GROUPS:
            source.update(source_metadata(host))
    return result


def request_metadata(payload):
    result = {key: copy.deepcopy(payload.get(key)) for key in ("description", "quantity", "source_ids")}
    if "source_scope" in payload:
        result["source_scope"] = copy.deepcopy(payload["source_scope"])
    for key in ("items", "input_documents", "search_policy"):
        if key in payload:
            result[key] = copy.deepcopy(payload[key])
    return result


def rebuild_saved_sources(store, job):
    """Recompute derived offers; captured documents and prior audit stay intact."""
    from .source_documents import classify_page
    from .selection import finalize
    old = job.get("result") or {}
    base = old.get("base_result")
    if not isinstance(base, dict) or not base.get("request"):
        return None
    kept, removed, pages = [], [], copy.deepcopy(old.get("discovery_pages", []))
    for offer in base.get("offers", []):
        document_id = offer.get("document_id")
        document = store.get_document(job["id"], document_id) if document_id else None
        classification = classify_page(document) if document else {"kind": "unknown"}
        if classification["kind"] != "listing":
            kept.append(copy.deepcopy(offer))
            continue
        removed.append(offer["id"])
        page = {"url": document["url"], "title": document.get("primary_title") or document.get("title"),
                "document_id": document_id, "reason": classification["reason"],
                "product_links": classification["product_links"][:12],
                "rejected_offer_id": offer["id"],
                **source_metadata(document["url"], text=document.get("text", ""), offer=offer)}
        if not any(item.get("document_id") == document_id for item in pages):
            pages.append(page)
    if not removed:
        return None
    result = finalize(base["request"], kept, base.get("policy"), job.get("excluded_ids", []))
    result["search"] = copy.deepcopy(old.get("search", {}))
    result["search"]["source_revalidation"] = {"removed_catalogs": len(removed),
                                               "retained_product_offers": len(kept)}
    result["discovery_pages"] = pages
    result["validation_notice"] = ("Страницы каталогов сохранены как источники поиска. "
        f"Карточек без отдельного товарного предложения снято: {len(removed)}; их цены не используются.")
    remaining_domains = {domain_of(offer.get("domain") or offer.get("url")) for offer in kept}
    discovery_domains = {domain_of(page.get("url")) for page in pages}
    for source in result["search"].get("source_statuses", []):
        host = domain_of(source.get("domain") or source.get("url"))
        if host in discovery_domains and host not in remaining_domains:
            source["message"] = "Найден каталог; отдельное предложение не подтверждено."
    annotate_result(result)
    return {"result": result, "removed_offer_ids": removed}

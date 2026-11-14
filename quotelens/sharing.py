"""Immutable, recipient-only justification snapshots. No owner app is embedded."""
from __future__ import annotations

from copy import deepcopy
from html import escape
import re
from urllib.parse import urlsplit


def _pick(value, keys):
    return {k: deepcopy(value[k]) for k in keys if k in value}


def _url(value):
    try:
        p = urlsplit(value or "")
        return str(value) if p.scheme == "https" and p.hostname and not p.username and not p.password else None
    except ValueError:
        return None


def _proof(value):
    if not isinstance(value, dict):
        return {}
    out = _pick(value, ("quote", "document_id", "source_hash", "retrieved_at", "basis", "locator", "attribute", "raw_value", "raw_label", "visibility", "tab_id", "tab_label", "source_unit", "normalized_unit", "unit_reason", "normalization", "inferred", "related_field"))
    if _url(value.get("url")):
        out["url"] = value["url"]
    return out


def _offer(value):
    out = _pick(value, ("id", "title", "supplier", "domain", "price", "currency", "unit", "pack_size", "tax", "delivery", "attributes", "conditions", "status", "normalized_price", "duplicate_of", "manual_excluded", "reasons", "source_issues", "functional_status", "requirement_checks", "condition_checks", "document_id", "source_hash", "retrieved_at", "provenance", "manual_confirmations", "manual_acceptances", "relevance"))
    if _url(value.get("url")):
        out["url"] = value["url"]
    out["evidence"] = {k: _proof(p) for k, p in value.get("evidence", {}).items() if isinstance(p, dict)}
    out["observed_terms"] = {kind: [{**_proof(p), "value": p.get("value")} for p in rows if isinstance(p, dict)] for kind, rows in (value.get("observed_terms") or {}).items() if isinstance(rows, list)}
    out["attribute_hints"] = {key: [{**_proof(p), "value": p.get("value"), "reason": p.get("reason"), "confirmed_for_product": False} for p in rows] for key, rows in value.get("attribute_hints", {}).items()}
    out["pricing_conditions"] = [_pick(p, ("quote", "context", "quantity", "unit", "applies", "url", "document_id", "source_hash", "retrieved_at")) for p in value.get("pricing_conditions", [])]
    details = value.get("supplier_details") or {}
    out["supplier_details"] = _pick(details, ("legal_name", "inn", "email", "phone", "website", "verification"))
    out["supplier_details"]["evidence"] = {k: _proof(p) for k, p in details.get("evidence", {}).items() if isinstance(p, dict)}
    return out


def _request(value):
    # Structured needs are relevant to the recipient; full input files and form
    # descriptions may contain unrelated information and are never shared.
    out = _pick(value, ("product", "title", "quantity", "unit", "category", "missing_critical_fields"))
    out["requirements"] = [_pick(r, ("key", "label", "value", "op", "unit", "manual_override")) for r in value.get("requirements", []) if isinstance(r, dict)]
    out["conditions"] = [_pick(r, ("key", "type", "label", "text", "value", "op")) if isinstance(r, dict) else str(r) for r in value.get("conditions", [])]
    return out


def sanitize_snapshot(result, documents=(), queries=()):
    def line(value):
        out = _pick(value, ("id", "title", "quantity", "unit", "calculation", "warnings", "target_group"))
        out["request"] = _request(value.get("request", {}))
        out["offers"] = [_offer(o) for o in value.get("offers", [])]
        return out
    out = line(result)
    out["items"] = [line(v) for v in result.get("items", [])]
    if out["items"]:
        out["request"]["items"] = [{"id": v.get("id"), "title": v.get("title"), "quantity": v.get("quantity"), "unit": v.get("unit")} for v in out["items"]]
    search = result.get("search") or {}
    out["search"] = _pick(search, ("started_at", "finished_at", "stop", "completeness", "source_scope", "coverage"))
    out["search"]["source_statuses"] = [_pick(s, ("name", "domain", "status", "message", "url", "source_group_label")) for s in search.get("source_statuses", []) if isinstance(s, dict)]
    out["search"]["search_policy"] = _pick(search.get("search_policy") or {}, ("mode", "max_seconds", "max_offers"))
    out["search"]["stop"] = _pick(search.get("stop") or {}, ("code", "label"))
    # A snapshot contains the checked source material, not model transcripts,
    # authentication headers, provider URLs, spending or application events.
    docs = []
    tab_quotes = {}
    for offer in out.get("offers", []):
        for proof in offer.get("evidence", {}).values():
            if proof.get("visibility") == "reachable_tab" and proof.get("document_id") and proof.get("quote"):
                tab_quotes.setdefault((proof["document_id"], proof.get("source_hash")), {}).setdefault(proof.get("tab_label") or "Характеристики", []).append(proof["quote"])
    for value in documents:
        if value.get("method") == "search_api" or not _url(value.get("url")):
            continue
        doc = _pick(value, ("document_id", "title", "text", "sha256", "retrieved_at", "method", "page_kind"))
        doc["url"] = value["url"]
        sections = tab_quotes.get((doc.get("document_id"), doc.get("sha256")), {})
        if sections:
            doc["structured_sections"] = [{"title": title, "text": "\n".join(dict.fromkeys(quotes))} for title, quotes in sections.items()]
        docs.append(doc)
    out["search"]["audit"] = {"queries": list(dict.fromkeys(str(q) for q in queries)),
        "checked_pages": [{k: v for k, v in d.items() if k != "text"} for d in docs],
        "conclusion": "Предложения не найдены среди проверенных источников. Полнота всего рынка не установлена."}
    out["review"] = _pick(result.get("review", {}), ("revision", "excluded_ids"))
    return out, docs


def reader_html(result, documents=(), *, revision=0, created_at=""):
    """A clean HTML artifact with native disclosure; works offline as saved."""
    esc = lambda v: escape(str(v if v is not None else "—"), quote=True)
    text = lambda v: "; ".join(map(str, v)) if isinstance(v, list) else str(v if v is not None else "—")
    def money(v):
        return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " ₽" if isinstance(v, (int, float)) and not isinstance(v, bool) else "Не рассчитано"
    from .reports import _unit_money
    def link(url, label):
        return f'<a href="{esc(url)}" rel="noreferrer noopener">{esc(label)}</a>' if _url(url) else esc(label)
    title = result.get("request", {}).get("title") or result.get("request", {}).get("product") or "Обоснование стоимости закупки"
    calc = result.get("calculation", {})
    provisional = calc.get("provisional") or {}
    ready = calc.get("status") == "ready" and calc.get("total") is not None
    amount = calc.get("total") if ready else provisional.get("total")
    partial = not ready and amount is None and provisional.get("known_subtotal") is not None
    if partial:
        amount = provisional["known_subtotal"]
    label = "По профилю расчёта" if ready else "Известная часть закупки; полная стоимость не определена" if partial else "Предварительный результат; полная стоимость требует проверки"
    body = [f'<header><p>Обоснование стоимости · ревизия {esc(revision)}</p><h1>{esc(title)}</h1><strong class="total">{money(amount)}</strong><p>{label}</p><small>Сохранено {esc(created_at)}</small></header>']
    lines = result.get("items") or [result]
    body.append('<nav aria-label="Позиции">' + "".join(f'<a href="#item-{i}">{esc(v.get("title") or v.get("request", {}).get("product") or "Позиция")}</a>' for i, v in enumerate(lines, 1)) + '</nav>')
    status_labels = {"included": "В расчёте", "excluded": "Отброшено", "uncertain": "Требует проверки"}
    for i, item in enumerate(lines, 1):
        req, group = item.get("request", {}), item.get("target_group", {})
        name = item.get("title") or req.get("product") or "Позиция"
        body.append(f'<section id="item-{i}"><h2>{i}. {esc(name)}</h2><p>{esc(item.get("quantity", req.get("quantity")))} {esc(item.get("unit", req.get("unit")))} · {group.get("count", 0)} из 3 целевых предложений</p>')
        if group:
            cv = f'{group["cv"]:.2f}%' if isinstance(group.get("cv"), (int, float)) else "не определён"
            body.append(f'<p>Разброс цен (коэффициент вариации, CV): {cv} · порог профиля {esc(group.get("cv_limit", 33))}%</p><p>{esc(group.get("reason", ""))}</p>')
        rows = "".join(f'<tr><th>{esc(r.get("label") or r.get("key"))}</th><td>{esc(r.get("op", "eq"))} {esc(text(r.get("value")))} {esc(r.get("unit") or "")}</td></tr>' for r in req.get("requirements", []))
        if rows:
            body.append('<details><summary>Требования ТЗ</summary><table>' + rows + '</table></details>')
        if req.get("conditions"):
            body.append('<details><summary>Условия поставки</summary><ul>' + "".join('<li>' + esc(c.get("text") or c.get("value") or c.get("label") or "Условие" if isinstance(c, dict) else c) + '</li>' for c in req["conditions"]) + '</ul></details>')
        for offer in item.get("offers", []):
            details = offer.get("supplier_details") or {}
            unit_price = esc(_unit_money(offer.get("price"), offer.get("currency") or "валюта не подтверждена"))
            body.append(f'<article><div class="badge {esc(offer.get("status"))}">{esc(status_labels.get(offer.get("status"), "Требует проверки"))}</div><h3>{esc(offer.get("title"))}</h3><p>{link(offer.get("url"), offer.get("supplier") or offer.get("domain") or "Источник")}</p><strong>{unit_price}</strong><span> / {esc(offer.get("unit"))}</span><p>Сохранено {esc(offer.get("retrieved_at"))}</p>')
            attrs = offer.get("attributes") or {}
            public = [("Производитель", attrs.get("manufacturer") or attrs.get("producer")),
                      ("Организация", details.get("legal_name")), ("ИНН", details.get("inn")),
                      ("Email", details.get("email")), ("Телефон", details.get("phone"))]
            body.append('<details><summary>Поставщик и производитель</summary><dl>' + ''.join('<dt>' + esc(k) + '</dt><dd>' + esc(v or "Нет подтверждённых сведений") + '</dd>' for k, v in public) + '</dl>')
            if attrs.get("brand"):
                body.append('<p>Бренд: ' + esc(attrs["brand"]) + '</p>')
            if details.get("website"):
                body.append('<p>' + link(details["website"], "Сайт поставщика") + '</p>')
            if details.get("inn"):
                body.append('<p>' + link((details.get("verification") or {}).get("registry_url"), "Проверить в реестре ФНС") + ' · Проверка организации в реестре не выполнена.</p>')
            body.append('</details>')
            if offer.get("reasons"):
                body.append('<details><summary>Решение по предложению</summary><ul>' + ''.join('<li>' + esc(r) + '</li>' for r in offer["reasons"]) + '</ul></details>')
            checks = offer.get("requirement_checks", []) + offer.get("condition_checks", [])
            if checks:
                rows = ''.join('<tr><th>' + esc(c.get("label") or c.get("key")) + '</th><td>' + esc(text(c.get("expected"))) + '</td><td>' + esc(text(c.get("actual"))) + '</td><td>' + esc(c.get("status")) + (' · отклонение принято пользователем' if c.get("acceptance") else '') + '</td></tr>' for c in checks)
                body.append('<details><summary>Сравнение с ТЗ</summary><div class="scroll"><table><tr><th>Требование</th><th>Нужно</th><th>В источнике</th><th>Результат</th></tr>' + rows + '</table></div></details>')
            if offer.get("manual_acceptances"):
                body.append('<details><summary>Принятые отклонения</summary><ul>' + ''.join('<li>' + esc(k) + ': ' + esc(v.get("reason")) + ' · ' + esc(v.get("at")) + '</li>' for k, v in offer["manual_acceptances"].items()) + '</ul></details>')
            if offer.get("manual_confirmations"):
                rows = []
                for key, confirmation in offer["manual_confirmations"].items():
                    proof = confirmation.get("supporting_source") or {}
                    label = "Показанное значение принято пользователем" if confirmation.get("origin") == "accepted_suggestion" else "Введено пользователем"
                    label += "; предположение по белизне CIE" if proof.get("inferred") else ""
                    source = '<blockquote>' + esc(proof["quote"]) + '</blockquote>' + link(proof.get("url"), "Источник") if proof.get("quote") else ""
                    rows.append('<li>' + esc(key) + ': ' + esc(confirmation.get("original_value")) + ' → ' + esc(confirmation.get("value")) + ' · ' + esc(confirmation.get("reason")) + ' · ' + esc(label) + source + '</li>')
                body.append('<details><summary>Ручные уточнения</summary><ul>' + ''.join(rows) + '</ul></details>')
            proofs = list((offer.get("evidence") or {}).items()) + list((details.get("evidence") or {}).items())
            quotes = ''.join('<li><b>' + esc(k) + '</b><blockquote>' + esc(p["quote"]) + '</blockquote>' + link(p.get("url"), "Источник") + '<small> · ' + esc(p.get("retrieved_at") or offer.get("retrieved_at")) + ' · SHA256 ' + esc(p.get("source_hash") or offer.get("source_hash")) + '</small></li>' for k, p in proofs if p.get("quote"))
            if quotes:
                body.append('<details><summary>Доказательства</summary><ul>' + quotes + '</ul></details>')
            body.append('</article>')
        body.append('</section>')
    search = result.get("search", {})
    audit = search.get("audit") or {}
    body.append('<section><h2>Что проверено</h2><p>' + esc((search.get("stop") or {}).get("label")) + '</p><p>Вывод относится к проверенным источникам; отсутствие товара на всём рынке не установлено.</p>')
    if audit.get("queries"):
        body.append('<details><summary>Поисковые запросы</summary><ul>' + ''.join('<li>' + esc(q) + '</li>' for q in audit["queries"]) + '</ul></details>')
    body.append('<details><summary>Сохранённые страницы · ' + str(len(documents)) + '</summary>')
    for doc in documents:
        body.append('<details><summary>' + esc(doc.get("title") or doc.get("url")) + '</summary><p>' + link(doc.get("url"), "Оригинал страницы") + '</p><small>' + esc(doc.get("retrieved_at")) + ' · SHA256 ' + esc(doc.get("sha256")) + '</small><pre>' + esc(doc.get("text")) + '</pre></details>')
        for section in doc.get("structured_sections", []):
            body.append('<details><summary>Сохранённый HTML · вкладка «' + esc(section["title"]) + '»</summary><pre>' + esc(section["text"]) + '</pre></details>')
    body.append('</details>')
    if search.get("source_statuses"):
        body.append('<details><summary>Состояние источников</summary><ul>' + ''.join('<li>' + esc(s.get("domain") or s.get("url")) + ': ' + esc(s.get("status")) + ' · ' + esc(s.get("message")) + '</li>' for s in search["source_statuses"]) + '</ul></details>')
    body.append('</section>')
    css = 'body{font:16px/1.5 system-ui,sans-serif;background:#f7f4ef;color:#24211f;max-width:1080px;margin:auto;padding:24px}h1{font-family:Georgia,serif;font-size:30px;color:#702c40}h2{font-size:23px}h3{font-size:19px}.total{font-size:34px}nav{display:flex;gap:8px;flex-wrap:wrap;margin:24px 0}nav a{background:#f4e7eb;padding:8px 12px;border-radius:8px}section,header{background:white;padding:24px;border-radius:14px;margin-bottom:20px}article{border:1px solid #ddd7d0;border-radius:12px;padding:18px;margin:16px 0}details{margin:10px 0}summary{cursor:pointer;font-weight:600;padding:6px 0}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;border-bottom:1px solid #ddd;padding:9px;vertical-align:top}small{color:#6f6864;overflow-wrap:anywhere}a{color:#702c40;overflow-wrap:anywhere}pre{font:13px/1.5 monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:540px;overflow:auto}blockquote{margin:8px 0;border-left:3px solid #702c40;padding-left:12px}.badge{display:inline-block;padding:3px 10px;background:#fff0cd;border-radius:20px;font-size:13px}.badge.included{background:#ddf5e5}.badge.excluded{background:#fce2e2}.scroll{overflow:auto}@media(max-width:600px){body{padding:10px}section,header{padding:16px}h1{font-size:24px}td,th{padding:6px}.total{font-size:28px}}'
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(title) + '</title><style>' + css + '</style><main>' + ''.join(body) + '</main></html>'

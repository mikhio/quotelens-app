"""Small same-origin HTTP app. Run: python -m quotelens.server."""
from __future__ import annotations

import copy
import html
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .engine import analyze_offers, parse_request, _requirement_match, _attribute_match, _field_confirmed, _manual_confirmation, product_identity_assessment, LABELS
from .providers import ModelProvider
from .retrieval import DEFAULT_LIMITS, SOURCES, SourceError, run_search, utcnow, validate_public_url
from .scenarios import list_scenarios, load_scenario, attach_real_images
from .agent import Research, LIMITS
from .storage import Store
from .selection import finalize
from .source_policy import normalize_scope, source_metadata, annotate_result, request_metadata
from .source_presets import source_presets

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(ROOT / ".runtime")))
JOBS = {}
LOCK = threading.RLock()
ACTIVE = threading.BoundedSemaphore(2)
PARSE_ACTIVE = threading.BoundedSemaphore(2)
IMAGE_ACTIVE = threading.BoundedSemaphore(2)
IMAGE_PENDING = threading.BoundedSemaphore(12)
IMAGE_CACHE = OrderedDict()
IMAGE_CACHE_LIMIT = 16 * 1024 * 1024
SUPPLIER_CACHE = OrderedDict()
SUPPLIER_CACHE_LOCK = threading.RLock()
SUPPLIER_CACHE_GATES = tuple(threading.Lock() for _ in range(32))
SUPPLIER_CACHE_ENTRIES = 256
SUPPLIER_CACHE_BYTES = 2 * 1024 * 1024
_STORE = None
WEB_SOURCE = {"id": "web", "name": "Интернет", "domain": "", "url": "https://duckduckgo.com/",
              "kind": "web", "description": "Открытые страницы поставщиков; факты проверяются по карточкам.", "connected": True}


def store():
    global _STORE
    with LOCK:
        if _STORE is None or _STORE.path != str(DATA_DIR / "quotelens.sqlite3"):
            _STORE = Store(DATA_DIR / "quotelens.sqlite3")
        return _STORE


def clean_text(value):
    return html.escape(str(value if value is not None else "—"), quote=True)


def price_text(value):
    if value is None:
        return "Не рассчитано"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def annotate_decisions(result, *, _review_fields=True):
    if result.get("items"):
        decorated = {}
        for item in result["items"]:
            annotate_decisions(item, _review_fields=False)
            for offer in item.get("offers", []):
                decorated[item["id"] + ":" + str(offer["id"])] = offer
        for offer in result.get("offers", []):
            found = decorated.get(offer["id"], {})
            for key in ("functional_status", "requirement_checks", "functional_reasons", "commercial_reasons", "product_identity"):
                if key in found:
                    offer[key] = copy.deepcopy(found[key])
            if found.get("product_identity", {}).get("status") == "mismatch":
                offer["status"] = "excluded"
                offer["reasons"] = list(dict.fromkeys([*offer.get("reasons", []), found["product_identity"]["reason"]]))
        from .review import annotate_review_fields
        return annotate_review_fields(result) if _review_fields else result
    """Separate explicit functional constraints from price/contract completeness."""
    request = result.get("request", {})
    try:
        parsed = parse_request(request.get("description", ""), request.get("quantity", 1))
    except ValueError:
        parsed = request
    requirements = request.get("requirements") or []
    for offer in result.get("offers", []):
        attributes, evidence = offer.get("attributes", {}), offer.get("evidence", {})
        checks, functional = [], []
        for requirement in requirements:
            key = requirement.get("key")
            if not key:
                continue
            actual = attributes.get(key)
            proof = evidence.get("attributes." + key)
            manual = _manual_confirmation(offer, "attributes." + key)
            confirmed = _field_confirmed(offer, "attributes." + key)
            verdict = _attribute_match(offer, requirement) if actual is not None and confirmed else None
            status = "matches" if verdict is True else "mismatch" if verdict is False else "unconfirmed"
            label = requirement.get("label") or LABELS.get(key, key)
            checks.append({"key": key, "label": label, "operator": requirement.get("op", "eq"),
                "expected": requirement.get("value"), "actual": actual, "unit": requirement.get("unit"),
                "status": status, "evidence": proof if confirmed and not manual else None,
                "confirmation": copy.deepcopy(manual) if manual else None})
            acceptance = (offer.get("manual_acceptances") or {}).get("attributes." + key)
            if acceptance:
                checks[-1]["acceptance"] = copy.deepcopy(acceptance)
            if verdict is False:
                functional.append(f"{label}: {actual}; требуется {requirement.get('value')} {requirement.get('unit') or ''}.".strip())
        if not checks:
            category = parsed.get("category")
            title = str(offer.get("title") or "")
            patterns = {"oscilloscope": r"\bосцил{1,2}ограф\b", "paper": r"\bбумага\b", "service": r"уборк"}
            matches_type = category in patterns and bool(re.search(patterns[category], title, re.I))
            if category == "oscilloscope" and matches_type:
                noun = re.search(patterns[category], title, re.I)
                matches_type = not bool(re.search(r"щуп|пробник|аксессуар|кабель|адаптер|футляр|кейс", title[:noun.start()], re.I))
            if matches_type:
                checks.append({"key": "category", "label": "Тип товара", "operator": "eq",
                    "expected": "Осциллограф" if category == "oscilloscope" else parsed.get("product"),
                    "actual": title, "unit": None, "status": "matches", "evidence": evidence.get("title")})
            functional_status = "matches" if matches_type else "not_specified"
        else:
            functional_status = ("mismatch" if any(row["status"] == "mismatch" for row in checks)
                                 else "unconfirmed" if any(row["status"] == "unconfirmed" for row in checks) else "matches")
        identity = product_identity_assessment(request, offer)
        offer["product_identity"] = identity
        if identity["status"] == "mismatch":
            functional_status = "mismatch"
            functional.insert(0, identity["reason"])
            checks.insert(0, {"key": "product_identity", "label": "Тип товара", "operator": "eq",
                             "expected": identity["expected"], "actual": identity["actual"], "unit": None,
                             "status": "mismatch", "evidence": copy.deepcopy(identity.get("evidence"))})
            offer["status"] = "excluded"
            offer["reasons"] = list(dict.fromkeys([*offer.get("reasons", []), identity["reason"]]))
        issues = [str(issue) for issue in offer.get("source_issues", []) if isinstance(issue, str)]
        if issues:
            offer["reasons"] = list(dict.fromkeys([*offer.get("reasons", []), *issues]))
        commercial = list(issues)
        if offer.get("price") is None:
            commercial.append("Цена не подтверждена.")
        if not offer.get("unit"):
            commercial.append("Единица цены не подтверждена.")
        if offer.get("tax") == "unknown" or not offer.get("tax"):
            commercial.append("НДС не указан.")
        elif offer.get("tax") == "excluded":
            commercial.append("Цена указана без НДС.")
        offer.update(functional_status=functional_status, requirement_checks=checks,
                     functional_reasons=functional, commercial_reasons=commercial)
    from .review import annotate_review_fields
    from .targeting import annotate_target
    annotate_target(result, enforce=result.get("search", {}).get("search_policy", {}).get("mode") == "target")
    return annotate_review_fields(result) if _review_fields else result


def report_html(result):
    from .reports import report_html as render
    return render(result)


def refresh_calculation_view(result):
    """Recompute a saved derived view without fetching prices or altering audit."""
    if result.get("items"):
        return result
    base = result.get("base_result")
    if base and "provisional" not in result.get("calculation", {}):
        fresh = finalize(base["request"], base["offers"], base.get("policy"), result.get("review", {}).get("excluded_ids", []))
        for field in ("search", "discovery_pages", "validation_notice", "review", "input_documents"):
            if field in result:
                fresh[field] = copy.deepcopy(result[field])
        return fresh
    return result


def refresh_saved_product_identity(job):
    """Apply the pure saved-view replay; never write a revision or re-fetch."""
    from .procurement import refresh_product_identity_view
    return refresh_product_identity_view(job.get("result") or {})


def _captured_supplier_view(database, job_id, identifier, source_hash):
    """Parse one immutable capture without owning the application's job lock."""
    from .supplier import supplier_details
    key = (database.path, job_id, identifier, source_hash)

    def cached_view():
        with SUPPLIER_CACHE_LOCK:
            found = SUPPLIER_CACHE.get(key)
            if found is not None:
                SUPPLIER_CACHE.move_to_end(key)
                return copy.deepcopy(found[0])
        return None

    cached = cached_view()
    if cached is not None:
        return cached
    # Bounded lock striping prevents concurrent reads of the same capture from
    # repeating a large HTML parse. Neither this gate nor the cache mutex is the
    # global LOCK used to publish events and handle cancellation.
    with SUPPLIER_CACHE_GATES[hash(key) % len(SUPPLIER_CACHE_GATES)]:
        cached = cached_view()
        if cached is not None:
            return cached
        document = database.get_document(job_id, identifier)
        details = supplier_details(document) if document else {}
        size = len(json.dumps(details, ensure_ascii=False).encode("utf-8"))
        if size <= SUPPLIER_CACHE_BYTES:
            with SUPPLIER_CACHE_LOCK:
                SUPPLIER_CACHE[key] = (copy.deepcopy(details), size)
                SUPPLIER_CACHE.move_to_end(key)
                while (len(SUPPLIER_CACHE) > SUPPLIER_CACHE_ENTRIES
                       or sum(entry[1] for entry in SUPPLIER_CACHE.values()) > SUPPLIER_CACHE_BYTES):
                    SUPPLIER_CACHE.popitem(last=False)
        return details


def captured_supplier_details(job_id, result, *, database=None, used_only=False):
    """Add source-backed contact views to detached jobs; never rewrite captures."""
    database = database or store()
    by_document = {}
    lines = [result, *result.get("items", [])]

    def selected_ids(line):
        calculation = line.get("calculation") or {}
        values = [*calculation.get("selected_offer_ids", []),
                  *(calculation.get("provisional") or {}).get("offer_ids", [])]
        return {str(value) for value in values if isinstance(value, (str, int)) and not isinstance(value, bool)}

    root_selected = selected_ids(result)
    for line in lines:
        selected = selected_ids(line)
        prefix = str(line["id"]) + ":" if line is not result and line.get("id") is not None else ""
        for offer in line.get("offers", []):
            if offer.get("supplier_details") is not None or not offer.get("document_id"):
                continue
            if used_only and not (offer.get("status") == "included" or str(offer.get("id")) in selected
                                  or prefix + str(offer.get("id")) in root_selected):
                continue
            identifier = str(offer["document_id"])
            source_hash = offer.get("source_hash")
            capture = (identifier, source_hash if isinstance(source_hash, str) else None)
            if capture not in by_document:
                by_document[capture] = _captured_supplier_view(database, job_id, *capture)
            offer["supplier_details"] = copy.deepcopy(by_document[capture])
    return result


def acquired_offer_image(job, identifier):
    """Fetch only a saved offer's image through the public-address transport."""
    offer = next((o for o in (job.get("result") or {}).get("offers", []) if o.get("id") == identifier), None)
    if not offer or not isinstance(offer.get("image_url"), str):
        return None
    key = (offer["image_url"], offer.get("source_hash"))
    with LOCK:
        cached = IMAGE_CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= 300:
            IMAGE_CACHE.move_to_end(key)
            return cached[1]
        IMAGE_CACHE.pop(key, None)
    if not IMAGE_PENDING.acquire(blocking=False):
        raise BlockingIOError("Изображения загружаются. Повторите запрос.")
    acquired = False
    try:
        # One page may request several thumbnails together. Bound both waiting
        # requests and upstream I/O, rather than rejecting the third image.
        if not IMAGE_ACTIVE.acquire(timeout=8):
            raise BlockingIOError("Изображения загружаются. Повторите запрос.")
        acquired = True
        with LOCK:
            cached = IMAGE_CACHE.get(key)
            if cached and time.monotonic() - cached[0] <= 300:
                IMAGE_CACHE.move_to_end(key)
                return cached[1]
        from .acquisition import acquire_image
        image = acquire_image(offer["image_url"], timeout=8)
        if not isinstance(image, dict) or not isinstance(image.get("body"), bytes) or not 0 < len(image["body"]) <= 8 * 1024 * 1024 or image.get("mime") not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}:
            raise ValueError("Изображение недоступно.")
        with LOCK:
            IMAGE_CACHE[key] = (time.monotonic(), image)
            while len(IMAGE_CACHE) > 24 or sum(len(item[1]["body"]) for item in IMAGE_CACHE.values()) > IMAGE_CACHE_LIMIT:
                IMAGE_CACHE.popitem(last=False)
        return image
    finally:
        if acquired:
            IMAGE_ACTIVE.release()
        IMAGE_PENDING.release()


def save_job(job):
    public = {k: v for k, v in job.items() if k not in {"cancel"}}
    store().save_job(public)


def public_job(job):
    return copy.deepcopy({k: v for k, v in job.items() if k != "cancel"})


def execute(job, payload):
    def event(message, kind="info"):
        with LOCK:
            if len(job["events"]) < 160:
                entry = {"time": utcnow(), "message": message[:700], "kind": kind}
                job["events"].append(entry)
                store().add_event(job["id"], entry)
    try:
        request = parse_request(payload["description"], payload["quantity"])
        if payload["mode"] == "snapshot":
            scenario = load_scenario(payload["scenario_id"])
            event("Открываю сохранённый пример. Новые запросы к источникам и модели не выполняются.")
            # The current description is analyzed against the saved candidate set.
            offers = copy.deepcopy(scenario["offers"])
            if payload.get("source_scope", {}).get("mode") == "domains":
                domains = set(payload["source_scope"]["domains"])
                offers = [o for o in offers if (o.get("domain") or "").removeprefix("www.") in domains]
            elif payload.get("source_ids") is not None and "web" not in payload["source_ids"]:
                domains = {s["domain"] for s in SOURCES if s["id"] in payload["source_ids"]}
                offers = [o for o in offers if (o.get("domain") or "").removeprefix("www.") in domains]
            result = analyze_offers(request, offers, {"min_suppliers": payload["limits"]["min_suppliers"]})
            kind = scenario.get("kind", "synthetic")
            event("Учебные данные: цены и поставщики придуманы для проверки." if kind == "synthetic" else "Используются сохранённые реальные карточки с датой получения.", "warning" if kind == "synthetic" else "info")
            budget = {"pages_used": 0, "max_pages": payload["limits"]["max_pages"], "queries_used": 0, "max_queries": payload["limits"]["max_queries"],
                      "model_calls": 0, "max_model_calls": payload["limits"]["max_model_calls"], "elapsed_seconds": 0, "max_seconds": payload["limits"]["max_seconds"]}
            source_statuses = scenario.get("source_statuses", [])
            if payload.get("source_scope", {}).get("mode") == "domains" or payload.get("source_ids") is not None and "web" not in payload["source_ids"]:
                source_statuses = [s for s in source_statuses if (s.get("domain") or "").removeprefix("www.") in domains]
            result["search"] = {"mode": "snapshot", "started_at": job["created_at"], "finished_at": utcnow(), "scenario_kind": kind,
                                "captured_at": scenario.get("captured_at"), "min_suppliers": payload["limits"]["min_suppliers"], "stop": {"code": "snapshot_complete", "label": "Сохранённая выборка обработана", "detail": "Повторный прогон не означает нового поиска или полноты рынка."},
                                "budget": budget, "source_statuses": source_statuses, "completeness": "bounded_not_exhaustive", "model_used": False}
        else:
            if payload.get("items") and payload.get("search_policy"):
                from .procurement import ProcurementResearch
                result = ProcurementResearch(store(), job["id"], payload, job["cancel"], event).run()
            else:
                result = Research(store(), job["id"], payload, job["cancel"], event).run()
            result["search"]["min_suppliers"] = payload["limits"]["min_suppliers"]
        annotate_decisions(annotate_result(result))
        captured_supplier_details(job["id"], result)
        result.setdefault("search", {})["audit"] = store().search_audit(job["id"])
        attach_real_images(result["offers"])
        with LOCK:
            store().save_result(job["id"], result)
            job["result"] = store().get_job(job["id"])["result"]
            job["status"] = "cancelled" if job["cancel"].is_set() else "complete"
            event("Расчёт завершён." if result["calculation"]["status"] == "ready" else "Надёжных данных для итоговой цены недостаточно.", "success" if result["calculation"]["status"] == "ready" else "warning")
            save_job(job)
    except Exception:
        with LOCK:
            latest = store().get_job(job["id"])
            if latest and latest.get("result"):
                job["result"] = latest["result"]
            job["status"] = "failed"
            job["error"] = "Не удалось завершить поиск. Повторите запрос или выберите другие источники."
            save_job(job)
    finally:
        ACTIVE.release()


class PreparationBusy(ValueError):
    pass


def prepare_input(description="", files=None, *, context_only=False):
    """Every attachment entry path shares this bounded parsing admission gate."""
    if not PARSE_ACTIVE.acquire(blocking=False):
        raise PreparationBusy("Сейчас разбираются другие ТЗ. Попробуйте через несколько секунд.")
    try:
        from .spec_ingest import prepare_specifications, prepare_context_note
        if not isinstance(context_only, bool):
            raise ValueError("Некорректный режим обработки дополнения.")
        if context_only:
            if files is not None and (not isinstance(files, list) or files):
                raise ValueError("Отправьте дополнение отдельно от новых файлов.")
            return prepare_context_note(description)
        prepared = prepare_specifications(description, files or [])
        # The document parser preserves structure. For a short ordinary text,
        # also expose the existing known-category rules in the editable preview
        # instead of keeping parsed conditions hidden until research starts.
        for item in prepared.get("items", []):
            if not item.get("requirements") and len(item.get("description", "")) <= 1000:
                parsed = parse_request(item["description"], item.get("quantity") or 1)
                for requirement in parsed.get("requirements", []):
                    item["requirements"].append({**requirement, "quote": item["description"],
                                                "source": copy.deepcopy(item.get("source", {}))})
        return prepared
    finally:
        PARSE_ACTIVE.release()


def validate_payload(value):
    if not isinstance(value, dict):
        raise ValueError("Ожидается объект с описанием закупки.")
    prepared = None
    if value.get("files"):
        prepared = prepare_input(value.get("description", ""), value["files"])
        value = {**value, "description": prepared["description"], "items": value.get("items") or prepared["items"],
                 "input_documents": prepared["documents"]}
    description = value.get("description")
    if not isinstance(description, str) or len(description.strip()) < 3 or len(description) > 120000:
        raise ValueError("Добавьте описание закупки от 3 до 120000 символов.")
    quantity = value.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0 or quantity > 1_000_000:
        raise ValueError("Количество должно быть положительным числом не больше 1 000 000.")
    mode = value.get("mode", "live")
    if mode not in {"live", "snapshot"}:
        raise ValueError("Неизвестный режим анализа.")
    if mode == "snapshot":
        if value.get("scenario_id") not in {s["id"] for s in list_scenarios()}:
            raise ValueError("Выберите существующий сохранённый пример.")
    urls = value.get("source_urls", [])
    if not isinstance(urls, list) or len(urls) > 10:
        raise ValueError("Можно добавить не более десяти ссылок.")
    if mode == "live":
        for url in urls:
            try:
                validate_public_url(url, require_allowed=False)
            except SourceError as exc:
                raise ValueError(str(exc)) from None
    limits = dict(LIMITS)
    incoming = value.get("limits", {})
    if not isinstance(incoming, dict):
        raise ValueError("Некорректные ограничения поиска.")
    ranges = {"min_suppliers": (2, 10), "max_pages": (1, 24), "max_queries": (1, 8), "max_seconds": (1, 360), "max_model_calls": (0, 12)}
    for key, val in incoming.items():
        if key not in ranges or isinstance(val, bool) or not isinstance(val, int) or not ranges[key][0] <= val <= ranges[key][1]:
            raise ValueError("Недопустимый лимит поиска: " + str(key))
        limits[key] = val
    source_ids = value.get("source_ids")
    if source_ids is not None:
        active_ids = {s["id"] for s in SOURCES if s["kind"] in {"catalog", "supplier"}} | {"web"}
        if not isinstance(source_ids, list) or not source_ids or any(not isinstance(i, str) or i not in active_ids for i in source_ids) or len(source_ids) != len(set(source_ids)):
            raise ValueError("Выберите подключённые источники поиска.")
    payload = {"description": description.strip(), "quantity": quantity, "mode": mode, "scenario_id": value.get("scenario_id"), "source_urls": urls, "limits": limits, "source_ids": source_ids}
    if "source_scope" in value:
        if source_ids is not None:
            raise ValueError("Задайте область поиска либо прежний список источников, а не оба сразу.")
        scope = normalize_scope(value["source_scope"])
        if mode == "live":
            for domain in scope["domains"]:
                try:
                    validate_public_url("https://" + domain + "/", require_allowed=False)
                except SourceError as exc:
                    raise ValueError(str(exc)) from None
        payload["source_scope"] = scope
    from .procurement import normalize_search_policy
    policy = normalize_search_policy(value.get("search_policy"))
    items = value.get("items")
    if items is not None:
        from .spec_ingest import normalize_items
        items = normalize_items(items)
        if not items:
            raise ValueError("Добавьте хотя бы одну позицию закупки.")
        for item in items:
            if item.get("quantity") is None or item["quantity"] <= 0:
                raise ValueError("Укажите количество для позиции: " + str(item.get("title", ""))[:100])
        payload["items"] = items
        policy = policy or normalize_search_policy({"mode": "target", "max_seconds": 600})
    elif policy is not None:
        from .spec_ingest import normalize_items
        prepared = prepare_input(description, [])
        if prepared.get("items"):
            payload["items"] = normalize_items(prepared["items"])
        else:
            payload["items"] = [{"id": "item-1", "title": description.splitlines()[0][:180],
                                 "description": description, "quantity": quantity, "unit": None}]
        # A simple description has the explicit form quantity. Multi-line
        # source quantities, where found, remain attached to their own items.
        # This is automatic preparation of raw text, not a reviewed explicit
        # empty selection. Unknown categories may still be structured by the
        # model against their exact source text.
        for item in payload["items"]:
            if item.get("requirements") == []:
                item.pop("requirements", None)
        if len(payload["items"]) == 1 and payload["items"][0].get("quantity") is None:
            payload["items"][0]["quantity"] = quantity
        for item in payload["items"]:
            if item.get("quantity") is None:
                raise ValueError("Проверьте количества распознанных позиций перед поиском.")
    if policy is not None:
        payload["search_policy"] = policy
    documents = value.get("input_documents", prepared.get("documents", []) if prepared else [])
    if not isinstance(documents, list) or len(documents) > 128:
        raise ValueError("Некорректные исходные файлы закупки.")
    safe_documents, total_document_text = [], 0
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get("name"), str) or not isinstance(document.get("text", ""), str):
            raise ValueError("Некорректный исходный документ.")
        if len(document["name"]) > 250 or len(document.get("text", "")) > 250000:
            raise ValueError("Исходный документ слишком велик.")
        total_document_text += len(document.get("text", ""))
        if total_document_text > 1_000_000:
            raise ValueError("Суммарный текст исходных документов и правок слишком велик.")
        safe_documents.append({k: copy.deepcopy(document[k]) for k in ("id", "name", "sha256", "text", "warnings") if k in document})
    if safe_documents:
        payload["input_documents"] = safe_documents
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "QuoteLens/0.1"

    def log_message(self, fmt, *args):
        # No input bodies, tokens, upstream response bodies or arbitrary URL logging.
        pass

    def respond(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode() if content_type.startswith("application/json") else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        shared = re.fullmatch(r"/api/shared/([A-Za-z0-9_-]{43})/(report\.pdf|report\.docx)?", path)
        if shared:
            snapshot = store().get_share(shared[1])
            if not snapshot:
                return self.respond(404, {"error": "Ссылка не найдена."})
            headers = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}
            if shared[2]:
                from .reports import report_pdf, report_docx
                pdf = shared[2].endswith("pdf")
                content = report_pdf(snapshot["result"]) if pdf else report_docx(snapshot["result"])
                mime = "application/pdf" if pdf else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                return self.respond(200, content, mime, {**headers, "Content-Disposition": 'attachment; filename="quotelens-report.' + ("pdf" if pdf else "docx") + '"'})
            from .sharing import reader_html
            return self.respond(200, reader_html(snapshot["result"], snapshot["documents"], revision=snapshot["revision"], created_at=snapshot["created_at"]), "text/html; charset=utf-8", headers)
        if path == "/health":
            return self.respond(200, {"status": "ok", "version": __version__})
        if path == "/api/config":
            model = ModelProvider()
            return self.respond(200, {"model": {"configured": model.configured, "name": model.model or None}, "search": {"configured": True, "mode": "live_agent", "broad_web_configured": True}, "defaults": LIMITS, "version": __version__, "review": True,
                "uploads": {"extensions": ["docx", "pdf", "txt", "md"], "max_files": 8},
                "search_policy": {"modes": ["target", "offers", "time"], "default_mode": "target", "target_offers": 3, "cv_limit": 33, "default_seconds": 600, "max_offers": 100, "max_seconds": 3600}})
        if path == "/api/costs":
            return self.respond(200, store().cost_report())
        if path == "/api/scenarios":
            items = []
            for item in list_scenarios():
                scenario = load_scenario(item["id"])
                item = dict(item)
                item["request"] = {k: scenario["request"].get(k) for k in ["description", "quantity"]}
                items.append(item)
            return self.respond(200, items)
        if path == "/api/sources":
            return self.respond(200, [{**WEB_SOURCE, "selectable": True}] + source_presets())
        if path == "/api/jobs":
            try:
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
                if any(key not in {"limit", "cursor"} or len(values) != 1 for key, values in params.items()):
                    raise ValueError("Некорректные параметры истории поисков.")
                raw_limit = params.get("limit", ["20"])[0]
                if not raw_limit.isascii() or not raw_limit.isdecimal():
                    raise ValueError("Размер страницы истории должен быть целым числом от 1 до 50.")
                limit = int(raw_limit)
                if not 1 <= limit <= 50:
                    raise ValueError("Размер страницы истории должен быть от 1 до 50.")
                cursor = params.get("cursor", [None])[0]
                return self.respond(200, store().job_history(limit=limit, cursor=cursor))
            except ValueError as exc:
                return self.respond(400, {"error": str(exc)[:300]})
        docmatch = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/documents(?:/([a-zA-Z0-9_-]{1,80}))?", path)
        if docmatch:
            if not store().get_job(docmatch[1]):
                return self.respond(404, {"error": "Анализ не найден."})
            if not docmatch[2]:
                return self.respond(200, store().list_documents(docmatch[1]))
            doc = store().get_document(docmatch[1], docmatch[2])
            if not doc:
                return self.respond(404, {"error": "Документ не найден."})
            # Captured HTML is inert source evidence, never executable product HTML.
            visible = {k: v for k, v in doc.items() if k != "html"}
            from .price_context import primary_attribute_rows
            sections = {}
            for row in primary_attribute_rows(doc):
                if row.get("visibility") == "reachable_tab":
                    sections.setdefault(row.get("tab_label") or "Характеристики", []).append(row["quote"])
            if sections:
                visible["structured_sections"] = [{"title": key, "basis": "captured_html_reachable_tab", "text": "\n".join(dict.fromkeys(values))} for key, values in sections.items()]
            return self.respond(200, visible)
        imagematch = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/images/([^/]{1,400})", path)
        if imagematch:
            if urllib.parse.urlsplit(self.path).query:
                return self.respond(400, {"error": "Параметры изображения не поддерживаются."})
            job = store().get_job(imagematch[1])
            if not job:
                return self.respond(404, {"error": "Анализ не найден."})
            try:
                image = acquired_offer_image(job, urllib.parse.unquote(imagematch[2]))
                if not image:
                    return self.respond(404, {"error": "Изображение не найдено."})
                return self.respond(200, image["body"], image["mime"], {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
            except BlockingIOError:
                return self.respond(429, {"error": "Изображения загружаются. Повторите запрос."})
            except Exception:
                return self.respond(502, {"error": "Не удалось загрузить изображение товара."})
        suppliermatch = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/suppliers/([^/]{1,400})", path)
        if suppliermatch:
            if urllib.parse.urlsplit(self.path).query:
                return self.respond(400, {"error": "Параметры сведений о поставщике не поддерживаются."})
            database = store()
            job = database.get_job(suppliermatch[1])
            if not job:
                return self.respond(404, {"error": "Анализ не найден."})
            identifier = urllib.parse.unquote(suppliermatch[2])
            offer = next((row for row in (job.get("result") or {}).get("offers", [])
                          if str(row.get("id")) == identifier), None)
            if offer is None:
                return self.respond(404, {"error": "Предложение не найдено."})
            document_id = offer.get("document_id")
            if document_id:
                source_hash = offer.get("source_hash")
                details = _captured_supplier_view(database, suppliermatch[1], str(document_id),
                                                  source_hash if isinstance(source_hash, str) else None)
            else:
                from .supplier import supplier_details
                details = supplier_details({})
            return self.respond(200, {"offer_id": identifier, "supplier_details": details})
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{24})(?:/(report\.html|report\.json|report\.pdf|report\.docx))?", path)
        if match:
            report = match[2]
            database = store()
            # Store.get_job returns a detached SQLite snapshot. The global lock
            # is needed only for the fallback to the mutable in-memory worker.
            # HTML parsing, annotations and report preparation must not prevent
            # another request from listing jobs, publishing an event or cancelling.
            current = database.get_job(match[1])
            if current is None:
                with LOCK:
                    live = JOBS.get(match[1])
                    current = public_job(live) if live is not None else None
            if current is None:
                try:
                    saved = DATA_DIR / (match[1] + ".json")
                    current = json.loads(saved.read_text())
                except (OSError, ValueError):
                    return self.respond(404, {"error": "Анализ не найден."})
            if current.get("result"):
                current["result"] = refresh_calculation_view(refresh_saved_product_identity(current))
                annotate_decisions(annotate_result(current["result"]))
                # Cold legacy jobs can contain hundreds of captures. Their
                # prices/history must open without reparsing every saved HTML.
                # A selected supplier is read lazily above; only explicit human
                # document exports request bulk contact enrichment.
                if report in {"report.html", "report.pdf", "report.docx"}:
                    captured_supplier_details(match[1], current["result"], database=database, used_only=True)
                current["result"].setdefault("search", {})["audit"] = database.search_audit(match[1])
                if not current.get("search_progress"):
                    current["search_progress"] = {
                        "phase": "running" if current.get("status") == "running" else "complete",
                        "sources": current["result"].get("search", {}).get("source_statuses", []),
                        "offers_count": len(current["result"].get("offers", [])),
                        "budget": current["result"].get("search", {}).get("budget", {})}
            if report:
                if not current.get("result"):
                    return self.respond(409, {"error": "Анализ ещё не завершён."})
                if report.endswith("pdf") or report.endswith("docx"):
                    from .reports import report_pdf, report_docx
                    try:
                        if report.endswith("pdf"):
                            content, mime, filename = report_pdf(current["result"]), "application/pdf", "quotelens-report.pdf"
                        else:
                            content, mime, filename = report_docx(current["result"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "quotelens-report.docx"
                        return self.respond(200, content, mime, {"Content-Disposition": 'attachment; filename="' + filename + '"'})
                    except Exception:
                        return self.respond(503, {"error": "Не удалось подготовить этот формат отчёта. Попробуйте ещё раз или выберите другой формат."})
                if report.endswith("html"):
                    return self.respond(200, report_html(current["result"]), "text/html; charset=utf-8", {"Content-Disposition": 'inline; filename="quotelens-report.html"'})
                return self.respond(200, current["result"], extra={"Content-Disposition": 'attachment; filename="quotelens-report.json"'})
            return self.respond(200, current)
        if path.startswith("/api/"):
            return self.respond(404, {"error": "Метод не найден."})
        relative = "index.html" if path == "/" else urllib.parse.unquote(path).removeprefix("/static/").lstrip("/")
        filename = (ROOT / "static" / relative).resolve()
        if not filename.is_relative_to((ROOT / "static").resolve()) or not filename.is_file():
            return self.respond(404, "Страница не найдена.", "text/plain; charset=utf-8")
        content_type = mimetypes.guess_type(str(filename))[0] or "application/octet-stream"
        return self.respond(200, filename.read_bytes(), content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path.startswith("/api/shared/"):
            return self.respond(405, {"error": "Обоснование доступно только для чтения."})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host") and origin != os.getenv("APP_ALLOWED_ORIGIN"):
            return self.respond(403, {"error": "Разрешены только запросы из этого приложения."})
        path = urllib.parse.urlsplit(self.path).path
        operation = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/(share|continue)", path)
        if operation:
            admitted = False
            daily_reserved = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2000:
                    raise ValueError("Недопустимый размер запроса.")
                value = json.loads(self.rfile.read(length))
                allowed = {"expected_revision"} | ({"max_seconds"} if operation[2] == "continue" else set())
                if not isinstance(value, dict) or set(value) - allowed:
                    raise ValueError("Некорректные параметры.")
                if operation[2] == "share":
                    database = store()
                    job = database.get_job(operation[1])
                    if not job:
                        return self.respond(404, {"error": "Исследование не найдено."})
                    result = annotate_decisions(annotate_result(refresh_saved_product_identity(job)))
                    captured_supplier_details(job["id"], result, database=database, used_only=True)
                    # The Store atomically checks status and expected_revision;
                    # an intervening review cannot publish an out-of-date view.
                    receipt = database.create_share(job["id"], value.get("expected_revision"), result)
                    base = os.getenv("APP_PUBLIC_SHARE_BASE", "/api/shared/").rstrip("/") + "/"
                    receipt["url"] = base + receipt.pop("token") + "/"
                    return self.respond(201, receipt)
                with LOCK:
                    job = store().get_job(operation[1])
                    if not job:
                        return self.respond(404, {"error": "Исследование не найдено."})
                    if job["status"] == "running":
                        return self.respond(409, {"error": "Дождитесь завершения текущего поиска."})
                    if not ACTIVE.acquire(blocking=False):
                        return self.respond(429, {"error": "Сейчас выполняются два исследования. Попробуйте после завершения."})
                    admitted = True
                    daily_limit = int(os.getenv("APP_SEARCH_RUNS_DAY", "0"))
                    if not store().quota_reserve("search_runs", 1, daily_limit if daily_limit > 0 else 2**63-1):
                        return self.respond(429, {"error": "Сегодня исчерпан лимит проходов поиска."})
                    daily_reserved = True
                    payload = store().begin_continuation(job["id"], value.get("max_seconds", 600), value.get("expected_revision"))
                    resumed = store().get_job(job["id"])
                    resumed["cancel"] = threading.Event()
                    JOBS[job["id"]] = resumed
                threading.Thread(target=execute, args=(resumed, payload), daemon=True).start()
                admitted = False  # worker now owns semaphore release
                daily_reserved = False
                return self.respond(202, public_job(resumed))
            except (ValueError, TypeError, KeyError) as exc:
                from .review import RevisionConflict
                if isinstance(exc, RevisionConflict):
                    return self.respond(409, {"error": str(exc), "code": "revision_conflict", "current_revision": exc.current_revision})
                return self.respond(400, {"error": str(exc)[:300]})
            finally:
                if admitted:
                    ACTIVE.release()
                if daily_reserved:
                    store().quota_release("search_runs", 1)
        if path == "/api/prepare":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 24_000_000:
                    raise ValueError("Суммарный размер файлов слишком велик.")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError("Ожидаются текст и файлы закупки.")
                context_only = value.get("context_only", False)
                if not isinstance(context_only, bool):
                    raise ValueError("Некорректный режим обработки дополнения.")
                if context_only:
                    prepared = prepare_input(value.get("description", ""), value.get("files", []), context_only=True)
                else:
                    prepared = prepare_input(value.get("description", ""), value.get("files", []))
                return self.respond(200, prepared)
            except PreparationBusy as exc:
                return self.respond(429, {"error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                return self.respond(400, {"error": str(exc)[:300]})
        review = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/review", path)
        if review:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16000:
                    raise ValueError("Недопустимый размер проверки.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Некорректная проверка.")
                action, identifier = payload.get("action"), payload.get("offer_id")
                reason = payload.get("reason", None if action == "update" else "" if action in {"confirm_value", "reset_field"} else "Проверка пользователем")
                if set(payload) - {"action", "offer_id", "reason", "fields", "expected_revision"} or action not in {"update", "exclude", "reset", "accept_deviation", "confirm_value", "reset_field"} or not isinstance(identifier, str) or not isinstance(reason, str) or len(reason) > 300:
                    raise ValueError("Некорректное действие проверки.")
                with LOCK:
                    job = store().get_job(review[1])
                    if not job:
                        return self.respond(404, {"error": "Анализ не найден."})
                    if job["status"] == "running" or not job.get("result"):
                        return self.respond(409, {"error": "Дождитесь завершения поиска."})
                    original = job["result"]
                    base = original.get("base_result")
                    if not base and not original.get("items"):
                        return self.respond(409, {"error": "Этот сохранённый пример не поддерживает пересчёт."})
                    store().apply_review(job["id"], identifier, action, reason, fields=payload.get("fields"), expected_revision=payload.get("expected_revision"))
                    updated = store().get_job(job["id"])
                    result = updated["result"]
                    annotate_decisions(annotate_result(result))
                    attach_real_images(result["offers"])
                    job.update(result=result, revision=updated["revision"])
                    # Persistence and audit were committed together by apply_review.
                    # Return only derived presentation additions from annotation.
                    updated["result"] = result
                    return self.respond(200, updated)
            except (ValueError, TypeError, KeyError) as exc:
                from .review import RevisionConflict
                if isinstance(exc, RevisionConflict):
                    return self.respond(409, {"error": str(exc), "code": "revision_conflict", "current_revision": exc.current_revision})
                return self.respond(400, {"error": str(exc)[:300]})
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/cancel", path)
        if match:
            with LOCK:
                job = JOBS.get(match[1])
                if not job:
                    return self.respond(404, {"error": "Анализ не найден."})
                if job["status"] == "running":
                    job["cancel"].set()
                return self.respond(200, {"id": job["id"], "status": job["status"]})
        if path != "/api/search":
            return self.respond(404, {"error": "Метод не найден."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 24_000_000:
                raise ValueError("Недопустимый размер запроса.")
            payload = validate_payload(json.loads(self.rfile.read(length)))
        except PreparationBusy as exc:
            return self.respond(429, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            return self.respond(400, {"error": str(exc)[:300]})
        if not ACTIVE.acquire(blocking=False):
            return self.respond(429, {"error": "Сейчас выполняются два анализа. Дождитесь завершения одного из них."})
        daily_limit = int(os.getenv("APP_SEARCH_RUNS_DAY", "0"))
        if payload["mode"] == "live" and not store().quota_reserve("search_runs", 1, daily_limit if daily_limit > 0 else 2**63-1):
            ACTIVE.release()
            return self.respond(429, {"error": "Сегодня исчерпан лимит поисков. Сохранённые результаты доступны."})
        with LOCK:
            if len(JOBS) >= 80:
                completed = [k for k, v in JOBS.items() if v["status"] != "running"]
                for old in completed[:40]:
                    del JOBS[old]
            request = request_metadata(payload)
            job = {"id": secrets.token_hex(12), "status": "running", "events": [], "created_at": utcnow(),
                   "request": request, "cancel": threading.Event()}
            JOBS[job["id"]] = job
            store().create_job(public_job(job), payload)
        threading.Thread(target=execute, args=(job, payload), daemon=True).start()
        return self.respond(202, {"id": job["id"], "status": "running", "request": request, "created_at": job["created_at"]})


def main():
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    store().recover_interrupted()
    print(f"QuoteLens: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

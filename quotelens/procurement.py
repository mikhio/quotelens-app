"""One durable research job, several procurement lines, one explicit budget.

The scheduler advances lines in turn. Captured evidence stays in the parent job;
no hidden child job consumes a daily search allowance.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
import math
import re
import threading
import time
from decimal import Decimal
from urllib.parse import urlsplit

from .engine import parse_request, _canonical_url, _model, _supplier_name, _requirement_match, _attribute_match, positive_product_identity, product_identity_assessment
from .selection import finalize
from .source_policy import annotate_result, request_metadata
from .retrieval import utcnow
from .targeting import annotate_target


STOP_LABELS = {
    "offer_target": "Найдено выбранное количество предложений",
    "target_complete": "По каждой позиции собрана сопоставимая группа по выбранному профилю",
    "time_budget": "Истекло выбранное время поиска",
    "source_exhausted": "Доступные найденные ссылки и варианты запросов обработаны",
    "cancelled": "Поиск остановлен пользователем",
    "global_budget": "Достигнут доступный бюджет сервиса",
    "cost_budget": "Достигнут бюджет этого исследования",
    "token_budget": "Достигнут лимит токенов этого исследования",
    "context_budget": "Достигнут лимит контекста модели",
    "model_budget": "Достигнут лимит вызовов модели",
    "page_budget": "Достигнут лимит страниц",
    "provider_error": "Модель временно недоступна; найденные ссылки обработаны",
    "provider_unavailable": "Модель не настроена; сохранено найденное",
    "provider_usage_error": "Расход провайдера требует проверки",
}

MAX_RESEARCH_MODEL_CALLS = 30


def normalize_search_policy(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"mode", "max_offers", "max_seconds", "max_cost_usd", "max_tokens"}:
        raise ValueError("Некорректное условие завершения поиска.")
    mode = value.get("mode", "offers")
    if mode not in {"offers", "time", "target"}:
        raise ValueError("Выберите профиль группы, ограничение по времени или числу предложений.")
    result = {"mode": mode}
    for key, default, low, high in (("max_offers", 3 if mode == "target" else 100, 1, 100), ("max_seconds", 600 if mode == "target" else 3600, 1, 3600)):
        amount = value.get(key, default)
        if isinstance(amount, bool) or not isinstance(amount, int) or not low <= amount <= high:
            raise ValueError("Недопустимое значение: " + key)
        result[key] = amount
    if mode == "target" and result["max_offers"] != 3:
        raise ValueError("Выбранный профиль требует три предложения по каждой позиции.")
    cost = value.get("max_cost_usd")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or not .001 <= cost <= 10:
            raise ValueError("Бюджет исследования должен быть от0,001 до10USD.")
        result["max_cost_usd"] = float(cost)
    tokens = value.get("max_tokens")
    if tokens is not None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1000 <= tokens <= 10_000_000:
            raise ValueError("Некорректный лимит токенов исследования.")
        result["max_tokens"] = tokens
    return result


class JobBudget:
    def __init__(self, policy, cancel):
        self.policy = policy
        self.cancel = cancel
        self.started = time.monotonic()
        self.started_at = utcnow()
        self.lock = threading.RLock()
        self.offer_keys = {}
        self.document_cache = OrderedDict()
        self.committed_micro = self.pending_micro = 0
        self.committed_tokens = self.pending_tokens = 0
        self.model_calls = 0
        self.reason = None

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    @property
    def count(self):
        return len(set().union(*self.offer_keys.values())) if self.offer_keys else 0

    def check(self):
        from .agent import BudgetStop
        if self.cancel.is_set():
            raise BudgetStop("cancelled")
        if self.elapsed >= self.policy["max_seconds"]:
            raise BudgetStop("time_budget")
        if self.policy["mode"] == "offers" and self.count >= self.policy["max_offers"]:
            raise BudgetStop("offer_target")

    def before_reserve(self, tokens, micro):
        from .agent import BudgetStop
        self.check()
        if self.model_calls >= MAX_RESEARCH_MODEL_CALLS:
            raise BudgetStop("model_budget")
        cap = self.policy.get("max_cost_usd")
        if cap is not None and self.committed_micro + self.pending_micro + micro > math.floor(cap * 1_000_000):
            raise BudgetStop("cost_budget")
        cap = self.policy.get("max_tokens")
        if cap is not None and self.committed_tokens + self.pending_tokens + tokens > cap:
            raise BudgetStop("token_budget")

    def reserved(self, tokens, micro):
        self.pending_tokens += tokens
        self.pending_micro += micro
        self.model_calls += 1

    def settled(self, reserved, used_tokens, used_micro):
        tokens, micro = reserved
        self.pending_tokens = max(0, self.pending_tokens - tokens)
        self.pending_micro = max(0, self.pending_micro - micro)
        self.committed_tokens += used_tokens
        self.committed_micro += used_micro

    def touch_document(self, research, document_id):
        # Full captures are durable in SQLite. An hour-long multi-line job must
        # not retain every HTML body in Python heap just to allow later reads.
        key = (research.item_id, document_id)
        self.document_cache.pop(key, None)
        document = research.documents[document_id]
        size = len(document.get("html") or "") * 2 + len(document.get("text") or "") * 2
        self.document_cache[key] = (research, size)
        while len(self.document_cache) > 8 or sum(value[1] for value in self.document_cache.values()) > 16_000_000:
            oldest = next(iter(self.document_cache))
            if oldest == key:
                break
            owner, _ = self.document_cache.pop(oldest)
            owner.documents.pop(oldest[1], None)

    def remember(self, item_id, offers, relevant):
        keys = set()
        for offer in offers:
            if not relevant(offer) or offer.get("price") is None or not offer.get("evidence", {}).get("price"):
                continue
            # A second query or another item must not turn one supplier card
            # into two newly discovered offers.
            seller = _supplier_name(offer) or str(offer.get("domain") or "").casefold()
            model = offer.get("attributes", {}).get("model")
            identity = _model(model) if model else _canonical_url(offer.get("url"))
            keys.add((seller, identity))
        self.offer_keys[item_id] = keys


def request_for_item(item):
    request = parse_request(item["description"], item["quantity"])
    request["product"] = item.get("title") or request["product"]
    if item.get("unit"):
        request["unit"] = item["unit"]
    explicit = item.get("requirements")
    request["requirements_authoritative"] = isinstance(explicit, list)
    if isinstance(explicit, list):
        parsed_keys = {r.get("key") for r in request.get("requirements", []) if isinstance(r, dict)}
        request["requirements"] = copy.deepcopy(explicit)
        present = {r.get("key") for r in explicit}
        request["missing_critical_fields"] = [x for x in request.get("missing_critical_fields", [])
                                              if x not in present and (not explicit or x != "поддерживаемая категория и её обязательные характеристики")]
        if not explicit and parsed_keys:
            request["missing_critical_fields"] = sorted(set(request["missing_critical_fields"]) | parsed_keys)
            request["requirements_notice"] = "Требования удалены при проверке позиций; они не будут восстановлены из исходного описания."
    if item.get("conditions"):
        request["conditions"] = copy.deepcopy(item["conditions"])
    request["item_id"] = item["id"]
    request["source"] = copy.deepcopy(item.get("source", {}))
    title = re.sub(r"\s+", " ", item.get("title") or request["product"]).strip()
    # Search text identifies the object. Contract addresses, warranties and the
    # whole pasted procurement document do not become search-query keywords.
    terms = title[:220]
    for requirement in request.get("requirements", []):
        if requirement.get("key") in {"format", "density_gsm", "sheets_per_pack", "channels", "bandwidth_mhz",
                                      "manufacturer", "brand", "model", "product_name", "article", "sku"}:
            value = requirement.get("value")
            if not isinstance(value, (list, dict)):
                if requirement.get("key") in {"manufacturer", "brand", "model", "product_name", "article", "sku"} and str(value or "").casefold() in terms.casefold():
                    continue
                terms += " " + str(value) + (" " + str(requirement.get("unit")) if requirement.get("unit") else "")
    request["search_terms"] = [terms[:300], title[:300]] if terms != title else [title[:300]]
    return request


def _number(value):
    return float(value.quantize(Decimal(".01")))


def aggregate_results(payload, items, results, budget=None, stop=None, progress=None):
    """Unknown item totals never become zero or a fabricated complete total."""
    output_items, offers, statuses, pages, sources = [], [], {}, [], []
    numeric_totals, estimates = [], []
    provisional_ids, provisional_prices, unit_assumptions = [], {}, []
    missing, missing_provisional = [], []
    totals = {"pages_used": 0, "queries_used": 0, "model_calls": 0, "reserved_tokens": 0,
              "reserved_micro_usd": 0, "settled_upper_micro_usd": 0}
    actual = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}
    actual_cost_known = True
    for item in items:
        iid = item["id"]
        result = copy.deepcopy(results.get(iid) or finalize(request_for_item(item), [], {"min_suppliers": 3}))
        annotate_target(result, enforce=(payload.get("search_policy") or {}).get("mode") == "target")
        result.update(id=iid, title=item["title"], quantity=item["quantity"], unit=item.get("unit"))
        output_items.append(result)
        calculation = result.get("calculation", {})
        total = calculation.get("total")
        if calculation.get("status") == "ready" and isinstance(total, (int, float)) and not isinstance(total, bool):
            numeric_totals.append(Decimal(str(total)))
        else:
            missing.append(iid)
        provisional = calculation.get("provisional") or {}
        selected_ids = ([str(o["id"]) for o in result.get("offers", []) if o.get("status") == "included"]
                        if calculation.get("status") == "ready" else provisional.get("offer_ids", []))
        provisional_ids.extend(iid + ":" + str(identifier) for identifier in selected_ids)
        provisional_prices.update({iid + ":" + str(key): value for key, value in provisional.get("normalized_prices", {}).items()})
        for assumption in provisional.get("unit_assumptions", []):
            if isinstance(assumption, dict):
                unit_assumptions.append({**assumption, "offer_id": iid + ":" + str(assumption.get("offer_id", ""))})
        estimate = total if total is not None else provisional.get("total")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool) and math.isfinite(estimate):
            estimates.append(Decimal(str(estimate)))
        else:
            missing_provisional.append(iid)
        for native_offer in result.get("offers", []):
            offer = copy.deepcopy(native_offer)
            original_id = str(offer["id"])
            offer["id"] = iid + ":" + original_id
            if offer.get("duplicate_of"):
                offer["duplicate_of"] = iid + ":" + str(offer["duplicate_of"])
            offer.update(item_id=iid, item_title=item["title"], original_offer_id=original_id)
            offers.append(offer)
        search = result.get("search", {})
        for row in search.get("source_statuses", []):
            statuses[row.get("domain") or row.get("name")] = copy.deepcopy(row)
        pages.extend(result.get("discovery_pages", []))
        b = search.get("budget", {})
        actual_cost_known = actual_cost_known and (not b.get("model_calls") or b.get("actual_cost_known") is True)
        for key in totals:
            totals[key] += b.get(key, 0) or 0
        for key in actual:
            actual[key] += b.get("actual_usage", {}).get(key, 0) or 0
        sources.extend(result.get("input_documents", []))
    all_ready = bool(items) and not missing
    known_subtotal = sum(numeric_totals, Decimal(0))
    estimate_subtotal = sum(estimates, Decimal(0))
    calculation = {"status": "ready" if all_ready else "insufficient_data",
                   "unit_price": None, "total": _number(known_subtotal) if all_ready else None,
                   "mean": None, "stddev": None, "cv": None,
                   "n": sum(x.get("calculation", {}).get("n", 0) for x in output_items),
                   "independent_suppliers": len({o.get("supplier") or o.get("domain") for o in offers if o.get("status") == "included"}),
                   "rule": "Сумма расчётов отдельных позиций закупки; характеристики и цены разных товаров не усредняются вместе.",
                   "reason": "Все позиции рассчитаны по выбранному профилю." if all_ready else "Есть позиции, которым нужны подтверждения; смотрите расчёты и причины по каждой позиции.",
                   "known_subtotal": _number(known_subtotal) if numeric_totals else None,
                   "missing_item_ids": missing,
                   "provisional": {"unit_price": None, "total": _number(estimate_subtotal) if estimates and not missing_provisional else None,
                                   "known_subtotal": _number(estimate_subtotal) if estimates else None,
                                   "missing_item_ids": missing_provisional,
                                   "n": sum((x.get("calculation", {}).get("provisional") or {}).get("n", x.get("calculation", {}).get("n", 0)) for x in output_items),
                                   "offer_ids": provisional_ids, "normalized_prices": provisional_prices, "unit_assumptions": unit_assumptions,
                                   "warnings": ["Предварительная стоимость требует проверки условий по позициям."] if not all_ready else [],
                                   "reason": "Сумма имеющихся предварительных расчётов; пропущенные позиции не считаются нулём."}}
    request = request_metadata(payload)
    request.update(product=payload.get("title") or "Закупка", category="procurement", unit=None, requirements=[],
                   items=[{k: copy.deepcopy(i.get(k)) for k in ("id", "title", "description", "quantity", "unit", "requirements", "conditions", "source", "warnings")} for i in items])
    policy = payload.get("search_policy") or {}
    if budget:
        totals["elapsed_seconds"] = round(budget.elapsed, 2)
        totals["model_calls"] = budget.model_calls
    totals["actual_usage"] = actual
    totals["actual_cost_known"] = actual_cost_known
    totals.update(max_seconds=policy.get("max_seconds"), max_offers=policy.get("max_offers"))
    code = stop or "running"
    search = {"mode": "live_agent", "started_at": budget.started_at if budget else utcnow(),
              "finished_at": utcnow() if stop else None, "stop": {"code": code, "label": STOP_LABELS.get(code, "Исследование продолжается"),
              "detail": "Выборка ограничена указанным условием поиска и доступностью источников; полнота рынка не заявляется."},
              "budget": totals, "source_statuses": list(statuses.values()), "completeness": "bounded_not_exhaustive",
              "search_policy": policy, "coverage": {"priced_offers": budget.count if budget else sum(o.get("price") is not None for o in offers),
                  "target_offers": 3 if policy.get("mode") == "target" else policy.get("max_offers"),
                  "items_total": len(items), "items_with_offers": sum(bool(r.get("offers")) for r in output_items),
                  "items_complete": sum(r["target_group"]["ready"] for r in output_items),
                  "target_matches": sum(r["target_group"]["count"] for r in output_items)}}
    result = {"request": request, "items": output_items, "offers": offers, "calculation": calculation, "search": search,
              "input_documents": copy.deepcopy(payload.get("input_documents", [])),
              "groups": {"items": [{"id": i["id"], "label": i["title"], "offer_ids": [o["id"] for o in offers if o["item_id"] == i["id"]]} for i in items]},
              "metrics": {"offers_total": len(offers), "accepted_count": sum(o.get("status") == "included" for o in offers),
                          "excluded_count": sum(o.get("status") == "excluded" for o in offers),
                          "uncertain_count": sum(o.get("status") == "uncertain" for o in offers),
                          "duplicates": sum(bool(o.get("duplicate_of")) for o in offers)}}
    if pages:
        result["discovery_pages"] = pages
    return annotate_result(result)


def refresh_product_identity_view(result):
    """Recompute derived arithmetic for saved hard mismatches, without I/O.

    Current offers retain manual confirmations/acceptances. Source baselines,
    saved timestamps, revisions and documents remain untouched; callers may
    cache this view by their existing saved-job revision outside the DB lock.
    """
    if not isinstance(result, dict):
        return result
    lines = result.get("items") or [result]
    if not any(product_identity_assessment(line.get("request", {}), offer)["status"] == "mismatch"
               for line in lines for offer in line.get("offers", []) if isinstance(offer, dict)):
        return result
    root_excluded = set(result.get("review", {}).get("excluded_ids", []))
    refreshed = {}
    target_mode = result.get("search", {}).get("search_policy", {}).get("mode") == "target"
    derived_keys = ("offers", "calculation", "sensitivity", "groups", "metrics", "target_group")
    for line in lines:
        request = line.get("request", {})
        prefix = str(line.get("id")) + ":" if result.get("items") else ""
        excluded = set(line.get("excluded_ids", [])) | set(line.get("review", {}).get("excluded_ids", []))
        excluded.update(value[len(prefix):] for value in root_excluded if not prefix or value.startswith(prefix))
        excluded.update(str(offer.get("id")) for offer in line.get("offers", []) if offer.get("manual_excluded"))
        policy = (line.get("base_result") or {}).get("policy") or line.get("policy") or {"min_suppliers": 3}
        evaluated = finalize(request, line.get("offers", []), policy, sorted(excluded))
        annotate_target(evaluated, enforce=target_mode)
        view = dict(line)
        view.update({key: evaluated[key] for key in derived_keys if key in evaluated})
        refreshed[line.get("id")] = view
    if not result.get("items"):
        return refreshed.get(result.get("id"), result)
    payload = dict(result.get("request", {}))
    items = payload.get("items") or [{key: line.get(key) for key in ("id", "title", "quantity", "unit")} for line in lines]
    payload.update(items=items, quantity=1, search_policy=result.get("search", {}).get("search_policy", {}),
                   input_documents=result.get("input_documents", []))
    aggregate = aggregate_results(payload, items, refreshed,
                                  stop=result.get("search", {}).get("stop", {}).get("code"))
    view = dict(result)
    view.update({key: aggregate[key] for key in ("items", "offers", "calculation", "groups", "metrics")})
    # Only derived coverage changes; original provider usage and capture dates
    # belong to the saved pass, not to this view-time interpretation.
    view["search"] = dict(result.get("search", {}))
    view["search"]["coverage"] = {**view["search"].get("coverage", {}),
                                  **{key: aggregate["search"]["coverage"][key]
                                     for key in ("items_complete", "target_matches")}}
    return view


class ItemStore:
    """Delegate evidence/quota writes, compose item results into the parent."""
    def __init__(self, owner, item):
        self.owner, self.item = owner, item

    def __getattr__(self, key):
        return getattr(self.owner.store, key)

    def save_result(self, job_id, result):
        self.owner.results[self.item["id"]] = result
        self.owner.publish()

    def set_search_progress(self, job_id, value):
        self.owner.progress[self.item["id"]] = copy.deepcopy(value)
        self.owner.publish(progress_only=True)

    def add_step(self, job_id, value):
        self.owner.store.add_step(job_id, {**value, "item_id": self.item["id"]})

    def discovery_urls(self, request, **kwargs):
        rows = self.owner.store.discovery_urls(request, **kwargs)
        if not isinstance(rows, list):
            return rows
        # Historical titles may reject an obviously different object before
        # its browser refresh consumes the line's budget. They never supply a
        # current price/specification or become evidence in the new result.
        # Unknown/synonymous titles remain useful discovery hints.
        return [row for row in rows if not isinstance(row, dict) or
                product_identity_assessment(request, {"title": row.get("title"),
                    "evidence": {"title": {"quote": row.get("title"), "url": row.get("url")}}})["status"] != "mismatch"]


class ProcurementResearch:
    def __init__(self, store, job_id, payload, cancel, event, provider=None, acquisition_factory=None):
        from .agent import PolicyResearch
        self.store, self.id, self.payload, self.cancel, self.event = store, job_id, payload, cancel, event
        self.items = copy.deepcopy(payload["items"])
        prior = payload.get("_resume_result") or {}
        self.results = {i["id"]: copy.deepcopy(i) for i in prior.get("items", []) if isinstance(i, dict) and i.get("id")}
        self.progress = {}
        self.budget = JobBudget(payload["search_policy"], cancel)
        self.researchers = []
        for item in self.items:
            child = {**payload, "description": item["description"], "quantity": item["quantity"],
                     "structured_request": request_for_item(item), "item_id": item["id"],
                     "_resume_item": self.results.get(item["id"]),
                     "limits": {**payload["limits"], "max_seconds": payload["search_policy"]["max_seconds"],
                                "max_pages": 10000, "max_queries": 10000, "max_model_calls": 10000}}
            def item_event(message, kind="info", title=item["title"]):
                self.event(str(title)[:80] + ": " + message, kind)
            acquire = acquisition_factory(item) if acquisition_factory else None
            self.researchers.append(PolicyResearch(ItemStore(self, item), job_id, child, cancel, item_event,
                                                  budget=self.budget, provider=provider, acquisition=acquire))
        # Seed facts, not prior-pass counters: every published pass reports only
        # its own usage, including before another line has had its first turn.
        for research in self.researchers:
            if research.continuation:
                self.results[research.item_id] = research.result()

    def publish(self, stop=None, progress_only=False):
        result = None
        if not progress_only:
            result = aggregate_results(self.payload, self.items, self.results, self.budget, stop, self.progress)
            self.store.save_result(self.id, result)
        sources = {}
        for value in self.progress.values():
            for source in value.get("sources", []):
                sources[source.get("url") or source.get("domain")] = source
        counters = {"pages_used": 0, "queries_used": 0, "model_calls": self.budget.model_calls}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}
        known_cost = True
        for research in self.researchers:
            counters["pages_used"] += research.pages
            counters["queries_used"] += research.queries
            for key in usage:
                usage[key] += research.actual_usage.get(key, 0)
            known_cost = known_cost and research.calls == research.reported_cost_calls
        counters.update(elapsed_seconds=round(self.budget.elapsed, 2), max_seconds=self.budget.policy["max_seconds"],
                        max_offers=self.budget.policy["max_offers"], actual_usage=usage, actual_cost_known=known_cost)
        groups = {r.item_id: r.current_target_group() for r in self.researchers}
        coverage = {"priced_offers": self.budget.count, "target_offers": self.budget.policy["max_offers"],
                    "items_total": len(self.items), "items_with_offers": sum(bool(v.get("offers")) for v in self.results.values()),
                    "items_complete": sum(g["ready"] for g in groups.values()),
                    "target_matches": sum(g["count"] for g in groups.values())}
        self.store.set_search_progress(self.id, {"phase": "complete" if stop else "research", "sources": list(sources.values()),
            "offers_count": sum(len(v.get("offers", [])) for v in self.results.values()), "priced_offers": self.budget.count,
            "items": [{"id": i["id"], "title": i["title"], "offers_count": len(self.results.get(i["id"], {}).get("offers", [])),
                       "target_group": groups.get(i["id"])} for i in self.items],
            "coverage": coverage, "budget": counters})
        return result

    def run(self):
        from .agent import BudgetStop
        active = self.researchers
        if self.budget.policy["mode"] == "target" and any(not r.current_target_group()["ready"] for r in active):
            active = [r for r in self.researchers if not r.current_target_group()["ready"]]
            for research in self.researchers:
                if research not in active:
                    research.stop_code, research.finished = "target_complete", True
        generators = [(r, r.stream()) for r in active]
        stop = "source_exhausted"
        self.event("Исследую позиции закупки по очереди; условие завершения действует на всю закупку.")
        try:
            while generators:
                current = []
                for research, gen in generators:
                    self.budget.check()
                    try:
                        next(gen)
                        self.results[research.item_id] = research.result()
                        if self.budget.policy["mode"] == "target" and research.current_target_group()["ready"]:
                            research.stop_code, research.finished = "target_complete", True
                            gen.close()
                        else:
                            current.append((research, gen))
                    except StopIteration:
                        self.results[research.item_id] = research.result()
                    self.publish(progress_only=self.budget.policy["mode"] != "target" and not any(r.continuation for r in self.researchers))
                    if self.budget.policy["mode"] == "target" and self.researchers and all(r.current_target_group()["ready"] for r in self.researchers):
                        raise BudgetStop("target_complete")
                generators = current
        except BudgetStop as exc:
            stop = str(exc)
        finally:
            for research in self.researchers:
                if stop != "source_exhausted" and not research.finished:
                    research.stop_code = stop
                self.results[research.item_id] = research.result()
            for _, gen in generators:
                gen.close()
        if stop == "source_exhausted":
            # A line handles BudgetStop itself and returns normally. Reaching
            # StopIteration therefore does not prove its frontier was drained.
            if self.budget.policy["mode"] == "target" and self.researchers and all(r.current_target_group()["ready"] for r in self.researchers):
                stop = "target_complete"
            else:
                codes = {r.stop_code for r in self.researchers}
                for code in ("cancelled", "time_budget", "global_budget", "cost_budget", "token_budget",
                             "context_budget", "model_budget", "page_budget", "offer_target",
                             "provider_usage_error", "provider_error", "provider_unavailable"):
                    if code in codes:
                        stop = code
                        break
        return self.publish(stop)


def review_procurement(result, excluded_ids, overrides=None):
    if overrides is not None or any(offer.get("manual_confirmations") for offer in result.get("offers", [])):
        from .review import rebuild_result
        if overrides is None:
            overrides = {offer["id"]: offer["manual_confirmations"] for offer in result.get("offers", []) if offer.get("manual_confirmations")}
        return rebuild_result(result, overrides, excluded_ids)
    payload = {"description": result["request"]["description"], "quantity": 1,
               "items": result["request"]["items"], "search_policy": result["search"].get("search_policy", {}),
               "input_documents": result.get("input_documents", [])}
    by_item = {}
    for item_result in result.get("items", []):
        iid = item_result["id"]
        base = item_result.get("base_result")
        if not base:
            by_item[iid] = item_result
            continue
        identifiers = [str(v).split(":", 1)[1] for v in excluded_ids if str(v).startswith(iid + ":")]
        updated = finalize(base["request"], base["offers"], base.get("policy"), identifiers)
        updated["search"] = copy.deepcopy(item_result.get("search", {}))
        for field in ("discovery_pages", "validation_notice"):
            if field in item_result:
                updated[field] = copy.deepcopy(item_result[field])
        by_item[iid] = updated
    updated = aggregate_results(payload, payload["items"], by_item, stop=result["search"]["stop"]["code"])
    updated["search"] = copy.deepcopy(result["search"])
    updated["review"] = {"excluded_ids": list(excluded_ids)}
    return updated


def revalidate_captured_job(store, job):
    """Rebuild derived facts from this job's immutable captures, without I/O.

    Caller must take a verified backup and use Store.revalidate_result so the
    entire previous result and revision remain auditable. Original form fields,
    captured documents and paid quotas are never changed here.
    """
    from .extraction import extract_product_offer
    from .selection import validate_candidate
    from .spec_ingest import prepare_specifications
    old = job.get("result") or {}
    original = old.get("base_result") or {}
    if old.get("items"):
        if job.get("status") == "running":
            raise ValueError("Дождитесь завершения исследования перед перепроверкой сохранённых источников.")
        items = copy.deepcopy(old.get("request", {}).get("items") or [])
        if not items:
            return None
        excluded = job.get("excluded_ids", old.get("review", {}).get("excluded_ids", []))
        by_item, used_ids = {}, set()
        old_by_id = {item["id"]: item for item in old["items"]}
        for item in items:
            iid = item["id"]
            prior = old_by_id.get(iid, {})
            baseline = prior.get("base_result") or {}
            line_request = copy.deepcopy(baseline.get("request") or prior.get("request") or request_for_item(item))
            previous = baseline.get("offers") or prior.get("offers", [])
            document_ids = store.extraction_document_ids(job["id"], iid)
            document_ids.update(o.get("document_id") for o in previous if o.get("document_id"))
            documents = [store.get_document(job["id"], did) for did in document_ids]
            documents = [doc for doc in documents if doc and doc.get("method") != "search_api"]
            used_ids.update(doc["document_id"] for doc in documents)
            observations = {}
            for document in documents:
                for old_offer in previous:
                    if old_offer.get("document_id") == document["document_id"]:
                        candidate = validate_candidate(old_offer, document, line_request)
                        if candidate:
                            observations[candidate["id"]] = candidate
                fresh = extract_product_offer(document, line_request)
                if fresh:
                    kept = observations.get(fresh["id"], {})
                    combined = {**kept, **fresh}
                    for key in ("price", "currency", "unit", "pack_size", "delivery"):
                        if fresh.get(key) is None and kept.get(key) is not None and not fresh.get("source_issues"):
                            combined[key] = kept[key]
                    combined["attributes"] = {**kept.get("attributes", {}), **fresh.get("attributes", {})}
                    combined["evidence"] = {**kept.get("evidence", {}), **fresh.get("evidence", {})}
                    observations[fresh["id"]] = combined
            from .seller_terms import enrich_offer
            for identifier, offer in list(observations.items()):
                primary = next((doc for doc in documents if doc["document_id"] == offer.get("document_id")), None)
                if primary:
                    observations[identifier] = enrich_offer(offer, line_request, primary, documents)
            line_excluded = [str(identifier).split(":", 1)[1] for identifier in excluded if str(identifier).startswith(iid + ":")]
            updated = finalize(line_request, list(observations.values()), baseline.get("policy") or {"min_suppliers": 3}, line_excluded)
            updated["search"] = copy.deepcopy(prior.get("search", {}))
            by_item[iid] = updated
        payload = {"description": old["request"]["description"], "quantity": 1, "items": items,
                   "input_documents": old.get("input_documents", []), "search_policy": old.get("search", {}).get("search_policy", {})}
        result = aggregate_results(payload, items, by_item, stop=old.get("search", {}).get("stop", {}).get("code", "source_exhausted"))
        result["search"] = copy.deepcopy(old.get("search", {}))
        result["review"] = copy.deepcopy(old.get("review", {}))
        counter = JobBudget(payload["search_policy"] or {"mode": "offers", "max_offers": 100, "max_seconds": 3600}, threading.Event())
        for line in result["items"]:
            req = line["request"]
            def relevant(offer, request=req):
                attrs = offer.get("attributes", {})
                return positive_product_identity(request, offer) and all(attrs.get(r["key"]) is None or _attribute_match(offer, r) is not False
                    for r in request.get("requirements", []) if isinstance(r, dict) and r.get("key"))
            counter.remember(line["id"], line["offers"], relevant)
        coverage = result["search"].setdefault("coverage", {})
        coverage.update(priced_offers=counter.count, items_total=len(items), items_with_offers=sum(bool(line["offers"]) for line in result["items"]))
        result["search"]["source_revalidation"] = {"at": utcnow(), "documents_reused": len(used_ids),
            "network_calls": 0, "model_calls": 0, "items_reparsed": 0, "items_reextracted": len(items)}
        result["validation_notice"] = "Перепроверены исходные страницы этого исследования. Цены не обновлялись, нового поиска и платных вызовов не было."
        if job.get("manual_overrides") or job.get("manual_acceptances"):
            from .review import rebuild_result
            result = rebuild_result(result, job.get("manual_overrides", {}), excluded, job.get("revision"), job.get("manual_acceptances", {}))
        return result
    request = original.get("request") or old.get("request")
    if not isinstance(request, dict):
        return None
    description = (job.get("request") or {}).get("description") or request.get("description", "")
    prepared = prepare_specifications(description, [])
    items = prepared.get("items", [])
    multi = len(items) > 1 and all(item.get("quantity") is not None for item in items)
    documents = [store.get_document(job["id"], row["id"]) for row in store.list_documents(job["id"])]
    documents = [doc for doc in documents if doc and doc.get("method") != "search_api"]
    previous = original.get("offers") or old.get("offers", [])
    excluded = job.get("excluded_ids", [])

    def rebuild(line_request):
        observations = {}
        for document in documents:
            for prior in previous:
                if prior.get("document_id") == document.get("document_id"):
                    validated = validate_candidate(prior, document, line_request)
                    if validated:
                        observations[validated["id"]] = validated
            fresh = extract_product_offer(document, line_request)
            if fresh:
                kept = observations.get(fresh["id"], {})
                merged = {**kept, **fresh}
                for key in ("price", "currency", "unit", "pack_size", "delivery"):
                    if fresh.get(key) is None and kept.get(key) is not None:
                        merged[key] = kept[key]
                merged["attributes"] = {**kept.get("attributes", {}), **fresh.get("attributes", {})}
                merged["evidence"] = {**kept.get("evidence", {}), **fresh.get("evidence", {})}
                observations[fresh["id"]] = merged
        return finalize(line_request, list(observations.values()), original.get("policy") or {"min_suppliers": 3}, excluded)

    if multi:
        payload = {"description": description, "quantity": 1, "items": items, "input_documents": prepared["documents"],
                   "search_policy": old.get("search", {}).get("search_policy", {"mode": "offers", "max_offers": 100, "max_seconds": 3600})}
        result = aggregate_results(payload, items, {i["id"]: rebuild(request_for_item(i)) for i in items},
                                   stop=old.get("search", {}).get("stop", {}).get("code", "source_exhausted"))
    else:
        result = rebuild(copy.deepcopy(request))
    result["search"] = copy.deepcopy(old.get("search", {}))
    result["search"]["source_revalidation"] = {"at": utcnow(), "documents_reused": len(documents),
                                               "network_calls": 0, "model_calls": 0, "items_reparsed": len(items) if multi else 1}
    result["validation_notice"] = "Перепроверено по сохранённым страницам исходного исследования. Нового поиска и обновления рыночных цен не было."
    if job.get("manual_overrides"):
        from .review import rebuild_result
        result = rebuild_result(result, job["manual_overrides"], excluded, job.get("revision"))
    return result

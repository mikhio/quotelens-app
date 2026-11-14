"""Typed manual facts and replay; captured source facts are never rewritten."""
from __future__ import annotations

from copy import deepcopy
import math
import re

from .engine import LABELS, _field_confirmed, _has_evidence, _requirement_match, _attribute_match, _unit, positive_product_identity, condition_specs
from .selection import finalize


class RevisionConflict(ValueError):
    def __init__(self, current_revision):
        super().__init__("Результат уже изменён. Перечитайте актуальную ревизию.")
        self.current_revision = current_revision


def field_value(offer, key):
    if key.startswith("attributes."):
        return (offer.get("attributes") or {}).get(key[11:])
    if key.startswith("conditions."):
        return (offer.get("conditions") or {}).get(key[11:])
    return offer.get(key)


def source_lines(result):
    """Resolve flat IDs explicitly; never split native IDs containing colons."""
    for line in result.get("items") or [result]:
        baseline = line.get("base_result") or line
        prefix = str(line["id"]) + ":" if result.get("items") else ""
        for offer in baseline.get("offers", []):
            if isinstance(offer, dict) and isinstance(offer.get("id"), str):
                yield prefix + offer["id"], line, baseline, offer


def field_specs(request):
    specs = {
        "price": {"label": "Цена", "type": "number", "positive": True},
        "currency": {"label": "Валюта", "type": "select", "options": ["RUB", "USD", "EUR"]},
        "unit": {"label": "Единица цены", "type": "string"},
        "pack_size": {"label": "Единиц в упаковке", "type": "number", "positive": True, "integer": True},
        "tax": {"label": "НДС", "type": "select", "options": ["included", "excluded", "unknown"]},
        "supplier": {"label": "Поставщик", "type": "string"},
        "delivery": {"label": "Доставка", "type": "string"},
    }
    for row in request.get("requirements") or []:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(row.get("key") or "")):
            continue
        expected = row.get("value")
        scalar = expected[0] if isinstance(expected, list) and expected else expected
        numeric = isinstance(scalar, (int, float)) and not isinstance(scalar, bool)
        specs["attributes." + row["key"]] = {"label": row.get("label") or LABELS.get(row["key"], row["key"]),
            "type": "number" if numeric else "string", "expected": deepcopy(expected),
            "operator": row.get("op", "eq"), "unit": row.get("unit")}
    conditions = request.get("conditions") or []
    for index, key, row in condition_specs(request):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            continue
        expected = row.get("value", row.get("text") or row.get("quote")) if isinstance(row, dict) else str(row)
        numeric = isinstance(expected, (int, float)) and not isinstance(expected, bool)
        specs["conditions." + key] = {"label": row.get("label") or key if isinstance(row, dict) else "Условие ТЗ",
            "type": "number" if numeric else "string", "expected": expected,
            "operator": row.get("op", "eq") if isinstance(row, dict) else "eq"}
    return specs


def validate_fields(request, fields):
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 50:
        raise ValueError("Укажите от 1 до 50 уточняемых полей.")
    specs, clean = field_specs(request), {}
    for key, value in fields.items():
        spec = specs.get(key)
        if spec is None:
            raise ValueError("Это поле нельзя уточнить: " + str(key)[:100])
        if spec["type"] == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e15:
                raise ValueError(spec["label"] + ": введите конечное число.")
            if spec.get("positive") and value <= 0 or spec.get("integer") and value != int(value):
                raise ValueError(spec["label"] + ": требуется положительное целое число." if spec.get("integer") else spec["label"] + ": требуется положительное число.")
        elif not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
            raise ValueError(spec["label"] + ": введите текст до 1000 символов.")
        else:
            value = value.strip()
        if spec.get("options") and value not in spec["options"]:
            raise ValueError(spec["label"] + ": выбрано недопустимое значение.")
        if key == "unit" and (len(value) > 40 or _unit(value) not in {"piece", "pack", "box", "sheet", "m2", "hour", "service"} and _unit(value) != _unit(request.get("unit"))):
            raise ValueError("Выберите сопоставимую единицу цены.")
        clean[key] = value
    return clean


def validate_acceptances(request, fields):
    """Accept a procurement deviation without changing the observed fact."""
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 50:
        raise ValueError("Укажите отклонение, которое принимаете.")
    allowed = {key for key in field_specs(request) if key.startswith(("attributes.", "conditions."))}
    if any(key not in allowed or not isinstance(value, bool) for key, value in fields.items()):
        raise ValueError("Можно принять только конкретное требование или условие ТЗ.")
    return dict(fields)


def selected_review_field(request, fields, *, attributes_only=False):
    """The one-click actions select a field, never submit a replacement fact."""
    if not isinstance(fields, dict) or len(fields) != 1:
        raise ValueError("Выберите одно поле.")
    key, selected = next(iter(fields.items()))
    if (key not in field_specs(request) or selected is not True
            or attributes_only and not key.startswith("attributes.")):
        raise ValueError("Выберите запрошенную характеристику.")
    return key


def shown_suggestion(request, offer, key):
    """Read the first stored hint and retain only its public source audit."""
    spec = field_specs(request).get(key)
    if spec is None or not key.startswith("attributes."):
        raise ValueError("Для этого поля нет подсказки источника.")
    stored = offer.get("attribute_hints")
    hints = stored.get(key[11:]) if isinstance(stored, dict) else None
    hint = hints[0] if isinstance(hints, list) and hints else None
    if (not isinstance(hint, dict) or not isinstance(hint.get("quote"), str) or not hint["quote"].strip()
            or not any(isinstance(hint.get(name), str) and hint[name].strip() for name in ("url", "document_id"))):
        raise ValueError("Для этого поля нет подсказки источника.")
    value = validate_fields(request, {key: hint.get("value")})[key]
    # A confirmation of a suggested number does not supply an absent source
    # unit. Ranges are rejected by validate_fields rather than flattened.
    if spec["type"] == "number" and spec.get("unit"):
        proof = (offer.get("evidence") or {}).get(key) or {}
        source_unit = hint.get("normalized_unit") or hint.get("source_unit") or hint.get("unit")
        if (hint.get("unit_reason") == "source_unit_not_stated" or proof.get("unit_reason") == "source_unit_not_stated"
                or not isinstance(source_unit, str) or _unit(source_unit) != _unit(spec["unit"])):
            raise ValueError("Сначала уточните единицу характеристики.")
    text_fields = ("quote", "url", "document_id", "source_hash", "retrieved_at", "basis", "locator", "reason",
                   "attribute", "raw_value", "raw_label", "visibility", "tab_id", "tab_label", "related_field",
                   "unit", "source_unit", "normalized_unit", "unit_reason", "unit_basis", "normalization")
    source = {name: deepcopy(hint[name]) for name in text_fields if isinstance(hint.get(name), str)}
    source.update({name: hint[name] for name in ("inferred", "confirmed_for_product") if isinstance(hint.get(name), bool)})
    source["value"] = deepcopy(value)
    return value, source


def apply_confirmations(offer, confirmations):
    updated = deepcopy(offer)
    updated.pop("review_fields", None)
    updated.pop("manual_review", None)
    updated.pop("manual_confirmations", None)
    if not confirmations:
        return updated
    updated["manual_confirmations"] = deepcopy(confirmations)
    for key, confirmation in confirmations.items():
        value = deepcopy(confirmation["value"])
        if key.startswith("attributes.") or key.startswith("conditions."):
            namespace, name = key.split(".", 1)
            updated.setdefault(namespace, {})[name] = value
        else:
            updated[key] = value
    return updated


def replay_line(line, overrides, excluded_ids, prefix="", acceptances=None):
    baseline = deepcopy(line.get("base_result") or {"request": line["request"], "offers": line.get("offers", []), "policy": {}})
    request = baseline["request"]
    offers = [apply_confirmations(offer, overrides.get(prefix + str(offer.get("id")), {})) for offer in baseline["offers"]]
    for offer in offers:
        offer.pop("manual_acceptances", None)
        accepted = (acceptances or {}).get(prefix + str(offer.get("id")), {})
        if accepted:
            offer["manual_acceptances"] = deepcopy(accepted)
        manual = offer.get("manual_confirmations", {})
        assessment = offer.get("relevance") or {}
        # A user must correct concrete contradictory facts before a previous
        # functional assessment can change. Variant/source conflicts persist.
        if manual and assessment.get("verdict") in {"uncertain", "mismatch"} and assessment.get("basis") != "source_variant_comparison" and not offer.get("source_issues"):
            requirements = [r for r in request.get("requirements", []) if isinstance(r, dict) and r.get("key")]
            original = next((o for o in baseline["offers"] if o.get("id") == offer.get("id")), {})
            contradictions = ["attributes." + r["key"] for r in requirements if _attribute_match(original, r) is False]
            corrected = bool(requirements) and any(key.startswith("attributes.") for key in manual) and all(key in manual for key in contradictions)
            if corrected and positive_product_identity(request, {**offer, "relevance": {}}) and all(_field_confirmed(offer, "attributes." + r["key"]) and _attribute_match(offer, r) is True for r in requirements):
                offer.pop("relevance", None)
    native_excluded = [offer["id"] for offer in offers if prefix + offer["id"] in excluded_ids]
    result = finalize(request, offers, baseline.get("policy"), native_excluded)
    # finalize's replay inputs are the active calculation facts; persist the
    # genuine source baseline separately so reset cannot retain manual values.
    result["base_result"] = baseline
    for key in ("search", "discovery_pages", "validation_notice", "input_documents"):
        if key in line:
            result[key] = deepcopy(line[key])
    result.setdefault("metrics", {})["manual_confirmed_fields"] = sum(len(o.get("manual_confirmations", {})) for o in offers)
    critical = ["price", "currency", "unit", "tax"] + ["attributes." + row["key"] for row in request.get("requirements", []) if isinstance(row, dict) and row.get("key")]
    verified = sum(_has_evidence(offer, key) for offer in offers for key in critical)
    required_count = len(offers) * len(critical)
    result["metrics"].update(evidence_fields_verified=verified, evidence_fields_required=required_count,
                             evidence_coverage=round(verified / required_count, 4) if required_count else None)
    return result


def rebuild_result(result, overrides, excluded_ids, revision=None, acceptances=None):
    if result.get("items"):
        from .procurement import aggregate_results
        payload = deepcopy(result["request"])
        payload.update(quantity=1, title=result["request"].get("title") or result["request"].get("product"),
                       items=result["request"]["items"], search_policy=result.get("search", {}).get("search_policy", {}),
                       input_documents=result.get("input_documents", []))
        by_item = {line["id"]: replay_line(line, overrides, excluded_ids, str(line["id"]) + ":", acceptances) for line in result["items"]}
        updated = aggregate_results(payload, payload["items"], by_item, stop=result.get("search", {}).get("stop", {}).get("code"))
        updated["request"] = deepcopy(result["request"])
        updated["metrics"]["manual_confirmed_fields"] = sum(line.get("metrics", {}).get("manual_confirmed_fields", 0) for line in by_item.values())
        for key in ("search", "discovery_pages", "validation_notice", "input_documents"):
            if key in result:
                updated[key] = deepcopy(result[key])
    else:
        updated = replay_line(result, overrides, excluded_ids, acceptances=acceptances)
    updated["review"] = {"excluded_ids": sorted(excluded_ids), "revision": revision if revision is not None else result.get("review", {}).get("revision", 0)}
    return annotate_review_fields(updated)


def annotate_review_fields(result):
    originals = {identifier: source for identifier, _, _, source in source_lines(result)}
    flat = {}
    for line in result.get("items") or [result]:
        request = line.get("request", {})
        prefix = str(line["id"]) + ":" if result.get("items") else ""
        specs = field_specs(request)
        for offer in line.get("offers", []):
            identifier = prefix + str(offer.get("id", ""))
            source = originals.get(identifier, offer)
            confirmations = offer.get("manual_confirmations") or {}
            accepted = offer.get("manual_acceptances") or {}
            fields = []
            for key, spec in specs.items():
                value = field_value(offer, key)
                # Optional empty commercial fields are available for editing,
                # but do not become fictitious blockers for the calculation.
                optional = key in {"pack_size", "delivery"} or key == "supplier" and bool(value)
                if key == "pack_size" and _unit(offer.get("unit")) in {"pack", "box"} and _unit(offer.get("unit")) != _unit(request.get("unit")):
                    optional = False
                status = "manual_confirmed" if key in confirmations else "source_confirmed" if _has_evidence(offer, key) else "unconfirmed"
                expected = spec.get("expected")
                verdict = None
                if key == "currency" and value and value != "RUB" or key == "tax" and value == "excluded":
                    status = "mismatch"
                elif key == "tax" and value in {None, "unknown"}:
                    status = "unconfirmed"
                if expected is not None and value is not None:
                    verdict = _attribute_match(offer, {"key": key[11:], "value": expected, "op": spec.get("operator", "eq"), "unit": spec.get("unit")}) if key.startswith("attributes.") else _requirement_match(value, expected, spec.get("operator", "eq"), key.split(".", 1)[-1])
                    if verdict is False:
                        status = "mismatch"
                    elif verdict is None:
                        status = "unconfirmed"
                row = {k: deepcopy(v) for k, v in spec.items() if k not in {"positive", "integer"}}
                row.update(key=key, value=deepcopy(value), original_value=deepcopy(field_value(source, key)), status=status, required=not optional)
                proof = (offer.get("evidence") or {}).get(key) or {}
                for name in ("source_unit", "normalized_unit", "unit_reason", "visibility", "tab_label"):
                    if name in proof:
                        row[name] = deepcopy(proof[name])
                if key in confirmations:
                    row["confirmation"] = deepcopy(confirmations[key])
                elif value is None and key.startswith("attributes."):
                    try:
                        _, row["confirmable_suggestion"] = shown_suggestion(request, offer, key)
                    except ValueError:
                        pass
                if key in accepted:
                    row["acceptance"] = deepcopy(accepted[key])
                if status == "unconfirmed":
                    row["reason"] = "В этой строке источника не указана единица; значение извлечено." if proof.get("unit_reason") == "source_unit_not_stated" and value is not None else "Диапазон источника частично пересекается с ТЗ; нужно подтверждение." if isinstance(value, list) and value else "Значение требует подтверждения для этой закупки." if value is not None else "Извлечённого значения нет; проверьте источник."
                elif status == "mismatch":
                    row["reason"] = "Значение не соответствует требованию."
                if key == "attributes.manufacturer" and value is None and (offer.get("attributes") or {}).get("brand"):
                    row["reason"] = "Указан бренд «" + str(offer["attributes"]["brand"]) + "»; производитель отдельно не извлечён."
                if key == "attributes.manufacturer" and value is not None and verdict is None:
                    row["reason"] = "Строка производителя требует проверки: значение может обозначать страну или диапазон вариантов."
                fields.append(row)
            offer["review_fields"] = fields
            if confirmations:
                latest = max(confirmations.values(), key=lambda c: c.get("revision", 0))
                offer["manual_review"] = {"fields": {key: deepcopy(value["value"]) for key, value in confirmations.items()},
                    "reason": latest.get("reason"), "revision": latest.get("revision"), "at": latest.get("at")}
            else:
                offer.pop("manual_review", None)
            offer["review_blockers"] = list(dict.fromkeys([*offer.get("source_issues", []),
                *([str(offer["relevance"].get("reason") or "Соответствие товару требует проверки.")] if (offer.get("relevance") or {}).get("verdict") in {"uncertain", "mismatch"} else []),
                *(["Уточните требования к позиции в запросе: " + ", ".join(map(str, request["missing_critical_fields"]))] if request.get("missing_critical_fields") else [])]))
            if not positive_product_identity(request, offer):
                offer["review_blockers"].append("Соответствие предмету закупки не установлено; проверьте товар и требования позиции.")
            flat[identifier] = offer
    if result.get("items"):
        for offer in result.get("offers", []):
            source = flat.get(offer["id"], {})
            for key in ("review_fields", "manual_review", "manual_confirmations", "manual_acceptances", "review_blockers"):
                if key in source:
                    offer[key] = deepcopy(source[key])
                else:
                    offer.pop(key, None)
    return result

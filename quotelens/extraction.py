"""Immediate deterministic extraction from a captured primary product page."""
from __future__ import annotations

import re

from .engine import _extract_model, _model, _decimal
from .price_context import (primary_price_contexts, primary_product_context, source_variant_issues,
                            requested_variant_mass, primary_requirement_observations, primary_product_images)
from .selection import _currency, _space, validate_candidate, printer_requirement_observations, quality_requirement_observations, primary_cms_financial_facts
from .source_documents import classify_page


_RELATED = re.compile(r"Рекомендуем(?:ые|\s+также)|Вас\s+также\s+может|Похожие\s+товары|Другие\s+товары\s+из\s+категории|С\s+этим\s+товаром\s+покупают|Сопутствующие\s+товары|related\s+products|you\s+may\s+also\s+like", re.I)
_MODEL = re.compile(r"(?<!\w)(?:[a-zа-яё]+[-_/][a-zа-яё0-9]*\d[a-zа-яё0-9]*(?:[-_/.][a-zа-яё0-9]+)*|[a-z]+\d[a-z0-9]*(?:[-_/.][a-zа-яё0-9]+)*|(?-i:[A-ZА-ЯЁ]{2,})\s+\d{2,}[a-zа-яё]*(?:[-_/.][a-zа-яё0-9]+)*)(?:[\s-]+(?:pro|plus|mini|max|ultra|hs|про)(?:[\s-]*\d+)?(?:[\s-]*hs)?)?(?!\w)", re.I)


def extract_product_offer(document, request, *, fact_documents=()):
    """Return a validated observation or None; missing source facts stay None.

    This function has no network/model/storage side effects. Confirmed lists
    never become offers. A specific heading can establish an incomplete detail;
    a verified primary DOM purchase block also supports otherwise unknown pages.
    """
    if not isinstance(document, dict) or not isinstance(request, dict) or not isinstance(document.get("text"), str):
        return None
    if document.get("source_role") == "seller_terms":
        return None
    shape = classify_page(document)
    if shape["kind"] in {"listing", "article"} or document.get("page_kind") in {"listing", "article"} and shape["kind"] != "product":
        return None
    title = _space(document.get("primary_title") or document.get("title"))
    if not title or len(title) > 300:
        return None
    text = _space(document["text"])
    start = text.find(title)
    if start < 0:
        return None
    blocks = primary_price_contexts(document)
    product_context = primary_product_context(document)
    focus = product_context["text"] if product_context else text[start:]
    related = _RELATED.search(focus) if not product_context else None
    if related:
        focus = focus[:related.start()]
    spec_focus = product_context["text"] if product_context else focus
    paper_product = request.get("category") == "paper" or bool(re.search(r"\bбумаг[аи]\b", title, re.I))
    model = _extract_model(title)
    if not model and not paper_product:
        match = _MODEL.search(title)
        model = match.group() if match else None
    if shape["kind"] != "product" and not (blocks and (model or product_context and product_context.get("strong_identity"))):
        return None
    candidate = {"title": title, "price": None, "tax": "unknown", "attributes": {}, "evidence": {}}

    def add(field, value, quote):
        if field.startswith("attributes."):
            candidate["attributes"][field[11:]] = value
        else:
            candidate[field] = value
        candidate["evidence"][field] = {"quote": quote}

    if model and _model(model) in _model(title):
        add("attributes.model", model, model)
    # A conflict between different primary price blocks is not resolved by
    # choosing the cheapest amount or trusting invisible schema metadata.
    source_issues = source_variant_issues(document)
    target_mass = requested_variant_mass(request)
    variants = {(block["variant"]["sku"], block["variant"]["packing"]) for block in blocks if block.get("variant")}
    if len(variants) > 1 and target_mass:
        exact = [block for block in blocks if block.get("variant", {}).get("mass_mcg") is not None and _decimal(block["variant"]["mass_mcg"]) == target_mass[0]]
        if exact:
            blocks = exact
            variants = {(block["variant"]["sku"], block["variant"]["packing"]) for block in blocks}
    if len(variants) > 1:
        source_issues.append("В основной таблице несколько артикулов или фасовок; однозначная строка для позиции не установлена.")
    if blocks and not source_issues and len({(block["amount"], block.get("currency") or _currency(block["quote"])) for block in blocks}) == 1:
        block = blocks[0]
        add("price", float(block["amount"]), block["quote"])
        currency = block.get("currency") or _currency(block["quote"])
        if currency:
            add("currency", currency, block.get("currency_quote") or block["quote"])
        # These short visible clauses describe the selected buy block. Stock
        # counts such as "в наличии 15 шт." never establish a sale unit.
        tax = block.get("tax_quote")
        if tax:
            add("tax", "excluded" if re.search(r"без|не\s*включ|excl", tax, re.I) else "included", tax)
        unit = re.search(r"(?:за\s+(?:1\s*)?(?:штук[ауи]?|шт\.?|пачк[ау]|упаковк[ау]|лист|час)|(?:₽|руб\.?|RUB)\s*/\s*(?:1\s*)?(?:шт\.?|пачк[ау]|упаковк[ау]|лист|час))", block["context"], re.I)
        if unit:
            quote = unit.group()
            sale_unit = "шт" if re.search(r"шт", quote, re.I) else "пачка" if re.search(r"пачк|упаковк", quote, re.I) else "лист" if re.search(r"лист", quote, re.I) else "час"
            add("unit", sale_unit, quote)
    elif blocks and not source_issues:
        source_issues.append("В основном блоке источника несколько разных цен или фасовок; однозначная цена для позиции не установлена.")
    patterns = {
        "channels": r"(?:(?:кол(?:ичество|[.-]?во)|число)\s+(?:аналоговых\s+)?каналов|аналоговые\s+каналы|number\s+of\s+(?:analog\s+)?channels)\s*[:=]?\s*(\d+)",
        "bandwidth_mhz": r"(?:полос[аы]\s+пропускания|bandwidth)\s*(?:,\s*(?:[МM]Гц|MHz)\s*[:=]?\s*(?:до\s+)?(\d+(?:[.,]\d+)?)(?![\d.,])|[:=]?\s*(?:до\s+)?(\d+(?:[.,]\d+)?)\s*(?:[МM]Гц|MHz)\b)",
    }
    for key, pattern in patterns.items():
        def observations(source, row=False):
            result = []
            for match in re.finditer(pattern, source, re.I):
                if key == "channels" and (row and source[:match.start()].strip() or re.match(
                        r"\s*(?:[МMГGкK]?(?:Гц|Hz|выб/|Sa/)|[мm]?[ВV]\b|МОм|MΩ)", source[match.end():], re.I)):
                    continue
                if key == "bandwidth_mhz" and re.search(r"огранич\w*\s*$|limit\w*\s*$", source[max(0, match.start() - 40):match.start()], re.I):
                    continue
                if re.search(r"\d+\s*(?:или|or)\s*$", source[max(0, match.start() - 30):match.start()], re.I) or re.match(r"\s*(?:или|or)\s*\d", source[match.end():], re.I):
                    return None
                result.append((next(group for group in match.groups() if group is not None), match.group()))
            return result

        # A labelled row in the primary technical table binds a value to its
        # attribute. Free prose can discuss sampler channel counts or filters.
        row_facts = [observations(row, row=True) for row in product_context["rows"] if len(row) <= 1200] if product_context else []
        if any(row is None for row in row_facts):
            continue
        facts = [fact for row in row_facts for fact in row]
        if not facts:
            facts = observations(spec_focus)
        if facts is None:
            continue
        if key == "channels" and not facts:
            matches = list(re.finditer(r"(?<!\d)(\d+)\s*канал[ао]?(?!\w)", title, re.I))
            facts = [] if re.search(r"\d+\s*(?:или|or)\s*\d+\s*канал", title, re.I) else [(match[1], match.group()) for match in matches]
        # Several different values in one description are ambiguous; e.g. an
        # optional bandwidth upgrade must not replace the observed base model.
        if facts and len({float(value.replace(",", ".")) for value, _ in facts}) == 1:
            value, quote = facts[0]
            add("attributes." + key, float(value.replace(",", ".")), quote)
    if paper_product:
        # A labelled catalogue sale unit belongs to this primary product.
        # Counts such as «в упаковке 5» do not establish the unit of its price.
        if candidate.get("price") is not None and not candidate.get("unit") and product_context:
            sale_units = list(re.finditer(r"\bЕдиница\s+измерения\s*[:=—-]?\s*(пач(?:ка|ки|ку)?\.?)(?!\w)", spec_focus, re.I))
            if sale_units:
                add("unit", "пачка", sale_units[0].group())
        paper_patterns = {
            "format": r"(?<![a-zа-яё0-9])([aа][0-6])(?!\d)",
            "density_gsm": r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:г\s*/\s*(?:м[²2]|кв\.?\s*м)|gsm)",
            "sheets_per_pack": r"(?:количество\s+листов(?:\s+в\s+(?:пачке|упаковке))?\s*[:=]?\s*(\d+)|(?:пачк|упаковк)\w*\s+по\s+(\d+)\s*лист)",
        }
        for key, pattern in paper_patterns.items():
            facts = [(next(value for value in m.groups() if value is not None), m.group())
                     for m in re.finditer(pattern, spec_focus, re.I)]
            # A bare value following a written range is not its actual density.
            if key == "density_gsm":
                facts = [(value, quote) for value, quote in facts
                         if not re.search(r"\d\s*[-–—±]\s*" + re.escape(value), spec_focus)]
            values = {value.casefold().replace("а", "a") if key == "format" else float(value.replace(",", "."))
                      for value, _ in facts}
            if len(values) == 1:
                value, quote = facts[0]
                add("attributes." + key, "A" + value[-1] if key == "format" else float(value.replace(",", ".")), quote)
    observation_request = {**request, "requirements": [*request.get("requirements", []),
        *([] if any(r.get("key") == "brand" for r in request.get("requirements", [])) else [{"key": "brand", "label": "Бренд", "value": "", "op": "eq"}])]}
    observations = quality_requirement_observations(document, observation_request)
    aliases = printer_requirement_observations(document, observation_request, primary_text=spec_focus)
    observations.update({key: fact for key, fact in aliases.items() if key not in observations})
    for key, observation in observations.items():
        add("attributes." + key, observation["value"], observation["quote"])
    from .attribute_hints import explicit_paper_color
    color = explicit_paper_color(document)
    if color and not candidate["attributes"].get("color"):
        add("attributes.color", color["value"], color["quote"])
    images = primary_product_images(document)
    if images:
        candidate["image_url"] = images[0]["url"]
        candidate["evidence"]["image_url"] = images[0]
    for field, fact in primary_cms_financial_facts(document).items():
        if candidate.get(field) is None or field == "tax" and candidate.get(field) == "unknown":
            candidate[field] = fact["value"]
            candidate["evidence"][field] = {key: value for key, value in fact.items() if key != "value"}
    offer = validate_candidate(candidate, document, request, fact_documents=fact_documents)
    if offer:
        from .attribute_hints import contextual_attribute_hints
        hints = contextual_attribute_hints(document, request, offer.get("attributes", {}), offer.get("evidence", {}))
        if hints:
            offer["attribute_hints"] = hints
        offer["validation"]["extraction"] = "deterministic_primary_product"
        availability = re.search(r"цен[ауы]\s*(?:по\s+запросу|уточня[йи]те)|уточня[йи]те\s+цен[уы]", spec_focus, re.I)
        offer["validation"]["price_state"] = "confirmed" if offer.get("price") is not None else "on_request" if availability else "not_extracted"
        if availability:
            offer["evidence"]["price_availability"] = {"quote": availability.group(), "url": document.get("url"), "document_id": document.get("document_id"), "source_hash": document.get("sha256"), "retrieved_at": document.get("retrieved_at")}
        if source_issues:
            offer["source_issues"] = list(dict.fromkeys((offer.get("source_issues") or []) + source_issues))
            offer["validation"]["price_reason"] = " ".join(offer["source_issues"])
        # A product's own pack specification permits an explicitly conditional
        # estimate. It does not prove the sale unit and never changes raw unit.
        packaging = offer.get("evidence", {}).get("attributes.sheets_per_pack")
        if (offer.get("price") is not None and not offer.get("unit") and isinstance(packaging, dict)
                and re.search(r"(?:в\s+)?пачк|упаковк", packaging.get("quote", ""), re.I)
                and offer.get("attributes", {}).get("sheets_per_pack")):
            offer["provisional_unit"] = {"unit": "пачка", **packaging,
                                         "basis": "single_primary_product_pack",
                                         "assumption": "Предполагается цена за одну указанную пачку; явное подтверждение единицы продажи отсутствует."}
    return offer

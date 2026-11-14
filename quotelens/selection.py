"""Evidence validation and reproducible selection. No model or network calls."""
from __future__ import annotations

from copy import deepcopy
import ast
import html as html_module
from decimal import Decimal, InvalidOperation
import hashlib
import re
from urllib.parse import urlsplit, urljoin

from .engine import (analyze_offers, parse_request, provisional_calculation, _canonical_url, _model,
                     _supplier_name, _unit, _verified_identity, _requirement_match,
                     _manual_acceptance, _condition_checks, positive_product_identity, _printer_object_kind,
                     _named_printer_models, _printer_models_match, _NAMED_PRINTER_MODEL, _material_entries)
from .source_documents import classify_page, _ShapeParser
from .supplier import supplier_details
from .price_context import (primary_price_contexts, primary_product_context, source_variant_issues,
                            requested_variant_mass, primary_requirement_observations, primary_product_images,
                            primary_attribute_rows, _normal_observation, _measure_unit, _attribute_concept)


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _contains(value, text):
    return bool(_space(value)) and _space(value) in _space(text)


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = Decimal(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        return value if value.is_finite() else None
    except (ValueError, InvalidOperation, TypeError):
        return None


_NUMBER = r"(?<![\w\d])(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?"
_MONEY = r"(?:₽|р\s*уб(?:\.|лей|ля)?|RUB\b|USD\b|EUR\b|\$|€|р\.)"


def _numbers(quote):
    # Normalize Unicode spaces for matching, preserving the original quotation.
    return [_decimal(value) for value in re.findall(_NUMBER, _space(quote))]


def _value_backed(value, quote):
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_value_backed(item, quote) for item in value)
    number = _decimal(value)
    if number is not None:
        return number in _numbers(quote)
    return isinstance(value, str) and bool(value.strip()) and _space(value).casefold() in _space(quote).casefold()


def _unit_pattern(unit):
    patterns = {"piece": r"(?<![a-zа-яё])шт(?:\.|\b)|\bштук\w*|\bpieces?\b",
                "pack": r"\bпач\w*|\bупаков\w*|\bpack\b|\bуп\.",
                "box": r"\bкороб\w*|\bbox\b", "sheet": r"\bлист\w*|\bsheets?\b",
                "m2": r"м\s*[²2]|кв\.?\s*м|\bm2\b", "hour": r"\bчас\w*|\bhours?\b",
                "service": r"\bуслуг\w*|\bservice\b"}
    return patterns.get(_unit(unit), re.escape(_space(unit)))


def _unit_backed(unit, quote):
    return isinstance(unit, str) and bool(unit.strip()) and bool(re.search(_unit_pattern(unit), quote, re.I))


def _sale_unit_backed(unit, quote):
    """An inventory count or a dimension is not the price's sale unit."""
    if not isinstance(unit, str) or not unit.strip():
        return False
    unit_pattern = "(?:" + _unit_pattern(unit) + ")"
    single = r"(?:1(?:[.,]0+)?\s*)?"
    price_unit = (r"(?:\bза\s*|\bper\s+)" + single + unit_pattern +
                  "|" + _MONEY + r"\s*/\s*" + single + unit_pattern +
                  r"|\bединиц[аы]\s+(?:продажи|цен[ыа]|измерения)\s*[:=—-]?\s*" + single + unit_pattern)
    return bool(re.search(price_unit, _space(quote), re.I))


def _numeric_requirement_backed(value, quote):
    expected = []
    for item in value if isinstance(value, (list, tuple)) else [value]:
        amount = _decimal(item)
        expected.extend([amount] if amount is not None else _numbers(item))
    quoted = _numbers(quote)
    # A long quote containing several specs cannot bind one numeric value to
    # the chosen label. The model can supply the short exact clause instead.
    return not expected or len(quoted) == len(expected) and sorted(quoted) == sorted(expected)


def _uncovered_numbers(description, requirements, quantity, unit):
    description = _space(description)
    covered = []
    for requirement in requirements:
        quote = _space(requirement["quote"])
        start = 0
        while (start := description.find(quote, start)) >= 0:
            covered.append((start, start + len(quote)))
            start += len(quote)
    unresolved = []
    for match in re.finditer(_NUMBER, description):
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        following = description[match.end():match.end() + 40]
        preceding = description[max(0, match.start() - 35):match.start()]
        unit_after_number = bool(re.match(r"\s*(?:" + _unit_pattern(unit) + ")", following, re.I))
        # Explicit purchase quantity/unit definitions are not product specs.
        if unit_after_number and (_decimal(match.group()) == _decimal(quantity) or _decimal(match.group()) == 1 and re.search(r"единиц[аы][^,;]*$", preceding, re.I)):
            continue
        left = max(description.rfind(",", 0, match.start()), description.rfind(";", 0, match.start())) + 1
        end = re.search(r"[,;](?!\d)", description[match.end():])
        right = match.end() + end.start() if end else len(description)
        unresolved.append(description[left:right].strip()[:120])
    return list(dict.fromkeys(unresolved))


def _operation_backed(op, quote):
    if not isinstance(op, str):
        return False
    lower = quote.casefold()
    lower_bound = bool(re.search(r"не\s*менее|минимум|\bот\s+\d|>=|≥|at least", lower))
    upper_bound = bool(re.search(r"не\s*более|максимум|\bдо\s+\d|<=|≤|at most", lower))
    if op in {"gte", ">="}:
        return lower_bound and not upper_bound
    if op in {"lte", "<="}:
        return upper_bound and not lower_bound
    if op == "gt":
        return bool(re.search(r"более|больше|greater than|>\s*\d", lower)) and not upper_bound
    if op == "lt":
        return bool(re.search(r"менее|меньше|less than|<\s*\d", lower)) and not lower_bound
    if op in {"eq", "=", "contains"}:
        return not lower_bound and not upper_bound
    if op == "in":
        return bool(re.search(r"\bили\b|\bor\b|[;/]", lower))
    return False


def _attribute_backed(key, label, quote):
    patterns = {
        "channels": r"канал|channels?", "bandwidth_mhz": r"полос\w*|bandwidth",
        "density_gsm": r"плотност|gsm|г\s*/\s*(?:м|кв\.?\s*м)", "sheets_per_pack": r"лист|sheets?",
        "format": r"формат|\b[aа][0-6]\b", "supplier_inn": r"\bинн\b|tax id",
    }
    if key == "bandwidth_mhz" and re.search(r"огранич\w*\s+полос|bandwidth\s+limit|limit\w*\s+bandwidth", quote, re.I):
        return False
    if key in patterns:
        return bool(re.search(patterns[key], quote, re.I))
    if key in {"model", "service_type"}:
        return True  # Exact model/service text is checked separately.
    words = re.findall(r"[^\W\d_]{3,}", str(label or "") + " " + key.replace("_", " "), re.UNICODE)
    lower = quote.casefold()
    return any(word.casefold() in lower for word in words)


def validate_request(base_request, proposal):
    """Keep deterministic known categories; admit only quoted new constraints."""
    if not isinstance(base_request, dict):
        raise ValueError("Ожидается разобранная потребность.")
    result = parse_request(base_request.get("description"), base_request.get("quantity", 1))
    # A proposal cannot overwrite a supported category, quantity or hard spec.
    if result["category"] != "other" or not isinstance(proposal, dict):
        return result
    description = result["description"]
    product = proposal.get("product")
    category = proposal.get("category")
    if not isinstance(product, str) or len(product) > 300 or _space(product).casefold() not in _space(description).casefold():
        return result
    if not isinstance(category, str) or not re.fullmatch(r"[\w -]{2,80}", category) or category in {"other", "unknown"}:
        return result
    requirements = []
    keys = set()
    rejected = []
    for item in (proposal.get("requirements") if isinstance(proposal.get("requirements"), list) else [])[:30]:
        if not isinstance(item, dict):
            rejected.append("Некорректное требование")
            continue
        key, label, quote = item.get("key"), item.get("label"), item.get("quote")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or key in keys:
            rejected.append(str(key or "Без ключа"))
            continue
        op = item.get("op", "eq")
        if not isinstance(quote, str) or not 1 <= len(quote) <= 800 or not _contains(quote, description) or not _value_backed(item.get("value"), quote) or not _numeric_requirement_backed(item.get("value"), quote) or not _operation_backed(op, quote) or not _attribute_backed(key, label, quote):
            rejected.append(str(label or key))
            continue
        unit = item.get("unit")
        if unit and not _unit_backed(unit, quote):
            rejected.append(str(label or key))
            continue
        requirements.append({"key": key, "label": str(label or key)[:120], "op": op,
                             "value": deepcopy(item["value"]), "unit": unit, "quote": quote})
        keys.add(key)
    if not requirements:
        return result
    result.update(category=category, product=product, requirements=requirements)
    missing = []
    proposed_unit = proposal.get("unit")
    if _unit_backed(proposed_unit, description):
        result["unit"] = proposed_unit
    elif base_request.get("unit_explicit") and isinstance(base_request.get("unit"), str):
        result["unit"] = base_request["unit"]
    else:
        missing.append("явная единица потребности")
    if rejected:
        missing.extend("неподтверждённое требование: " + item for item in rejected)
    uncovered = _uncovered_numbers(description, requirements, result["quantity"], result["unit"])
    missing.extend("неразобранное числовое условие: " + clause for clause in uncovered)
    result["missing_critical_fields"] = missing
    result["validation"] = {"method": "quoted_dynamic_requirements", "verified_requirements": len(requirements),
                            "rejected_requirements": rejected, "unresolved_conditions": uncovered,
                            "unit_verified": not bool("явная единица потребности" in missing)}
    terms = proposal.get("search_terms")
    if isinstance(terms, list):
        result["search_terms"] = list(dict.fromkeys([term.strip()[:220] for term in terms if isinstance(term, str) and term.strip()]))[:6] or result["search_terms"]
    return result


def _currency(quote):
    found = set()
    for currency, pattern in {"RUB": r"₽|р\s*уб(?:\.|лей|ля)?|\bRUB\b", "USD": r"\$|\bUSD\b|доллар", "EUR": r"€|\bEUR\b|евро"}.items():
        if re.search(pattern, quote, re.I):
            found.add(currency)
    return next(iter(found)) if len(found) == 1 else None


def _tax_backed(value, quote):
    excluded = bool(re.search(r"без\s*НДС|НДС\s*не\s*включ|excl(?:uding)?\.?\s*VAT", quote, re.I))
    included = bool(re.search(r"(?:\bс\s*НДС|вкл\.?\s*НДС|включая\s*НДС|НДС\s*включ|incl(?:uding)?\.?\s*VAT)", quote, re.I))
    return excluded if value == "excluded" else included and not excluded if value == "included" else False


def _price_tokens(text):
    """Locate amounts attached to money or directly to a price label."""
    text = _space(text)
    tokens = {}
    for match in re.finditer(r"(?P<amount>" + _NUMBER + r")\s*" + _MONEY, text, re.I):
        tokens[match.start("amount")] = (match.start("amount"), match.end("amount"), _decimal(match.group("amount")))
    for match in re.finditer(_MONEY + r"\s*(?P<amount>" + _NUMBER + ")", text, re.I):
        tokens[match.start("amount")] = (match.start("amount"), match.end("amount"), _decimal(match.group("amount")))
    for match in re.finditer(r"\b(?:цена|стоимость|price)\s*[:=—-]?\s*(?P<amount>" + _NUMBER + ")", text, re.I):
        tokens.setdefault(match.start("amount"), (match.start("amount"), match.end("amount"), _decimal(match.group("amount"))))
    return sorted(tokens.values())


_QUANTITY_CONDITION = re.compile(
    r"(?P<condition>\b(?:при\s+(?:заказе|покупке)\s*(?:(?:от|до|не\s+менее|не\s+более|свыше|более|менее)\s*)?"
    r"|(?:от|до|не\s+менее|не\s+более|свыше|более|менее)\s*))"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>штук\w*|шт\.?|пач\w*|упаков\w*|короб\w*|лист\w*|час\w*|м[²2]|pieces?|packs?|boxes)", re.I)


def _stock_quantity(context, match):
    before = context[max(0, match.start() - 100):match.start()]
    stocks = list(re.finditer(r"в\s+наличии|наличие|остаток|на\s+складе", before, re.I))
    orders = list(re.finditer(r"при\s+(?:заказе|покупке)|цена\s+(?:от|при)", before, re.I))
    return bool(stocks and (not orders or stocks[-1].start() > orders[-1].start()))


def price_conditions(context, request):
    """Retain purchase constraints separately from the observed source price."""
    result = []
    quantity = _decimal(request.get("quantity"))
    for match in _QUANTITY_CONDITION.finditer(context or ""):
        if _stock_quantity(context, match):
            continue
        bound = _decimal(match["amount"])
        applies = None
        if quantity is not None and _unit_backed(request.get("unit"), match["unit"]):
            prefix = _space(match["condition"]).casefold()
            if re.search(r"не\s+менее|\bот\b", prefix): applies = quantity >= bound
            elif re.search(r"не\s+более|\bдо\b", prefix): applies = quantity <= bound
            elif re.search(r"свыше|более", prefix): applies = quantity > bound
            elif re.search(r"менее", prefix): applies = quantity < bound
            else: applies = quantity == bound
        result.append({"quote": match.group(), "quantity": float(bound), "unit": match["unit"], "applies": applies})
    # A minimum-order message may be phrased without the ordinary price-tier
    # preposition; the exact clause still needs to be shown to a reviewer.
    for match in re.finditer(r"минимальн\w*\s+количеств\w*\s+(?:для\s+)?заказ\w*\s*[—:=-]?\s*(\d+(?:[.,]\d+)?)\s*(шт\.?|штук\w*|пач\w*|упаков\w*)", context or "", re.I):
        if not any(row["quantity"] == float(_decimal(match[1])) and row["unit"].rstrip('.') == match[2].rstrip('.') for row in result):
            applies = quantity >= _decimal(match[1]) if quantity is not None and _unit_backed(request.get("unit"), match[2]) else None
            result.append({"quote": match.group(), "quantity": float(_decimal(match[1])), "unit": match[2], "applies": applies})
    return result


def _price_context(text, start, end):
    boundaries = [0] + [match.end() for match in re.finditer(r"[.!?;]\s+(?=[A-ZА-ЯЁ])", text)] + [len(text)]
    left = max(boundary for boundary in boundaries if boundary <= start)
    right = min(boundary for boundary in boundaries if boundary >= end)
    previous = max((boundary for boundary in boundaries if boundary < left), default=left)
    if _QUANTITY_CONDITION.search(text[previous:left]) and not _price_tokens(text[previous:left]):
        left = previous
    # A distant condition or second price in an unsegmented block is ambiguous;
    # never silently narrow it to the model's short citation.
    return text[left:right] if right - left <= 1600 else None


def _model_tokens(text):
    pattern = (r"(?<!\w)(?:(?=[\w/-]*\d)[a-zа-яё][a-zа-яё0-9]*(?:[-_/][a-zа-яё0-9]+)+"
               r"|[a-z][a-z0-9]*\d[a-z0-9]*(?:[/.-][a-zа-яё0-9]+)*"
               r"|(?-i:[A-ZА-ЯЁ]{2,})\s+\d{2,}(?:[/.-][a-zа-яё0-9]+)*)(?!\w)")
    result = set()
    for match in re.finditer(pattern, text, re.I):
        prefix = re.sub(r"\W", "", re.split(r"\d", match.group())[0].casefold())
        if prefix not in {"цена", "стоимость", "price", "rub", "usd", "eur", "ндс", "vat", "гб", "gb", "шт", "мгц", "mhz", "ghz", "ram", "ssd", "ddr"}:
            result.add(_model(match.group()))
    return result


def _price_backed(value, quote, focus, title, attrs, request, dom_contexts=()):
    value = _decimal(value)
    if value is None or value <= 0:
        return False, None
    quote = _space(quote)
    verified = [item for item in dom_contexts if item["amount"] == value and _contains(quote, item["context"])]
    if dom_contexts and not verified:
        return False, None
    tokens = _price_tokens(quote)
    matching = [token for token in tokens if token[2] == value]
    table_quote = any(item.get("method") == "primary_visible_table" and item["quote"] == quote for item in verified)
    if not table_quote and (not matching or len({token[2] for token in tokens}) != 1):
        return False, None
    expected_models = _model_tokens(title)
    model = attrs.get("model")
    if isinstance(model, str) and _model(model) in _model(title):
        expected_models.add(_model(model))

    def valid_context(context, table=False, observed=False):
        if not context:
            return False
        if not table and len({token[2] for token in _price_tokens(context)}) != 1:
            extras = [token for token in _price_tokens(context) if token[2] != value]
            if not observed or any(not re.search(r"(?:наценк\w*|доплат\w*|доставк\w*|сбор\w*)\s*[:—=-]?\s*$", context[max(0, token[0]-45):token[0]], re.I) for token in extras):
                return False
        if re.search(r"минимальн\w*\s+цен|старая\s+цена|old\s+price", context, re.I):
            return False
        offset = context.find(quote)
        if offset < 0 or re.search(r"(?:\bот|\bfrom)\s*$", context[max(0, offset - 35):offset], re.I):
            return False
        if not table:
            contextual_models = _model_tokens(context)
            if expected_models and contextual_models - expected_models:
                return False
            if not expected_models and contextual_models and re.search(r"цен[аы]\s+(?:для|модел)|price\s+for", context, re.I):
                return False
        quantity = _decimal(request.get("quantity"))
        for condition in _QUANTITY_CONDITION.finditer(context):
            if _stock_quantity(context, condition) or observed:
                continue
            if quantity is None or not _unit_backed(request.get("unit"), condition["unit"]):
                return False
            bound = _decimal(condition["amount"])
            prefix = _space(condition["condition"]).casefold()
            if re.search(r"не\s+менее|\bот\b", prefix):
                applies = quantity >= bound
            elif re.search(r"не\s+более|\bдо\b", prefix):
                applies = quantity <= bound
            elif re.search(r"свыше|более", prefix):
                applies = quantity > bound
            elif re.search(r"менее", prefix):
                applies = quantity < bound
            else:
                applies = quantity == bound
            if not applies:
                return False
        return True

    # Independently bound primary DOM blocks take precedence over duplicate
    # SEO/menu/from-price occurrences elsewhere in the flat captured page.
    if verified:
        for item in verified:
            context = item["context"]
            if valid_context(context, item.get("method") == "primary_visible_table", observed=True):
                return True, context
        return False, None
    contexts, offset = [], 0
    while (offset := focus.find(quote, offset)) >= 0:
        for number_start, number_end, _ in matching:
            absolute_start, absolute_end = offset + number_start, offset + number_end
            if re.search(r"(?:\bот|\bfrom)\s*$", focus[max(0, absolute_start - 35):absolute_start], re.I):
                return False, None
            context = _price_context(focus, absolute_start, absolute_end)
            if not valid_context(context):
                return False, None
            contexts.append(context)
        offset += len(quote)
    return bool(contexts), contexts[0] if contexts else None


def _printer_property_concept(label):
    compact = re.sub(r"[^a-zа-яё0-9]", "", _space(label).casefold())
    if "точност" in compact and compact.endswith("xy"):
        return "position_xy"
    if "точност" in compact and compact.endswith("z"):
        return "position_z"
    if compact in {"интерфейс", "интерфейсы"}:
        return "interfaces"
    if compact == "расходныематериалы":
        return "materials"
    if re.match(r"^диаметрсоп(?:л|ел)", compact):
        return "nozzle_options"
    if compact == "печатнаяплатформа":
        return "platform_material"
    if compact.startswith("габаритыустройства"):
        return "dimensions"
    if compact in {"форматфайлов", "форматыфайлов", "поддержкаформатов", "fileformats", "supportedformats"}:
        return "file_formats"
    return _attribute_concept(label)


def quality_attribute_rows(document):
    """Retain anonymous two-column facts inside the verified primary product."""
    from .price_context import _primary_nodes, _active_text, _captured_quote, _locator
    rows = primary_attribute_rows(document)
    seen = {(row["raw_label"].casefold(), row["raw_value"].casefold()) for row in rows}
    visible = _space(document.get("text"))
    for node, scope, primary, text in _primary_nodes(document,
            lambda n: n.tag in {"div", "li", "p"} and len(n.children) == 2) or []:
        foreign = False
        primary_models = _named_printer_models(primary)
        for parent in node.ancestors():
            if parent is scope:
                break
            if parent.children:
                heading = _active_text(parent.children[0], scope, primary)
                models = _named_printer_models(heading) if len(heading) <= 160 else []
                if models and primary_models and not _printer_models_match(primary_models, models):
                    foreign = True
                    break
        if foreign:
            continue
        children = [child for child in node.children if not child.hidden]
        if len(children) != 2 or any(child.tag in {"a", "button", "input", "img"} for child in children):
            continue
        label, value = [_active_text(child, scope, primary) for child in children]
        if (not label or not value or len(label) > 120 or len(value) > 1000
                or not re.search(r"[a-zа-яё]{2}", label, re.I)
                or re.search(r"цен[аы]|стоимость|price|артикул|наличи|остаток", label, re.I)):
            continue
        quote = _captured_quote(text, visible)
        identity = (label.casefold(), value.casefold())
        if quote and identity not in seen:
            rows.append({"raw_label": label, "raw_value": value, "quote": quote,
                         "locator": _locator(node), "basis": "primary_product_attribute"})
            seen.add(identity)
    return rows


def _quality_normal_observation(row, requirement):
    normalized = dict(row)
    speed_alias = bool(re.search(r"мм\s*/\s*сек(?:унд[уы]?)?\b", row.get("raw_value", ""), re.I))
    if speed_alias:
        normalized["raw_value"] = re.sub(r"мм\s*/\s*сек(?:унд[уы]?)?\b", "мм/с", row["raw_value"], flags=re.I)
    fact = _normal_observation(normalized, requirement)
    if fact and speed_alias:
        fact.update(raw_value=row["raw_value"], source_unit="мм/сек", normalization=fact.get("normalization", "") + ";speed_unit_word_alias")
    return fact


def quality_requirement_observations(document, request):
    result = primary_requirement_observations(document, request)
    rows = quality_attribute_rows(document)
    for requirement in request.get("requirements", []):
        if not isinstance(requirement, dict) or not requirement.get("key"):
            continue
        concept = _attribute_concept(requirement.get("label") or requirement["key"])
        matches = [row for row in rows if _attribute_concept(row["raw_label"]) == concept]
        if not matches:
            continue
        if re.search(r"оптимальн|рекомендуем|optimal|recommended", str(requirement.get("label")), re.I):
            matches = [row for row in matches if re.search(r"оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        elif re.search(r"максим|maximum|\bmax\b", str(requirement.get("label")), re.I):
            matches = [row for row in matches if not re.search(r"миним|minimum|\bmin\b|оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        elif re.search(r"миним|minimum|\bmin\b", str(requirement.get("label")), re.I):
            matches = [row for row in matches if not re.search(r"максим|maximum|\bmax\b|оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        facts = [_quality_normal_observation(row, requirement) for row in matches]
        if facts and all(fact is not None for fact in facts) and len({repr(fact["value"]) for fact in facts}) == 1:
            result[requirement["key"]] = facts[0]
        elif matches:
            result.pop(requirement["key"], None)
    return result


def printer_requirement_observations(document, request, rows=None, primary_text=None):
    """Bounded primary-row aliases; preserve uncertain list interpretation."""
    subject = request.get("product") or request.get("title") or request.get("description")
    title = document.get("primary_title") or document.get("title")
    if _printer_object_kind(subject)[0] != "printer_3d" or _printer_object_kind(title)[0] != "printer_3d":
        return {}
    rows = list(quality_attribute_rows(document) if rows is None else rows)
    text = primary_text if primary_text is not None else (primary_product_context(document) or {}).get("text", "")
    # The same word "камера" also names the printing chamber. Only the
    # explicitly adjacent drying-compartment heading binds this prose value.
    dryer = re.compile(r"Отсек\s+для\s+сушки\s+пластика.{0,260}?Температура\s+в\s+камере\s+может\s+достигать\s+(?P<value>\d+(?:[.,]\d+)?(?:\s*и\s*\d+(?:[.,]\d+)?)?\s*°\s*[CС])", re.I)
    for match in dryer.finditer(text):
        rows.append({"raw_label": "Температура в камере", "raw_value": match["value"],
                     "quote": match.group(), "basis": "primary_product_description_attribute",
                     "source_concept": "dryer_temperature", "context_label": "Отсек для сушки пластика"})
    supported = {"position_xy", "position_z", "interfaces", "materials", "nozzle_options", "platform_material", "dimensions", "dryer_temperature", "file_formats"}
    result = {}
    for requirement in request.get("requirements") or []:
        if not isinstance(requirement, dict) or not requirement.get("key"):
            continue
        concept = _printer_property_concept(requirement.get("label") or requirement["key"])
        if concept not in supported:
            continue
        facts = []
        for row in rows:
            if (row.get("source_concept") or _printer_property_concept(row["raw_label"])) != concept:
                continue
            if concept == "platform_material" and not re.search(r"стекл|алюмини|сталь|керамик|buildtak|\bpei\b|glass|alumin", row["raw_value"], re.I):
                continue
            normalized_row = dict(row)
            micron = re.search(r"\bмикрон(?:а|ов)?\b", row["raw_value"], re.I)
            if micron:
                normalized_row["raw_value"] = re.sub(r"\bмикрон(?:а|ов)?\b", "мкм", row["raw_value"], flags=re.I)
            fact = _quality_normal_observation(normalized_row, requirement)
            if fact is None:
                continue
            fact.update(raw_value=row["raw_value"], raw_label=row["raw_label"])
            if micron:
                fact.update(source_unit=micron.group(), normalization=fact.get("normalization", "") + ";micron_word_alias")
            if concept in {"materials", "nozzle_options", "platform_material"} and _space(fact["value"]).casefold() != _space(requirement.get("value")).casefold():
                fact["comparison_supported"] = False
                fact["comparison_reason"] = "Значение источника извлечено; сравнение перечня, комплектации или альтернатив требует проверки."
            facts.append(fact)
        if facts and len({repr(fact["value"]) for fact in facts}) == 1:
            result[requirement["key"]] = facts[0]
    return result


def _primary_fact_title(document):
    parser = _ShapeParser()
    try:
        parser.feed(str(document.get("html") or ""))
    except (ValueError, RecursionError):
        return ""
    heading = _space(" ".join(parser.heading) or document.get("primary_title") or document.get("title"))
    return heading if heading and _contains(heading, document.get("text", "")) else ""


def _cms_literal(raw, start):
    """A bounded literal subtree, never JavaScript evaluation."""
    quote, escaped, depth = None, False, 0
    for index in range(start, min(len(raw), start + 200000)):
        char = raw[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            depth += 1
            if depth > 40:
                return None
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    return None


def _parse_cms_literal(raw):
    parts = re.split(r"('(?>[^'\\]|\\.)*'|\"(?>[^\"\\]|\\.)*\")", raw)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\b(true|false|null)\b", lambda m: {"true":"True", "false":"False", "null":"None"}[m[0]], parts[index])
    return ast.literal_eval("".join(parts))


def primary_cms_financial_facts(document):
    raw = str(document.get("html") or "")
    if len(raw) > 2000000:
        return {}
    title = _primary_fact_title(document)
    url = str(document.get("url") or "")
    found = []
    for match in list(re.finditer(r"\bsetViewedProduct\s*\(\s*(\d+)\s*,\s*(\{)", raw))[:8]:
        literal = _cms_literal(raw, match.start(2))
        if not literal:
            continue
        try:
            product = _parse_cms_literal(literal)
        except (ValueError, SyntaxError, RecursionError, MemoryError):
            continue
        if not isinstance(product, dict) or str(product.get("PRODUCT_ID")) != match[1]:
            continue
        if (not product.get("DETAIL_PAGE_URL") or _space(html_module.unescape(str(product.get("NAME") or ""))).casefold() != title.casefold()
                or urljoin(url, str(product.get("DETAIL_PAGE_URL") or "")) != url):
            continue
        price = product.get("MIN_PRICE")
        if not isinstance(price, dict) or price.get("CAN_BUY") not in {None, "Y", True} or price.get("CAN_ACCESS") not in {None, "Y", True}:
            continue
        amount, vat, net = map(_decimal, (price.get("VALUE"), price.get("VALUE_VAT"), price.get("VALUE_NOVAT")))
        currency = price.get("CURRENCY")
        if amount is None or amount <= 0 or currency not in {"RUB", "USD", "EUR"}:
            continue
        price_quote = None
        for block in re.finditer(r"(['\"])MIN_PRICE\1\s*:\s*(\{)", literal):
            fragment = _cms_literal(literal, block.start(2))
            try:
                if fragment and _parse_cms_literal(fragment) == price:
                    price_quote = fragment
                    break
            except (ValueError, SyntaxError, RecursionError):
                pass
        if not price_quote:
            continue
        base = {"url": url, "document_id": document.get("document_id") or document.get("id"),
                "source_hash": document.get("sha256"), "retrieved_at": document.get("retrieved_at"),
                "visibility": "embedded_product_data", "basis": "primary_cms_product_literal",
                "locator": "setViewedProduct/" + match[1], "primary_name": title,
                "primary_url": url, "product_id": match[1]}
        facts = {"price": {"value": float(amount), "quote": price_quote, **base},
                 "currency": {"value": currency, "quote": price_quote, **base}}
        if vat is not None and net is not None and amount == vat and net < vat:
            facts["tax"] = {"value": "included", "quote": price_quote, **base}
            primary_text = (primary_product_context(document) or {}).get("text", "")
            if re.search(r"без\s*НДС|НДС\s*не\s*включ|excl(?:uding)?\s*VAT", primary_text, re.I):
                facts.pop("tax", None)
        unit = product.get("CATALOG_MEASURE_NAME")
        if isinstance(unit, str) and _unit(unit) in {"piece", "pack", "box", "sheet", "m2", "hour"}:
            for item in re.finditer(r"(['\"])CATALOG_MEASURE_NAME\1\s*:\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", literal):
                try:
                    if ast.literal_eval(item[2]) == unit:
                        facts["unit"] = {"value": unit, "quote": item[0], **base}
                        break
                except (ValueError, SyntaxError):
                    pass
        found.append(facts)
    fingerprint = lambda row: (row['price']['value'], row['currency']['value'],
                              _unit(row['unit']['value']) if 'unit' in row else None,
                              row.get('tax', {}).get('value'))
    if not found or len({fingerprint(row) for row in found}) != 1:
        return {}
    return found[0]


def _model_brand_words(title):
    generic = {"принтер", "осциллограф", "printer", "product", "технические", "характеристики",
               "specifications", "technical", "купить", "модель", "model", "pro", "plus", "mini", "max", "ultra", "hs",
               "профессиональный", "промышленный", "настольный", "professional", "industrial", "desktop", "новый", "new"}
    # A shared epithet is not a shared brand. Use the single lexical anchor
    # immediately preceding the model, rather than intersecting every word.
    models = [match for match in _NAMED_PRINTER_MODEL.finditer(title) if _named_printer_models(match.group())]
    before = title[:models[0].start()].rstrip() if len(models) == 1 else ""
    tokens = re.findall(r"[a-zа-яё]{3,}", before, re.I)
    words = {tokens[-1].casefold()} - generic if tokens else set()
    return {word.translate(str.maketrans({"а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "т": "t", "х": "x"})) for word in words}


def _fact_comparison_key(value, requirement):
    if not isinstance(value, str):
        return repr(value)
    concept = _printer_property_concept(requirement.get("label") or requirement.get("key") or "")
    if concept == "materials":
        return (concept, tuple(sorted(_material_entries(value))))
    if concept in {"nozzle_options", "file_formats", "interfaces"}:
        return (concept, tuple(sorted(part.strip().casefold() for part in re.split(r"[,;/]", value) if part.strip())))
    return _space(value).casefold()


def _explicit_fact_denial(value):
    return isinstance(value, str) and bool(re.search(r"не\s+поддерж\w*|не\s+предусмотр\w*|not\s+support\w*|unsupported|^(?:нет|no|отсутствует)$", value.strip(), re.I))


def enrich_exact_model_facts(offer, primary_document, fact_documents, request):
    """Join atomic technical facts, never prices or seller terms, by exact model.

    Every joined value keeps its own document/hash/URL/quote and both primary
    headlines establishing the model link. A missing fact stays missing and
    conflicting sources cannot be cherry-picked to satisfy the request.
    """
    if not offer or not fact_documents:
        return offer
    primary_title = _primary_fact_title(primary_document)
    primary_models = _named_printer_models(primary_title)
    if len(primary_models) != 1:
        return offer
    observed, used_documents = {}, {}
    primary_id = primary_document.get("document_id") or primary_document.get("id")
    matched_documents = set()
    for document in list(fact_documents)[:128]:
        if not isinstance(document, dict) or document is primary_document:
            continue
        identifier = document.get("document_id") or document.get("id")
        if not identifier or identifier == primary_id or not document.get("sha256") or not document.get("url"):
            continue
        preliminary = _named_printer_models(document.get("primary_title") or document.get("title"))
        if len(preliminary) != 1 or not _printer_models_match(primary_models, preliminary):
            continue
        if identifier in matched_documents:
            continue
        matched_documents.add(identifier)
        if len(matched_documents) > 12:
            break
        shape = classify_page(document)
        if shape["kind"] != "product" or document.get("source_role") == "seller_terms":
            continue
        title = _primary_fact_title(document)
        models = _named_printer_models(title)
        if len(models) != 1 or not _printer_models_match(primary_models, models):
            continue
        primary_brand, fact_brand = _model_brand_words(primary_title), _model_brand_words(title)
        if models[0] != primary_models[0]:
            if models[0].endswith(primary_models[0]):
                fact_brand.add(models[0][:-len(primary_models[0])])
            elif primary_models[0].endswith(models[0]):
                primary_brand.add(primary_models[0][:-len(models[0])])
        if not primary_brand or not fact_brand or primary_brand != fact_brand:
            continue
        primary_kind, _ = _printer_object_kind(primary_title)
        fact_kind, _ = _printer_object_kind(title)
        if fact_kind in {"accessory", "pen_3d", "engraver", "office_printer"} and fact_kind != primary_kind:
            continue
        observations = quality_requirement_observations(document, request)
        aliases = printer_requirement_observations(document, request)
        observations.update({key: value for key, value in aliases.items() if key not in observations})
        requirements = {row.get("key"): row for row in request.get("requirements", []) if isinstance(row, dict)}
        for key, observation in observations.items():
            label = str(requirements.get(key, {}).get("label") or key)
            if key in {"supplier_inn", "supplier_group"} or re.search(r"цен[аы]|ндс|налог|гарант|достав|оплат|налич|поставщик|seller|supplier", label, re.I):
                continue
            quote = observation.get("quote")
            if not quote or not (_contains(quote, document.get("text", "")) or observation.get("visibility") == "reachable_tab"):
                continue
            proof = {name: deepcopy(value) for name, value in observation.items() if name != "value"}
            proof.update(url=document["url"], document_id=identifier, source_hash=document["sha256"],
                         retrieved_at=document.get("retrieved_at"),
                         binding={"basis": "exact_model_primary_titles", "model": primary_models[0],
                                  "primary_document_id": primary_id, "primary_title_quote": primary_title,
                                  "fact_document_id": identifier, "fact_title_quote": title})
            observed.setdefault(key, []).append((deepcopy(observation["value"]), proof))
            used_documents[identifier] = {name: document.get(name) for name in ("url", "sha256", "retrieved_at")}
    for key, facts in observed.items():
        field = "attributes." + key
        requirement = next((row for row in request.get("requirements", []) if row.get("key") == key), {})
        distinct = {_fact_comparison_key(value, requirement) for value, _ in facts}
        current = offer.get("attributes", {}).get(key)
        if len(distinct) != 1:
            numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value, _ in facts)
            bucket = "fact_conflicts" if numeric or any(_explicit_fact_denial(value) for value, _ in facts) else "fact_uncertainties"
            offer.setdefault("validation", {}).setdefault(bucket, []).append({"field": field,
                "reason": "Документы одной модели содержат различающиеся наблюдения; факт не объединён.",
                "sources": [deepcopy(proof) for _, proof in facts]})
            continue
        value, proof = facts[0]
        if current is not None:
            # The seller's own observed value has priority, but a contradictory
            # same-model document is retained as an explicit unresolved conflict.
            if _fact_comparison_key(current, requirement) != _fact_comparison_key(value, requirement):
                numeric = (isinstance(current, (int, float)) and not isinstance(current, bool)
                           and isinstance(value, (int, float)) and not isinstance(value, bool))
                proven_conflict = numeric or _explicit_fact_denial(current) or _explicit_fact_denial(value)
                bucket = "fact_conflicts" if proven_conflict else "fact_uncertainties"
                offer.setdefault("validation", {}).setdefault(bucket, []).append({"field": field,
                    "reason": "Значение продавца отличается от дополнительного документа; исходное значение сохранено.",
                    "sources": [deepcopy(proof)]})
                if proven_conflict:
                    issue = "Характеристика «" + key + "» различается в документах одной модели; требуется проверка версии или комплектации."
                    offer.setdefault("source_issues", []).append(issue)
                    offer.setdefault("validation", {}).setdefault("fact_source_issues", []).append(issue)
            continue
        offer.setdefault("attributes", {})[key] = value
        offer.setdefault("evidence", {})[field] = proof
        validation = offer.setdefault("validation", {})
        validation.setdefault("fact_enriched_fields", []).append(field)
        validation["rejected_fields"] = [name for name in validation.get("rejected_fields", []) if name != field]
    if used_documents:
        offer["fact_documents"] = [{"document_id": identifier, **metadata} for identifier, metadata in used_documents.items()]
    return offer


def validate_candidate(candidate, document, request, *, fact_documents=()):
    """Drop unsupported values, retaining incomplete observations as uncertain."""
    if not isinstance(candidate, dict) or not isinstance(document, dict) or not isinstance(document.get("text"), str):
        return None
    if document.get("source_role") == "seller_terms":
        return None
    title, url = candidate.get("title"), document.get("url")
    if not isinstance(title, str) or not title.strip() or len(title) > 300 or not isinstance(url, str):
        return None
    try:
        parsed_url = urlsplit(url)
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            return None
    except ValueError:
        return None
    attrs = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
    source_title = _space(document.get("primary_title") or document.get("title"))
    page_shape = classify_page(document)
    if page_shape["kind"] in {"listing", "article"}:
        return None
    if (document.get("page_kind") == "listing" and page_shape["kind"] != "product") or document.get("is_listing") or str(document.get("page_type")) in {"listing", "category", "search"} or attrs.get("is_listing") or str(attrs.get("page_type")) in {"listing", "category", "search"} or candidate.get("is_listing"):
        return None
    if re.search(r"каталог|результаты\s+поиска|поиск\s+товаров|выбрать\s+и\s+заказать|product\s+search", source_title, re.I):
        return None
    text = _space(document["text"])
    title_normal = _space(title)
    start = text.find(source_title) if source_title else -1
    product_context = primary_product_context(document)
    focus = product_context["text"] if product_context else text[start:] if 0 <= start < 4000 else text
    if not product_context:
        cut = re.search(r"Рекомендуем(?:ые|\s+также)|Вас\s+также\s+может|Похожие\s+товары|Другие\s+товары\s+из\s+категории|С\s+этим\s+товаром\s+покупают|Сопутствующие\s+товары|related\s+products|you\s+may\s+also\s+like", focus, re.I)
        if cut:
            focus = focus[:cut.start()]
    if title_normal not in focus:
        return None
    if source_title and title_normal.casefold() not in source_title.casefold() and source_title.casefold() not in title_normal.casefold():
        return None
    if not source_title and focus.find(title_normal) > 1500:
        return None
    supplied_evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    document_id = document.get("document_id") or document.get("id")
    source_hash = document.get("sha256") or hashlib.sha256(document["text"].encode()).hexdigest()
    evidence = {}
    values = {}
    rejected = []
    price_context = None
    dom_contexts = primary_price_contexts(document)
    cms_facts = primary_cms_financial_facts(document)
    source_issues = source_variant_issues(document)
    requirements = {item["key"]: item for item in request.get("requirements", []) if isinstance(item, dict) and item.get("key")}
    observation_request = {**request, "requirements": [*request.get("requirements", []),
        *([] if any(r.get("key") == "brand" for r in request.get("requirements", [])) else [{"key": "brand", "label": "Бренд", "value": "", "op": "eq"}])]}
    source_attributes = quality_requirement_observations(document, observation_request)
    source_rows = quality_attribute_rows(document)
    aliases = printer_requirement_observations(document, observation_request, source_rows, focus)
    source_attributes.update({key: fact for key, fact in aliases.items() if key not in source_attributes})
    from .attribute_hints import explicit_paper_color
    color = explicit_paper_color(document)
    if color and "color" not in source_attributes:
        source_attributes["color"] = color
    source_images = primary_product_images(document)
    source_supplier = supplier_details(document)
    allowed = {"price", "currency", "unit", "pack_size", "tax", "supplier", "delivery", "title", "image_url"}
    for field, item in supplied_evidence.items():
        if not isinstance(field, str) or not isinstance(item, dict):
            continue
        quote = item.get("quote")
        attribute = field[11:] if field.startswith("attributes.") else None
        if field not in allowed and attribute is None:
            continue
        value = attrs.get(attribute) if attribute is not None else candidate.get(field)
        cms = cms_facts.get(field)
        if (cms and item.get("visibility") == "embedded_product_data" and item.get("quote") == cms["quote"]
                and value == cms["value"]):
            if source_issues or dom_contexts and any(
                    row["amount"] != _decimal(cms_facts["price"]["value"])
                    or (row.get("currency") or _currency(row.get("quote", ""))) not in {None, cms_facts["currency"]["value"]}
                    for row in dom_contexts):
                rejected.append(field)
                reason = "Финансовые данные CMS противоречат основной цене или валюте; дополнение не принято."
                if reason not in source_issues:
                    source_issues.append(reason)
                continue
            values[field] = value
            evidence[field] = {key: deepcopy(val) for key, val in cms.items() if key != "value"}
            continue
        observation = source_attributes.get(attribute) if attribute is not None else None
        dom_observed = bool(observation and quote == observation.get("quote")
                            and value == observation.get("value")
                            and observation.get("basis") == "primary_product_dom_attribute"
                            and observation.get("visibility") == "reachable_tab"
                            and observation.get("locator") and observation.get("tab_id"))
        canonical_attribute = bool(observation and quote == observation.get("quote") and value == observation.get("value"))
        dom_commercial = field in {"price", "currency", "unit", "tax"} and any(_contains(quote, row.get("context", "")) for row in dom_contexts) and _contains(quote, text)
        if field == "image_url":
            observation = next((image for image in source_images if image["url"] == value), None)
            if observation is None:
                rejected.append(field)
                continue
            values[field] = value
            evidence[field] = {key: val for key, val in observation.items() if key != "url"}
            evidence[field].update(url=url, document_id=document_id, source_hash=source_hash,
                                   retrieved_at=document.get("retrieved_at"))
            continue
        relevant_text = text if field == "supplier" or attribute == "supplier_inn" else focus
        if not isinstance(quote, str) or not 1 <= len(quote) <= 1200 or not (_contains(quote, relevant_text) or dom_observed or dom_commercial or canonical_attribute and _contains(quote, text)) or value is None:
            rejected.append(field)
            continue
        valid = False
        if field == "currency":
            valid = isinstance(value, str) and value in {"RUB", "USD", "EUR"} and _currency(quote) == value
        elif field == "tax":
            valid = _tax_backed(value, quote)
        elif field == "unit":
            valid = _sale_unit_backed(value, quote)
        elif field == "price":
            valid, price_context = _price_backed(value, quote, focus, title, attrs, request, dom_contexts) if not source_issues else (False, None)
        elif field == "pack_size":
            amount = _decimal(value)
            valid = amount is not None and amount > 0 and amount == amount.to_integral_value() and _value_backed(value, quote) and bool(re.search(r"упаков|короб|пач|pack|box", quote, re.I))
        elif attribute is not None:
            observation = source_attributes.get(attribute)
            observed = bool(observation and quote == observation["quote"])
            valid = (value == observation["value"] if observed else
                     _space(value).casefold().replace("а", "a") in _space(quote).casefold().replace("а", "a") if attribute == "format" else _value_backed(value, quote))
            if product_context and attribute not in {"supplier_inn", "supplier_group", "model"}:
                valid = valid and (_contains(quote, product_context["text"]) or _contains(quote, source_title) or dom_observed or canonical_attribute)
            if attribute == "bandwidth_mhz":
                # A short model quote must not hide the preceding word
                # "Ограничение" from the captured filter/limiter label.
                attribute_focus = product_context["text"] if product_context else focus
                occurrences = [match.start() for match in re.finditer(re.escape(_space(quote)), attribute_focus)]
                valid = valid and any(not re.search(r"огранич\w*\s*$|limit\w*\s*$", attribute_focus[max(0, offset - 40):offset], re.I) for offset in occurrences)
            if attribute == "supplier_inn":
                valid = (valid and str(value) == source_supplier.get("inn")
                         and source_supplier["verification"]["status"] == "format_valid"
                         and _attribute_backed(attribute, None, quote))
            elif attribute == "supplier_group":
                valid = False  # A model cannot manufacture seller affiliation.
            else:
                valid = valid and (observed or _attribute_backed(attribute, requirements.get(attribute, {}).get("label"), quote))
                requirement = requirements.get(attribute, {})
                # If the primary labelled rows for this property conflict or
                # have incompatible units, a model's shorter quotation cannot
                # select whichever number happens to satisfy the request.
                labelled = [row for row in source_rows
                            if _attribute_concept(row["raw_label"]) == _attribute_concept(requirement.get("label") or attribute)]
                if labelled and attribute not in source_attributes:
                    valid = False
                if not observed and _decimal(value) is not None and _measure_unit(requirement.get("unit")):
                    measured = _normal_observation({"raw_value": quote}, requirement)
                    valid = valid and bool(measured and _decimal(measured["value"]) == _decimal(value))
        else:
            valid = isinstance(value, str) and _value_backed(value, quote)
        if not valid:
            rejected.append(field)
            continue
        values[field] = value
        evidence[field] = {"quote": quote, "url": url, "document_id": document_id, "source_hash": source_hash,
                           "retrieved_at": document.get("retrieved_at")}
        if attribute is not None and source_attributes.get(attribute, {}).get("quote") == quote:
            evidence[field].update({key: val for key, val in source_attributes[attribute].items() if key not in {"value", "quote"}})
        if field == "price" and price_context:
            evidence[field]["context"] = price_context
    if "unit" in values and evidence["unit"].get("visibility") != "embedded_product_data" and price_context and not _contains(evidence["unit"]["quote"], price_context):
        # A different row's "per piece" cannot explain the selected price.
        if not re.search(r"единиц[аы]\s+(?:продажи|цен[ыа]|измерения)", evidence["unit"]["quote"], re.I):
            values.pop("unit")
            evidence.pop("unit")
            rejected.append("unit")
    # The visible primary title is itself a captured quotation.
    evidence.setdefault("title", {"quote": title, "url": url, "document_id": document_id, "source_hash": source_hash})
    price = values.get("price")
    if price is not None:
        price = float(_decimal(price))
    currency = values.get("currency")
    if not currency and price is not None:
        currency = _currency(evidence["price"]["quote"])
        if currency:
            evidence["currency"] = dict(evidence["price"])
    domain = parsed_url.hostname.removeprefix("www.").lower()
    if source_supplier["verification"]["status"] == "conflict":
        if "supplier" in values:
            values.pop("supplier")
            evidence.pop("supplier", None)
            rejected.append("supplier")
    supplier = source_supplier.get("legal_name") or values.get("supplier") or domain
    validated_attrs = {field[11:]: value for field, value in values.items() if field.startswith("attributes.")}
    if ("supplier_inn" not in attrs and source_supplier.get("inn")
            and source_supplier["verification"]["status"] == "format_valid"):
        validated_attrs["supplier_inn"] = source_supplier["inn"]
        evidence["attributes.supplier_inn"] = deepcopy(source_supplier["evidence"]["inn"])
    if source_supplier.get("legal_name"):
        evidence["supplier"] = deepcopy(source_supplier["evidence"]["legal_name"])
    # A generic model also has to identify this product, not another product in
    # a comparison paragraph within the same primary product description.
    if validated_attrs.get("model") and _model(validated_attrs["model"]) not in _model(title):
        validated_attrs.pop("model")
        evidence.pop("attributes.model", None)
        rejected.append("attributes.model")
    offer = {"id": hashlib.sha256((_canonical_url(url) + "\n" + title_normal.casefold()).encode()).hexdigest()[:16],
             "title": title.strip(), "supplier": supplier,
             "supplier_details": source_supplier,
             "supplier_basis": "source_legal_name" if source_supplier.get("legal_name") else "quoted_name" if "supplier" in values else "observed_hostname",
             "domain": domain, "url": url, "retrieved_at": document.get("retrieved_at"), "price": price,
             "currency": currency, "unit": values.get("unit"), "pack_size": values.get("pack_size"),
             "tax": values.get("tax", "unknown"), "delivery": values.get("delivery"), "attributes": validated_attrs,
             "evidence": evidence, "provenance": "live", "document_id": document_id, "source_hash": source_hash,
             "validation": {"rejected_fields": sorted(set(rejected)), "scope": "single_product"}}
    if price is not None and price_context:
        constraints = price_conditions(price_context, request)
        if constraints:
            offer["pricing_conditions"] = constraints
            for constraint in constraints:
                constraint.update(context=price_context, url=url, document_id=document_id, source_hash=source_hash, retrieved_at=document.get("retrieved_at"))
    if source_issues:
        offer["source_issues"] = source_issues
        offer["validation"]["price_reason"] = " ".join(source_issues)
    if values.get("image_url"):
        offer["image_url"] = values["image_url"]
        offer["image_source_url"] = url
    relevance = candidate.get("relevance")
    if isinstance(relevance, dict) and isinstance(relevance.get("verdict"), str) and relevance["verdict"] in {"match", "mismatch", "uncertain"} and isinstance(relevance.get("reason"), str):
        offer["relevance"] = {"verdict": relevance["verdict"], "reason": relevance["reason"][:700],
                              "basis": "model_assessment"}
    table_blocks = [block for block in dom_contexts if block.get("variant")]
    if offer["price"] is not None and table_blocks:
        target = requested_variant_mass(request)
        variants = {(block["variant"]["sku"], block["variant"]["packing"]) for block in table_blocks}
        if len(variants) > 1 and target:
            table_blocks = [block for block in table_blocks if _decimal(block["variant"]["mass_mcg"]) == target[0]]
        matching = [block for block in table_blocks if block["amount"] == _decimal(offer["price"])]
        if (len(variants) > 1 and not target) or len({(block["variant"]["sku"], block["variant"]["packing"]) for block in matching}) != 1:
            reason = "В основной таблице несколько артикулов или фасовок; однозначная строка для позиции не установлена."
            offer["price"] = None
            offer["evidence"].pop("price", None)
            offer["source_issues"] = list(dict.fromkeys((offer.get("source_issues") or []) + [reason]))
            offer["validation"]["price_reason"] = reason
            offer["validation"]["rejected_fields"] = sorted(set(offer["validation"]["rejected_fields"] + ["price"]))
        else:
            variant = matching[0]["variant"]
            offer["price_variant"] = dict(variant)
            if target and _requirement_match(variant["mass_mcg"], target[0], target[1], "mass_mcg") is False:
                operator = {"gte": "не менее", "lte": "не более", "eq": "равно", "gt": "больше", "lt": "меньше"}.get(target[1], target[1])
                amount = format(target[0].normalize(), "f").replace(".", ",")
                reason = f"Цена относится к фасовке «{variant['packing']}» (артикул {variant['sku']}); требование ТЗ — {operator} {amount} мкг на единицу. Пересчёт в другую фасовку не выполнялся."
                offer["relevance"] = {"verdict": "mismatch", "reason": reason, "basis": "source_variant_comparison"}
                offer["source_issues"] = list(dict.fromkeys((offer.get("source_issues") or []) + [reason]))
    from .seller_terms import enrich_offer
    return enrich_exact_model_facts(enrich_offer(offer, request, document), document, fact_documents, request)


def _accepted_model_mismatch(request, offer):
    """Resolve a model mismatch only through exact, explicit accepted deviations.

    Category identity, ambiguous source variants and unknown functional fit are
    independent blockers. The source values and original assessment survive in
    the returned offer; only the internal arithmetic input omits this redundant
    model assessment when its deterministic contradictions are all accepted.
    """
    if not isinstance(offer, dict) or offer.get("source_issues"):
        return None
    assessment = offer.get("relevance")
    if not isinstance(assessment, dict) or assessment.get("basis") != "model_assessment" or assessment.get("verdict") != "mismatch":
        return None
    neutral = deepcopy(offer)
    neutral.pop("relevance", None)
    if not positive_product_identity(request, neutral):
        return None
    fields = []
    attributes = offer.get("attributes") if isinstance(offer.get("attributes"), dict) else {}
    for requirement in request.get("requirements") or []:
        if not isinstance(requirement, dict) or not requirement.get("key"):
            continue
        key = requirement["key"]
        actual = attributes.get(key)
        if actual is not None and _requirement_match(actual, requirement.get("value"), requirement.get("op", "eq"), key) is False:
            fields.append("attributes." + key)
    fields.extend("conditions." + check["key"] for check in _condition_checks(request, offer) if check["status"] == "mismatch")
    return sorted(set(fields)) if fields and all(_manual_acceptance(offer, field) for field in fields) else None


def finalize(request, offers, policy=None, excluded_ids=None):
    """Apply manual exclusions before arithmetic; reset replays original inputs."""
    if not isinstance(request, dict) or not isinstance(offers, list):
        raise ValueError("Нужны потребность и список предложений.")
    policy = deepcopy(policy or {})
    source_originals = deepcopy(offers)
    originals = deepcopy(offers)
    accepted_assessments = {}
    for index, raw in enumerate(originals):
        accepted = _accepted_model_mismatch(request, raw)
        if accepted:
            accepted_assessments[index] = {"assessment": raw.pop("relevance"), "fields": accepted}
    excluded = {str(identifier) for identifier in (excluded_ids or [])}
    known = {str(offer.get("id")) for offer in originals if isinstance(offer, dict)}
    excluded &= known
    baseline = analyze_offers(request, originals, policy)
    assessments = {}
    for index, raw in enumerate(originals):
        assessment = raw.get("relevance") if isinstance(raw, dict) else None
        if isinstance(assessment, dict) and isinstance(assessment.get("verdict"), str) and assessment["verdict"] in {"mismatch", "uncertain"}:
            assessments[str(baseline["offers"][index]["id"])] = assessment
    active = []
    for index, raw in enumerate(originals):
        identifier = str(baseline["offers"][index]["id"])
        if identifier in excluded or identifier in assessments:
            continue
        # Preserve the deterministic ID if an earlier removed record changes
        # positions of malformed/incomplete input records in the engine.
        offer = deepcopy(raw) if isinstance(raw, dict) else {"title": "Некорректная запись"}
        offer.setdefault("id", identifier)
        active.append(offer)
    result = analyze_offers(request, active, policy) if excluded or assessments else baseline
    by_id = {str(offer["id"]): offer for offer in result["offers"]}
    merged = []
    for index, initial in enumerate(baseline["offers"]):
        identifier = str(initial["id"])
        if identifier in excluded:
            offer = deepcopy(initial)
            offer.update(status="excluded", manual_excluded=True, base_status=initial["status"],
                         reasons=["Исключено пользователем; предложение не участвует в расчёте."] + initial["reasons"])
        elif identifier in assessments:
            assessment = assessments[identifier]
            offer = deepcopy(initial)
            status = "excluded" if initial["status"] == "excluded" or assessment["verdict"] == "mismatch" else "uncertain"
            reason = ("Сравнение фасовки по источнику: " if assessment.get("basis") == "source_variant_comparison" else "Оценка модели: ") + ("функциональное несоответствие" if assessment["verdict"] == "mismatch" else "функциональное соответствие не установлено")
            offer.update(status=status, manual_excluded=False, base_status=initial["status"],
                         reasons=[reason + ". " + str(assessment.get("reason") or "")[:700]] + initial["reasons"])
        else:
            offer = by_id[identifier]
            offer["manual_excluded"] = False
            assessment = offer.get("relevance")
            if isinstance(assessment, dict) and assessment.get("verdict") == "match":
                offer["reasons"].append("Оценка функционального соответствия моделью: " + str(assessment.get("reason") or "")[:700])
        if index in accepted_assessments:
            resolution = accepted_assessments[index]
            offer["relevance"] = deepcopy(resolution["assessment"])
            offer["relevance_acceptance"] = {"basis": "manual_deviation", "fields": resolution["fields"],
                "reason": "Явные отклонения приняты пользователем; исходная оценка модели и значения источника сохранены."}
        merged.append(offer)
    result["offers"] = merged
    result["calculation"]["provisional"] = provisional_calculation(request, merged, policy.get("min_suppliers", 3))
    result["base_result"] = {"request": deepcopy(request), "offers": source_originals, "policy": policy}
    result["excluded_ids"] = sorted(excluded)
    result["review"] = {"excluded_ids": sorted(excluded)}
    sellers, models = {}, {}
    name_inns = {}
    for offer in merged:
        identity = _verified_identity(offer)
        if identity:
            name_inns.setdefault(_supplier_name(offer), set()).add(identity)
    for offer in merged:
        name = _supplier_name(offer)
        possible_ids = name_inns.get(name, set())
        identity = _verified_identity(offer) or (next(iter(possible_ids)) if len(possible_ids) == 1 else None)
        seller_key = str(offer.get("attributes", {}).get("supplier_group") or identity or name or "unknown:" + offer["id"])
        quoted_model = offer.get("attributes", {}).get("model")
        model = quoted_model or offer.get("title") or "Не установлена"
        model_key = (_model(model) if quoted_model else _space(model).casefold()) or _space(model).casefold()
        for groups, key, label, basis in ((sellers, seller_key, offer.get("supplier") or "Не установлен", "inn" if identity else "name"),
                                           (models, model_key, model, "quoted_model" if offer.get("attributes", {}).get("model") else "title")):
            group = groups.setdefault(key, {"id": hashlib.sha256(key.encode()).hexdigest()[:16], "label": label,
                                           "basis": basis, "offer_ids": [], "included_count": 0})
            group["offer_ids"].append(offer["id"])
            group["included_count"] += int(offer["status"] == "included")
    result["groups"] = {"sellers": list(sellers.values()), "models": list(models.values())}
    critical = ["price", "currency", "unit", "tax"] + ["attributes." + item["key"] for item in request.get("requirements", []) if isinstance(item, dict) and item.get("key")]
    required_count = len(merged) * len(critical)
    verified = sum(1 for offer in merged for field in critical
                   if isinstance(offer.get("evidence", {}).get(field), dict) and offer["evidence"][field].get("quote") and offer["evidence"][field].get("url"))
    result["metrics"] = {"offers_total": len(merged), "accepted_count": sum(offer["status"] == "included" for offer in merged),
                         "excluded_count": sum(offer["status"] == "excluded" for offer in merged),
                         "uncertain_count": sum(offer["status"] == "uncertain" for offer in merged),
                         "manual_excluded_count": len(excluded), "duplicates": sum(bool(offer.get("duplicate_of")) for offer in merged),
                         "seller_groups": len(sellers), "model_groups": len(models),
                         "evidence_fields_verified": verified, "evidence_fields_required": required_count,
                         "evidence_coverage": round(verified / required_count, 4) if required_count else None}
    return result

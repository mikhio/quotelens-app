"""Deterministic comparison and arithmetic; no network or model calls.

Prices are converted with Decimal. CV is expressed as a percentage. This
module implements a configurable calculation profile; results require specialist review.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .source_policy import source_metadata


LABELS = {
    "brand": "Бренд",
    "channels": "Число каналов", "bandwidth_mhz": "Полоса пропускания",
    "format": "Формат бумаги", "density_gsm": "Плотность бумаги",
    "sheets_per_pack": "Листов в пачке", "model": "Модель",
    "service_type": "Вид услуги",
}
PROFILE = "Среднее сопоставимых цен с НДС, RUB; минимум {minimum} разных поставщиков. Доставка не включена."


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        val = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        return val if val.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _number(value, places=None):
    if value is None:
        return None
    if places is not None:
        value = value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
    return float(value)


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _model(value):
    value = _clean(value).replace("акип", "akip").replace("актаком", "aktakom")
    value = value.translate(str.maketrans({"в": "v", "а": "a", "с": "c", "–": "-", "—": "-"}))
    return re.sub(r"[^a-z0-9/]", "", value)


def _extract_model(text):
    match = re.search(r"(?:АКИП|AKIP)[\s-]*\d{4}(?:/\d+[А-ЯA-Z]?)?|\bADS[\s-]*\d{4}\b", str(text), re.I)
    return match.group(0) if match else None


def _unit(value):
    text = _clean(value).rstrip(".").replace("²", "2")
    aliases = {
        "шт": "piece", "штука": "piece", "штук": "piece", "штуки": "piece", "piece": "piece",
        "пачка": "pack", "пачек": "pack", "пачки": "pack", "уп": "pack", "упак": "pack",
        "упаковка": "pack", "pack": "pack", "коробка": "box", "короб": "box", "box": "box",
        "лист": "sheet", "листы": "sheet", "листов": "sheet", "sheet": "sheet",
        "м2": "m2", "кв.м": "m2", "кв. м": "m2", "m2": "m2", "m²": "m2",
        "час": "hour", "ч": "hour", "hour": "hour", "услуга": "service", "service": "service",
    }
    return aliases.get(text, text)


def _comparison_op(prefix):
    if re.search(r"не\s*менее|минимум|\bот\s*$|>=|≥", prefix):
        return "gte"
    if re.search(r"не\s*более|максимум|\bдо\s*$|<=|≤", prefix):
        return "lte"
    return "eq"


def _toleranced_number(match):
    """Keep an explicitly written tolerance instead of reading its delta as value."""
    nominal = Decimal(match["nominal"].replace(",", "."))
    if match.groupdict().get("delta"):
        delta = Decimal(match["delta"].replace(",", "."))
        if match.groupdict().get("percent"):
            delta = nominal * delta / 100
        return [float(nominal - delta), float(nominal + delta)], "between"
    return float(nominal), "eq"


def _bandwidth_match(text):
    """Bind a frequency to its attribute; do not confuse sampling and bandwidth."""
    frequency = r"(не\s+менее\s+|не\s+более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+(?:[.,]\d+)?)\s*(мгц|mhz|ггц|ghz)\b"
    label = r"\b(?:полос[аыу](?:\s+пропускания)?|bandwidth)\s*(?:[:=–—-]\s*)?"
    all_frequencies = list(re.finditer(frequency, text))
    labelled = list(re.finditer(label + frequency, text))
    other_attribute = r"частот[аыу]|дискретизац|sampling|sample\s+rate|тактов|генератор|процессор"
    if len(labelled) == 1:
        chosen = labelled[0]
        for index, candidate in enumerate(all_frequencies):
            if chosen.start() <= candidate.start() and candidate.end() <= chosen.end():
                continue
            # A second value is safe only when it explicitly belongs to another
            # attribute. Bare alternatives or bounds are ambiguous without further evidence.
            previous_end = all_frequencies[index - 1].end() if index else 0
            clause = re.split(r"[,;\n]", text[previous_end:candidate.start()])[-1]
            if not re.search(other_attribute, clause):
                return None
        return chosen
    if not labelled and len(all_frequencies) == 1 and not re.search(other_attribute, text):
        return all_frequencies[0]
    return None


def parse_request(text: str, quantity: float = 1) -> dict:
    """Extract supported explicit constraints; never fill in missing hard specs."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Опишите товар или услугу.")
    amount = _decimal(quantity)
    if amount is None or amount <= 0:
        raise ValueError("Количество должно быть положительным числом.")
    description = text.strip()
    lower = _clean(description)
    request = {"description": description, "category": "other", "product": description,
               "quantity": float(amount), "unit": "шт", "requirements": [], "search_terms": [description]}
    requirements = request["requirements"]

    def add(key, value, unit=None, op="eq"):
        requirements.append({"key": key, "label": LABELS.get(key, key), "op": op, "value": value, "unit": unit})

    # Keep the owner's wording as evidence, while a common single-л typo
    # uses the same supported category and useful corrected search terms.
    oscilloscope_text = re.sub(r"осцилограф", "осциллограф", lower)
    if "осциллограф" in oscilloscope_text or _extract_model(text):
        request.update(category="oscilloscope", product="Осциллограф цифровой")
        if oscilloscope_text != lower:
            request["search_terms"] = [re.sub(r"осцилограф", "осциллограф", description, flags=re.I)]
        model = _extract_model(text)
        if model:
            request["product"] = model
            add("model", model)
        channel = re.search(r"(не\s+менее\s+|не\s+более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+)\s*[- ]?\s*канал", lower)
        if not channel:
            channel = re.search(r"(?:число|количество)\s+каналов\s*[:=]?\s*(не\s+менее\s+|не\s+более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+)", lower)
        if channel:
            add("channels", int(channel.group(2)), "канала", _comparison_op(channel.group(1) or ""))
        bandwidth = _bandwidth_match(lower)
        if bandwidth:
            value = Decimal(bandwidth.group(2).replace(",", "."))
            if bandwidth.group(3) in ("ггц", "ghz"):
                value *= 1000
            add("bandwidth_mhz", float(value), "МГц", _comparison_op(bandwidth.group(1) or ""))
        request["missing_critical_fields"] = [key for key in ("channels", "bandwidth_mhz") if not any(r["key"] == key for r in requirements)]
    elif "бумаг" in lower:
        request.update(category="paper", product="Бумага офисная", unit="пачка")
        match = re.search(r"(?<![a-zа-я0-9])[aа]\s*([0-6])(?!\d)", lower)
        if match:
            add("format", "A" + match.group(1))
        match = re.search(r"(?P<nominal>\d+(?:[.,]\d+)?)\s*(?:[±]\s*(?P<delta>\d+(?:[.,]\d+)?)\s*(?P<percent>%?)\s*)?(?:г\s*/\s*м\s*[²2]|г\s*/\s*кв\.?\s*м|gsm)", lower)
        if match:
            value, op = _toleranced_number(match)
            add("density_gsm", value, "г/м²", op)
        match = re.search(r"(?P<nominal>\d+)\s*лист", lower)
        if match and re.search(r"пачк|упак", lower):
            add("sheets_per_pack", int(match["nominal"]), "листов")
        else:
            # Common technical specifications put the label before the value.
            match = re.search(r"(?:количество\s+)?листов\s+в\s+(?:пачке|упаковке)\s*[:=]?\s*(?P<nominal>\d+)\s*(?:\(?\s*±\s*(?P<delta>\d+(?:[.,]\d+)?)\s*(?P<percent>%?)\s*\)?)?", lower)
            if match:
                value, op = _toleranced_number(match)
                add("sheets_per_pack", value, "листов", op)
        if re.search(r"за\s+лист|единица\s*[:=]?\s*лист", lower):
            request["unit"] = "лист"
        request["missing_critical_fields"] = [key for key in ("format", "density_gsm", "sheets_per_pack") if not any(r["key"] == key for r in requirements)]
    elif re.search(r"уборк|клининг", lower):
        request.update(category="service", product="Уборка помещений", unit="м²")
        add("service_type", "уборка помещений")
        request["missing_critical_fields"] = []
        # Explicitly selected hourly/project service units are preserved.
        if re.search(r"за\s+час|почасов", lower):
            request["unit"] = "час"
        elif "за услугу" in lower:
            request["unit"] = "услуга"
    else:
        request["missing_critical_fields"] = ["поддерживаемая категория и её обязательные характеристики"]
    return request


def _has_evidence(offer, field):
    """Only an unchanged source value can be described as source-confirmed."""
    manual = (offer.get("manual_confirmations") or {}).get(field)
    if isinstance(manual, dict) and manual.get("value") != manual.get("original_value"):
        return False
    evidence = offer.get("evidence") if isinstance(offer.get("evidence"), dict) else {}
    item = evidence.get(field)
    return isinstance(item, dict) and bool(str(item.get("quote") or "").strip()) and bool(str(item.get("url") or "").strip())


def _manual_confirmation(offer, field):
    confirmation = (offer.get("manual_confirmations") or {}).get(field)
    if not isinstance(confirmation, dict) or confirmation.get("kind") != "manual":
        return None
    if field.startswith("attributes.") or field.startswith("conditions."):
        namespace, key = field.split(".", 1)
        value = (offer.get(namespace) or {}).get(key)
    else:
        value = offer.get(field)
    return confirmation if value == confirmation.get("value") and confirmation.get("reason") and isinstance(confirmation.get("revision"), int) else None


def _manual_acceptance(offer, field):
    """A decision waives one requested deviation without changing source facts."""
    if not isinstance(field, str) or not field.startswith(("attributes.", "conditions.")):
        return None
    acceptances = offer.get("manual_acceptances")
    acceptance = acceptances.get(field) if isinstance(acceptances, dict) else None
    if (not isinstance(acceptance, dict) or not isinstance(acceptance.get("reason"), str) or not acceptance["reason"].strip()
            or not isinstance(acceptance.get("revision"), int) or isinstance(acceptance.get("revision"), bool)
            or not isinstance(acceptance.get("at"), str) or not acceptance["at"].strip()):
        return None
    return acceptance


def _field_confirmed(offer, field):
    return bool(_manual_confirmation(offer, field) or _has_evidence(offer, field))


_PRINT_3D = re.compile(
    r"(?<![a-zа-яё0-9])(?:3\s*[- ]?\s*[dд]|тр[её]хмерн\w*)[-\s]*(?:принтер\w*|printer\b)"
    r"|\b(?:принтер\w*|printer)\s+(?:для\s+)?3\s*[- ]?\s*[dд]\b"
    r"|\b(?:fdm|fff|sla|sls|dlp|msla)[-\s]+(?:3d[-\s]+)?printer\b"
    r"|\bаддитивн\w*\s+(?:установк\w*|систем\w*)"
    r"|\bустановк\w*\s+(?:для\s+)?3\s*[- ]?\s*[dд][-\s]*печат\w*", re.I)
_PRINT_PART = re.compile(
    r"^\s*(?:(?:запасн\w*|сменн\w*|совместим\w*|оригинальн\w*)\s+)*"
    r"(?:сопл\w*|экструдер\w*|филамент\w*|кабел\w*|адаптер\w*|чехол\w*|футляр\w*|"
    r"запчаст\w*|аксессуар\w*|модул\w*|ремень\w*|катушк\w*|пластик\w*|расходн\w*\s+материал\w*|"
    r"(?:набор|комплект)\s+(?:сопел|кабелей|запчастей)|nozzle\w*|filament\w*|spare\s+parts?)\b", re.I)
_PEN_3D = re.compile(r"\b3\s*[- ]?\s*[dд][-\s]*(?:ручк\w*|pen\b)|\bручк\w*[-\s]+3\s*[- ]?\s*[dд]\b", re.I)
_ENGRAVER = re.compile(r"\b(?:грав[её]р\w*|гравиров\w*|engraver\w*|engraving\s+machine|laser\s+cutter)\b", re.I)
_OFFICE_PRINTER = re.compile(r"\b(?:(?:струйн\w*|лазерн\w*|термо\w*|матричн\w*)\s+принтер\w*|мфу|laserjet|deskjet)\b", re.I)
_NAMED_PRINTER_MODEL = re.compile(
    r"(?<!\w)([a-zа-яё][a-zа-яё0-9]*\d[a-zа-яё0-9]*(?:[-_/][a-zа-яё0-9]+)*)"
    r"(?:[\s-]+((?:pro|plus|mini|max|ultra|hs|про)(?:[\s-]*\d+)?(?:[\s-]*hs)?))?(?!\w)", re.I)


def _named_printer_models(text):
    models = []
    for match in _NAMED_PRINTER_MODEL.finditer(str(text or "")):
        value = match[1] + (match[2] or "")
        if re.fullmatch(r"[23]\s*[- ]?\s*[dд]", value, re.I):
            continue
        value = _clean(value).replace("про", "pro")
        value = re.sub(r"д(?=\d)", "d", value)
        value = value.translate(str.maketrans({"а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "т": "t", "х": "x"}))
        models.append(re.sub(r"[^a-z0-9]", "", value))
    return models


def _printer_models_match(expected, actual):
    # A model can be attached to its manufacturer in a headline. A digit or
    # suffix after the requested designation is a different named variant.
    return any(a == e or a.endswith(e) and a[:-len(e)].isalpha() or e.endswith(a) and e[:-len(a)].isalpha()
               for e in expected for a in actual if e and a)


def _printer_object_kind(title):
    """Identify only explicit named objects, not arbitrary missing features."""
    title = str(title or "")
    if _PRINT_PART.search(title):
        return "accessory", "принадлежность или расходный материал для принтера"
    # A multifunction printer can also have engraving tools. The printer
    # noun wins unless the headline explicitly names a part for that printer.
    printer = _PRINT_3D.search(title)
    if printer and re.search(r"\bдля\b|\bfor\b|\bсовместим\w*", title[:printer.start()], re.I):
        return "accessory", "принадлежность или расходный материал для принтера"
    if printer:
        return "printer_3d", "3D-принтер"
    if _PEN_3D.search(title):
        return "pen_3d", "3D-ручка"
    if _ENGRAVER.search(title):
        return "engraver", "гравировальный станок"
    if _OFFICE_PRINTER.search(title):
        return "office_printer", "офисный или этикеточный принтер"
    return None, None


def product_identity_assessment(request, offer):
    """Separate a proven different object from an incomplete matching card.

    Hard type contradictions use the unchanged, quoted primary headline only.
    The request, recommendation text and accepted manufacturer/color deviations
    cannot supply source identity or turn an engraver into a printer.
    """
    request = request if isinstance(request, dict) else {}
    offer = offer if isinstance(offer, dict) else {}
    subject = str(request.get("product") or request.get("title") or request.get("description") or "").split("\n", 1)[0]
    title = str(offer.get("title") or "")
    proof = (offer.get("evidence") or {}).get("title") if _has_evidence(offer, "title") else None
    quoted = bool(proof and _clean(title) and _clean(title) in _clean(proof.get("quote")))
    expected_kind, expected_label = _printer_object_kind(subject)
    actual_kind, actual_label = _printer_object_kind(title) if quoted else (None, None)
    check = {"status": "unconfirmed", "expected": subject, "actual": title,
             "reason": "Соответствие предмету закупки не подтверждено источником.",
             "basis": "primary_title" if quoted else "unconfirmed_identity",
             "evidence": dict(proof) if quoted else None}
    # Broad paper/oscilloscope/service requests can mention a printer as a use
    # case. They retain their own existing identity rules.
    printer_request = request.get("category") not in {"paper", "oscilloscope", "service"} and expected_kind == "printer_3d"
    if printer_request and actual_kind and actual_kind != "printer_3d":
        check.update(status="mismatch", expected=expected_label,
                     reason=f"Предмет закупки — {expected_label}; в источнике указан {actual_label}. Это другой тип товара.")
        return check
    assessment = offer.get("relevance") if isinstance(offer.get("relevance"), dict) else {}
    named = _named_printer_models(subject) if printer_request else []
    for row in request.get("requirements") or []:
        if isinstance(row, dict) and row.get("key") in {"model", "product_name", "article", "sku"}:
            named.extend(_named_printer_models(row.get("value")))
    actual_models = _named_printer_models(title) if printer_request and quoted else []
    equivalence = bool(re.search(r"\bили\s+(?:эквивалент\w*|аналог\w*)|\bor\s+equivalent\b|допуска\w*\s+(?:эквивалент\w*|аналог\w*)",
                                 subject + "\n" + str(request.get("description") or ""), re.I))
    model_matches = bool(named and actual_models and _printer_models_match(named, actual_models))
    if printer_request and named and actual_models and not model_matches and not equivalence:
        check.update(status="mismatch", reason="Модель в источнике отличается от явно названной модели закупки; эквивалент не разрешён в запросе.")
        return check
    same_printer_type = (printer_request and actual_kind == "printer_3d" and (not named or model_matches or equivalence)
                         and assessment.get("verdict") != "mismatch")
    if subject.strip() and (same_printer_type or _positive_product_identity(request, offer)):
        check.update(status="matches", reason="Тип и предмет товара установлены; характеристики и условия проверяются отдельно.")
    return check


def positive_product_identity(request, offer):
    return product_identity_assessment(request, offer)["status"] == "matches"


def _positive_product_identity(request, offer):
    """Require a positive object match; absent attributes cannot establish it.

    Known broad classes retain their existing type checks. Generic structured
    lines instead need a quoted product/model anchor or a quoted object name.
    This is deliberately separate from specification completeness and price:
    a matching product with missing terms can remain conditional, while an
    unrelated or unidentified card remains available only for human review.
    """
    if not isinstance(request, dict) or not isinstance(offer, dict):
        return False
    relevance = offer.get("relevance") if isinstance(offer.get("relevance"), dict) else {}
    if relevance.get("verdict") == "mismatch":
        return False
    title = str(offer.get("title") or "")
    category = request.get("category")
    if category == "oscilloscope":
        noun = re.search(r"осцил{1,2}ограф", title, re.I)
        return bool(noun and not re.search(r"щуп|пробник|аксессуар|кабель|адаптер|футляр|кейс", title[:noun.start()], re.I))
    if category == "paper":
        return bool(re.search(r"\bбумаг[аи]\b", title, re.I))
    if category == "service":
        return bool(re.search(r"уборк|клининг", title, re.I) or (
            _has_evidence(offer, "attributes.service_type") and
            _clean((offer.get("attributes") or {}).get("service_type")) == "уборка помещений"))

    evidence = offer.get("evidence") if isinstance(offer.get("evidence"), dict) else {}
    title_proof = evidence.get("title") if _has_evidence(offer, "title") else None
    title_is_quoted = bool(title_proof and _clean(title) in _clean(title_proof.get("quote")))
    attributes = offer.get("attributes") if isinstance(offer.get("attributes"), dict) else {}
    model = attributes.get("model")
    model_proof = evidence.get("attributes.model") if _has_evidence(offer, "attributes.model") else None
    model_is_quoted = bool(model and model_proof and _model(model) in _model(model_proof.get("quote")) and _model(model) in _model(title))
    if not (title_is_quoted or model_is_quoted):
        return False

    subject = str(request.get("product") or request.get("title") or request.get("description") or "").splitlines()[0]
    if not subject.strip():
        return False
    identity_fields = {"model", "product_name", "substance", "substance_name", "protein", "catalog_number", "article", "sku"}
    explicit_anchors = []
    for requirement in request.get("requirements") or []:
        if not isinstance(requirement, dict) or requirement.get("key") not in identity_fields:
            continue
        expected = requirement.get("value")
        if isinstance(expected, str) and expected.strip():
            explicit_anchors.append(expected)
    anchors = explicit_anchors or [word for word in re.findall(r"[a-zа-яё0-9]+(?:[-_/][a-zа-яё0-9]+)*", subject, re.I)
                                   if re.search(r"[a-zа-яё]", word, re.I) and re.search(r"\d", word)
                                   and _clean(word) not in {"2d", "3d", "a3", "a4", "a5", "а3", "а4", "а5", "м2", "м3"}]
    source_tokens = [_model(word) for word in re.findall(r"[a-zа-яё0-9]+(?:[-_/][a-zа-яё0-9]+)*", title, re.I)]

    def anchored(value):
        token = _model(value)
        if not token:
            return False
        if re.search(r"\s", str(value).strip()) and token in _model(title):
            return True
        return any(actual == token or actual.startswith(token) and actual[len(token):len(token)+1].isalpha()
                   for actual in source_tokens)

    # A named model or reagent is a stronger requirement than generic words
    # such as "оборудование" or universal electrical characteristics.
    if anchors and not any(anchored(value) for value in anchors):
        return False
    accessory = re.search(r"\b(?:аксессуар\w*|запчаст\w*|филамент\w*|кабель\w*|адаптер\w*|футляр\w*|чехол\w*)\b", _clean(title))
    if accessory and accessory.group() not in _clean(subject):
        return False
    ignored = {"товар", "товара", "поставка", "поставку", "поставки", "закупка", "закупки", "оборудование", "оборудования", "система", "системы", "комплекс", "комплект", "набор", "новый", "новая", "производитель", "страна", "происхождения", "лабораторный", "лабораторная", "лабораторное", "профессиональный", "цифровой", "ручной", "электронный", "рекомбинантный", "человека", "белок"}
    words = [word for word in re.findall(r"[a-zа-яё]{4,}", _clean(subject)) if word not in ignored]
    shared = next((word for word in words if re.search(r"(?<![a-zа-яё])" + re.escape(word) + r"[a-zа-яё]*(?![a-zа-яё])", _clean(title))), None)
    if shared:
        prefix = _clean(title).split(shared, 1)[0]
        if re.search(r"\bдля\b|\bсовместим\w*|\bаксессуар\w*|\bзапчаст\w*", prefix):
            return False
    if anchors:
        return True
    # An explicit relevant assessment can bridge alternate names, but only
    # after a quoted primary identity exists. A missing assessment is unknown.
    return bool(shared or relevance.get("verdict") == "match" and str(relevance.get("reason") or "").strip())


def _requirement_match(actual, expected, op, key):
    if op not in {"eq", "gte", "lte", "gt", "lt", "in", "contains", "between", "=", ">=", "<="}:
        return None
    if isinstance(actual, (list, tuple)) and len(actual) == 2:
        low, high = map(_decimal, actual)
        if low is not None and high is not None and low <= high:
            if op == "between":
                if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                    return None
                required_low, required_high = map(_decimal, expected)
                if required_low is None or required_high is None or required_low > required_high:
                    return None
                return True if required_low <= low <= high <= required_high else False if high < required_low or low > required_high else None
            scalar = _decimal(expected)
            if scalar is not None:
                if op in {"eq", "="}:
                    return True if low == high == scalar else False if scalar < low or scalar > high else None
                if op in {"gte", ">="}:
                    return True if low >= scalar else False if high < scalar else None
                if op in {"lte", "<="}:
                    return True if high <= scalar else False if low > scalar else None
                if op == "gt":
                    return True if low > scalar else False if high <= scalar else None
                if op == "lt":
                    return True if high < scalar else False if low >= scalar else None
    if op == "between":
        if not isinstance(expected, (list, tuple)) or len(expected) != 2:
            return None
        actual, low, high = _decimal(actual), _decimal(expected[0]), _decimal(expected[1])
        return low <= actual <= high if actual is not None and low is not None and high is not None and low <= high else None
    if op == "in":
        return _clean(actual) in [_clean(x) for x in expected] if isinstance(expected, (list, tuple)) else None
    if op == "contains":
        return _clean(expected) in _clean(actual)
    if key == "model":
        return _model(actual) == _model(expected) if op in ("eq", "=") else None
    if key == "format" and op in ("eq", "="):
        return _clean(actual).replace("а", "a").replace(" ", "") == _clean(expected).replace("а", "a").replace(" ", "")
    if key == "paper_grade" and op in {"eq", "="}:
        # Paper quality class uses the same printed A/B/C glyphs in Russian
        # and Latin catalogues. Do not transliterate arbitrary product facts.
        grade = lambda v: _clean(v).translate(str.maketrans({"а": "a", "в": "b", "с": "c"})).strip(' «»"')
        return grade(actual) == grade(expected)
    a, b = _decimal(actual), _decimal(expected)
    if a is not None and b is not None:
        return {"eq": a == b, "=": a == b, "gte": a >= b, ">=": a >= b,
                "lte": a <= b, "<=": a <= b, "gt": a > b, "lt": a < b}[op]
    if op in ("eq", "="):
        presence = re.fullmatch(r"(.+?)\s*[-–—:]\s*наличие", _clean(expected))
        if presence:
            token = presence.group(1).strip()
            for occurrence in re.finditer(r"(?<![\w])" + re.escape(token) + r"(?![\w])", _clean(actual)) if token else []:
                before = _clean(actual)[max(0, occurrence.start() - 45):occurrence.start()]
                after = _clean(actual)[occurrence.end():occurrence.end() + 45]
                negative_before = re.search(r"(?:\bбез|\bнет|\bотсутств\w*|\bno|\bwithout|not\s+support\w*|не\s+(?:имеет|предусмотрен\w*|поддержива\w*))\s*$", before)
                negative_after = re.match(r"\s*[:–—-]?\s*(?:нет\b|отсутств\w*|not\s+support\w*|не\s+(?:поддержива\w*|предусмотрен\w*|имеется\b))", after)
                if not negative_before and not negative_after:
                    return True
            return False
        return _clean(actual) == _clean(expected)
    return None


def _material_entries(text):
    value = re.sub(r"\s+и\s+(?:другие|др\.?)\s*$", "", _clean(text))
    aliases = {"flexible": "flex", "nylon (pa)": "nylon", "nylon(pa)": "nylon", "pa (nylon)": "nylon"}
    return {aliases.get(part.strip(), part.strip()) for part in re.split(r"[,;]", value) if part.strip()}


def _semantic_attribute_match(actual, expected, requirement, proof):
    """Narrow capability comparisons; an unlisted capability stays unknown."""
    if not isinstance(actual, str) or not isinstance(expected, str) or requirement.get("op", "eq") not in {"eq", "="}:
        return False, None
    label = _clean(requirement.get("label") or requirement.get("key") or "")
    value, needed = _clean(actual), _clean(expected)
    if re.search(r"\b(?:не|нет|без|not|no|without|unsupported|только|исключительно|only)\b", value + " " + needed):
        return False, None  # Do not turn a negated/exclusive clause into a positive subset.
    if re.search(r"диаметр.*соп|nozzle.*diameter", label):
        from .price_context import _measure_unit, _observed_units
        units = _observed_units(actual + " " + str(proof.get("raw_label") or ""))
        target = _measure_unit(requirement.get("unit") or "мм")
        if not units or len(units) != 1 or target is None or next(iter(units))[:2] != target[:2]:
            return True, None
        if re.search(r"\d\s*[-–—]\s*\d", actual):
            return True, None
        numbers = lambda text: {_decimal(token) for token in re.findall(r"(?<![\d.])\d+(?:[.,]\d+)?(?!\d)", text)}
        required, available = numbers(expected), numbers(actual)
        return True, True if required and required <= available else None
    if re.search(r"поддерживаемые.*(?:пластики|материалы)|расходные материалы|supported materials", label):
        required, available = _material_entries(expected), _material_entries(actual)
        return True, True if required and required <= available else None
    if re.search(r"поддержка форматов|форматы файлов|file formats|supported formats", label):
        tokens = lambda text: set(re.findall(r"[a-z][a-z0-9]{1,8}", text, re.I))
        required, available = tokens(needed), tokens(value)
        return True, True if required and required <= available else None
    if re.search(r"материал.*платформ|печатная платформа|build plate material", label) and " или " in needed:
        alternative = needed.rsplit(" или ", 1)[1].strip()
        families = ((r"стекл|glass", r"стекл|glass"), (r"алюмини|alumin", r"алюмини|alumin"),
                    (r"сталь|steel", r"сталь|steel"), (r"керамик|ceramic", r"керамик|ceramic"))
        if len(alternative.split()) <= 3 and any(re.search(a, alternative) and re.search(b, value) for a, b in families):
            return True, True
        return True, True if needed == value else None
    if re.search(r"электропитание|источник питания|power supply", label):
        unit_pattern = r"кв|вольт(?:а|ов)?|в|v|гц|hz"
        pattern = re.compile(r"(?<![\w\d])(?P<lo>\d+(?:[.,]\d+)?)\s*(?:(?P<first>" + unit_pattern + r")\s*)?(?:(?P<sep>[-–—/])\s*(?P<hi>\d+(?:[.,]\d+)?)\s*)?(?P<unit>" + unit_pattern + r")(?![a-zа-яё])", re.I)

        def measures(text):
            result = {}
            for match in pattern.finditer(text):
                unit = match["unit"].casefold()
                kind = "frequency" if unit in {"гц", "hz"} else "voltage"
                factor = 1000 if unit == "кв" else 1
                first_unit = (match["first"] or unit).casefold()
                first_kind = "frequency" if first_unit in {"гц", "hz"} else "voltage"
                first_factor = 1000 if first_unit == "кв" else 1
                lo, hi = _decimal(match["lo"]) * first_factor, _decimal(match["hi"] or match["lo"]) * factor
                if first_kind != kind:
                    result.setdefault(first_kind, []).append((lo, lo, False))
                    result.setdefault(kind, []).append((hi, hi, False))
                    continue
                result.setdefault(kind, []).append((min(lo, hi), max(lo, hi), match["sep"] == "/"))
            return result

        demanded, available = measures(needed), measures(value)
        if not demanded:
            return True, None
        for kind, ranges in demanded.items():
            if kind not in available:
                return True, None
            for lo, hi, _ in ranges:
                if not any(a <= lo <= hi <= b and (not alternatives or lo == hi and lo in {a, b})
                           for a, b, alternatives in available[kind]):
                    return True, False
        return True, True
    return False, None


def _attribute_match(offer, requirement):
    """Compare a source observation without supplying a missing unit from TZ."""
    key = requirement.get("key")
    actual = (offer.get("attributes") or {}).get(key)
    proof = (offer.get("evidence") or {}).get("attributes." + str(key)) or {}
    semantic, comparison = _semantic_attribute_match(actual, requirement.get("value"), requirement, proof)
    if semantic:
        return comparison
    manual = _manual_confirmation(offer, "attributes." + str(key))
    numeric_actual = isinstance(actual, (int, float)) and not isinstance(actual, bool) or (
        isinstance(actual, (list, tuple)) and all(_decimal(value) is not None for value in actual))
    if numeric_actual and not manual and proof.get("quote") and not proof.get("normalization"):
        # Atomic candidate validation checks the quoted number. The arithmetic
        # layer additionally refuses an explicit unit conversion or a collapsed
        # interval without its canonical normalization record. Missing unit
        # metadata is handled below; do not weaken a known hard mismatch.
        if requirement.get("unit"):
            from .price_context import _measure_unit, _observed_units
            target = _measure_unit(requirement["unit"])
            units = _observed_units(str(proof.get("raw_value") or proof["quote"]))
            if target and units and (len(units) != 1 or next(iter(units))[:2] != target[:2]):
                return None
        if (not isinstance(actual, (list, tuple)) and re.search(r"\d(?:[.,]\d+)?\s*[-–—]\s*\d", proof["quote"])
                and not re.search(r"максим|миним|maximum|minimum|\bmax\b|\bmin\b", str(requirement.get("label") or ""), re.I)):
            return None
    if proof.get("comparison_supported") is False and not _manual_confirmation(offer, "attributes." + str(key)):
        return None
    if (key == "manufacturer" and actual is not None and not _manual_confirmation(offer, "attributes.manufacturer")
            and _has_evidence(offer, "attributes.country")
            and _clean(actual) == _clean((offer.get("attributes") or {}).get("country"))):
        return None
    if (not _manual_confirmation(offer, "attributes." + str(key)) and requirement.get("unit")
            and proof.get("unit_reason") == "source_unit_not_stated"):
        return None
    from .price_context import _attribute_concept
    concept = _attribute_concept(requirement.get("label") or str(key))
    compare_key = concept if concept in {"format", "paper_grade"} else key
    return _requirement_match(actual, requirement.get("value"), requirement.get("op", "eq"), compare_key)


def _normalized(offer, request):
    price = _decimal(offer.get("price"))
    if price is None or price <= 0:
        return None, "Цена отсутствует или не является положительным числом."
    if not _manual_confirmation(offer, "price") and any(row.get("applies") is not True for row in offer.get("pricing_conditions", [])):
        return None, "Цена в карточке найдена; применимость минимального количества или доплаты к этой закупке требует подтверждения."
    source_unit, target_unit = _unit(offer.get("unit")), _unit(request.get("unit"))
    if not source_unit or not target_unit:
        return None, "Не указана единица цены или единица потребности."
    if source_unit == target_unit:
        return price, None
    pack = _decimal(offer.get("pack_size"))
    if source_unit in {"pack", "box"} and target_unit in {"piece", "sheet", "pack"}:
        if pack is None or pack <= 0 or pack != pack.to_integral_value():
            return None, "Для пересчёта упаковки не указано положительное целое число единиц в ней."
        if source_unit == "pack" and target_unit == "pack":
            return price, None
        return price / pack, None
    return None, f"Несопоставимые единицы: {offer.get('unit')} и {request.get('unit')}; коэффициент пересчёта не установлен."


def _supplier_name(offer):
    name = re.sub(r"\b(?:ооо|оао|зао|пао|ао|ип|llc|ltd)\b", "", _clean(offer.get("supplier")))
    return re.sub(r"[^a-zа-яё0-9]", "", name)


def _verified_identity(offer):
    attributes = offer.get("attributes") if isinstance(offer.get("attributes"), dict) else {}
    identity = str(attributes.get("supplier_inn") or "").strip()
    if re.fullmatch(r"\d{10}|\d{12}", identity) and _has_evidence(offer, "attributes.supplier_inn"):
        quote = str(offer["evidence"]["attributes.supplier_inn"]["quote"])
        if identity in quote:
            return identity
    return None


def condition_specs(request):
    """Stable distinct identities for different clauses sharing 'warranty'."""
    conditions = request.get("conditions") or []
    conditions = [conditions] if isinstance(conditions, str) else conditions
    keys = [str(row.get("key") or row.get("type") or f"condition_{i+1}") if isinstance(row, dict) else f"condition_{i+1}" for i, row in enumerate(conditions)]
    for index, (key, row) in enumerate(zip(keys, conditions)):
        if keys.count(key) > 1:
            content = {k: row.get(k) for k in ("text", "quote", "value", "op", "unit")} if isinstance(row, dict) else str(row)
            key = key[:40] + "_" + hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        yield index, key, row


def _condition_checks(request, offer):
    """Compare procurement terms only when the supplier actually confirms them."""
    conditions = request.get("conditions") or []
    if isinstance(conditions, str):
        conditions = [conditions]
    checks = []
    for index, key, condition in condition_specs(request):
        if isinstance(condition, dict):
            expected = condition.get("text") or condition.get("quote") or condition.get("value")
            label = condition.get("label") or key
            operator = condition.get("op", "eq")
        else:
            expected, label, operator = str(condition), "Условие ТЗ", "eq"
        if expected is None or not str(expected).strip():
            continue
        lower = _clean(expected)
        if label == "Условие ТЗ":
            label = "Доставка" if "достав" in lower else "Гарантия" if "гарант" in lower else "Срок поставки" if "постав" in lower and re.search(r"срок|дн|течени", lower) else "Документация" if re.search(r"документац|сертифик", lower) else label
        evidence = offer.get("evidence") or {}
        proof = evidence.get("conditions." + key)
        if not isinstance(proof, dict) and "достав" in lower:
            proof = evidence.get("delivery")
        actuals = offer.get("conditions") if isinstance(offer.get("conditions"), dict) else {}
        actual = actuals.get(key)
        manual = _manual_confirmation(offer, "conditions." + key)
        confirmed = bool(manual) or isinstance(proof, dict) and bool(proof.get("quote")) and bool(proof.get("url"))
        status = "unconfirmed"
        if confirmed and actual is not None and isinstance(condition, dict) and "value" in condition:
            matched = _requirement_match(actual, condition["value"], operator, key)
            status = "matches" if matched is True else "mismatch" if matched is False else "unconfirmed"
        elif manual and actual is not None:
            matched = _requirement_match(actual, expected, operator, key)
            status = "matches" if matched is True else "mismatch" if matched is False else "unconfirmed"
        elif confirmed and _clean(expected) in _clean(proof["quote"]):
            status = "matches"
        checks.append({"key": key, "label": str(label), "expected": expected, "actual": actual,
                       "status": status, "evidence": proof if confirmed and not manual else None,
                       "confirmation": deepcopy(manual) if manual else None})
    return checks


def _canonical_url(url):
    try:
        parts = urlsplit(str(url or ""))
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_") and k not in {"yclid", "gclid"}])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
    except ValueError:
        return str(url or "")


def _calculation(request, included, minimum, invalid_request=None):
    prices = [offer["_decimal_price"] for offer in included]
    n = len(prices)
    identities = {offer.get("_supplier_key") or _verified_identity(offer) or _supplier_name(offer) for offer in included} - {None, ""}
    quantity = _decimal(request.get("quantity"))
    rule = PROFILE.format(minimum=minimum)
    if prices:
        mean = sum(prices) / Decimal(n)
        variance = sum((p - mean) ** 2 for p in prices) / (n - 1) if n > 1 else None
        stddev = variance.sqrt() if variance is not None else None
        cv = stddev / mean * 100 if stddev is not None and mean else None
    else:
        mean = stddev = cv = None
    ready = not invalid_request and quantity is not None and quantity > 0 and len(identities) >= minimum
    if invalid_request:
        reason = invalid_request
    elif quantity is None or quantity <= 0:
        reason = "Количество потребности не задано положительным числом."
    elif len(identities) < minimum:
        reason = f"Разных поставщиков: {len(identities)}; профиль требует минимум {minimum}. Принято предложений: {n}."
    else:
        reason = "Профиль выполнен. Поставщики разделены по установленной идентичности продавца; аффилированность и юридическая обоснованность метода не проверены."
    return {"status": "ready" if ready else "insufficient_data", "unit_price": _number(mean) if ready else None,
            "total": _number(mean * quantity, 2) if ready else None, "mean": _number(mean),
            "stddev": _number(stddev), "cv": _number(cv, 8), "n": n, "independent_suppliers": len(identities),
            "rule": rule, "reason": reason}


def _iqr_warning(included):
    # Tukey hinges; diagnostic only. Small samples do not justify outlier tests.
    if len(included) < 5:
        return "IQR не применён: для диагностики требуется не менее 5 сопоставимых цен."
    ordered = sorted(offer["_decimal_price"] for offer in included)

    def median(values):
        n = len(values)
        return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2

    middle = len(ordered) // 2
    low = median(ordered[:middle])
    high = median(ordered[middle + (len(ordered) % 2):])
    spread = high - low
    lower, upper = low - Decimal("1.5") * spread, high + Decimal("1.5") * spread
    unusual = [str(offer.get("id")) for offer in included if not lower <= offer["_decimal_price"] <= upper]
    if unusual:
        return "IQR: вне диагностических границ предложения " + ", ".join(unusual) + ". Они сохранены в расчёте; нужна проверка причин различий."
    return "IQR: значений вне диагностических границ не выявлено; это не подтверждение рыночной полноты."


def provisional_calculation(request, offers, minimum=3):
    """Explain conditional arithmetic separately from the strict calculation.

    Quoted prices and sale units are indispensable. Missing specifications or
    tax terms remain explicit assumptions; they never turn an observed number
    into a ready procurement justification. Known mismatches and manual/system
    exclusions are never brought back by this estimate.
    """
    warnings, candidates, unit_assumptions = [], [], []
    unidentified = 0
    seen_urls, seen_pairs = set(), set()
    quantity = _decimal(request.get("quantity"))
    for offer in offers:
        if not isinstance(offer, dict) or offer.get("status") == "excluded" or offer.get("manual_excluded") or offer.get("duplicate_of"):
            continue
        if offer.get("relevance", {}).get("verdict") == "mismatch":
            continue
        if not _field_confirmed(offer, "price"):
            continue
        if not offer.get("url") or not offer.get("retrieved_at") or offer.get("provenance") not in {"live", "snapshot", "synthetic"}:
            continue
        if _clean(offer.get("currency")) not in {"rub", "руб", "руб.", "₽"}:
            continue
        if not _field_confirmed(offer, "currency"):
            continue
        normalization_offer = offer
        unit_assumption = None
        if not _field_confirmed(offer, "unit"):
            inferred = offer.get("provisional_unit")
            packaging = (offer.get("evidence") or {}).get("attributes.sheets_per_pack")
            price_proof = (offer.get("evidence") or {}).get("price") or {}
            if not (not offer.get("unit") and isinstance(inferred, dict) and isinstance(packaging, dict)
                    and inferred.get("basis") == "single_primary_product_pack"
                    and inferred.get("document_id") and inferred.get("source_hash")
                    and _unit(inferred.get("unit")) == _unit(request.get("unit")) == "pack"
                    and inferred.get("quote") == packaging.get("quote") and inferred.get("url") == packaging.get("url")
                    and inferred.get("document_id") == price_proof.get("document_id")
                    and inferred.get("source_hash") == price_proof.get("source_hash")
                    and _field_confirmed(offer, "attributes.sheets_per_pack")
                    and re.search(r"пачк|упаковк", str(inferred.get("quote") or ""), re.I)
                    and offer.get("validation", {}).get("extraction") == "deterministic_primary_product"):
                continue
            normalization_offer = {**offer, "unit": inferred["unit"]}
            unit_assumption = {"offer_id": offer["id"], "unit": inferred["unit"], "quote": inferred["quote"],
                               "assumption": inferred.get("assumption") or "Предполагается цена за указанную пачку; подтвердите единицу продажи."}
        if _unit(normalization_offer.get("unit")) != _unit(request.get("unit")) and not _field_confirmed(offer, "pack_size"):
            continue
        price, problem = _normalized(normalization_offer, request)
        if problem or price is None:
            continue
        attrs = offer.get("attributes") or {}
        unmatched = []
        mismatch = False
        for requirement in request.get("requirements") or []:
            if not isinstance(requirement, dict) or not requirement.get("key"):
                unmatched.append("Некорректное требование")
                continue
            key = requirement["key"]
            actual = attrs.get(key)
            verdict = _attribute_match(offer, requirement) if actual is not None else None
            if verdict is False:
                mismatch = True
                break
            if verdict is None or not _field_confirmed(offer, "attributes." + key):
                unmatched.append(requirement.get("label") or LABELS.get(key, key))
        if mismatch:
            continue
        condition_checks = _condition_checks(request, offer)
        if any(check["status"] == "mismatch" for check in condition_checks):
            continue
        title = str(offer.get("title") or "")
        if not positive_product_identity(request, offer):
            unidentified += 1
            warnings.append(f"{title}: соответствие предмету закупки не подтверждено; цена не использована в предварительной оценке.")
            continue
        canonical = _canonical_url(offer.get("url"))
        seller = _verified_identity(offer) or _supplier_name(offer)
        pair = (seller, _model(attrs.get("model")) if attrs.get("model") else _clean(title))
        if canonical and canonical in seen_urls or all(pair) and pair in seen_pairs:
            continue
        if canonical:
            seen_urls.add(canonical)
        if all(pair):
            seen_pairs.add(pair)
        candidates.append((offer, price))
        if unit_assumption:
            unit_assumptions.append(unit_assumption)
            warnings.append(f"{title}: " + unit_assumption["assumption"])
        if unmatched:
            warnings.append(f"{title}: не подтверждены характеристики — {', '.join(dict.fromkeys(unmatched))}.")
        if offer.get("relevance", {}).get("verdict") == "uncertain":
            warnings.append(f"{title}: функциональное соответствие требует проверки.")
        if offer.get("tax") != "included" or not _field_confirmed(offer, "tax"):
            warnings.append("Расчёт использует указанные источниками цены; НДС не приведён к общей базе и требует проверки.")
        if not offer.get("delivery"):
            warnings.append("Доставка не добавлена: её стоимость не подтверждена источниками.")
        incomplete_conditions = _unique_condition_labels(condition_checks)
        if incomplete_conditions:
            warnings.append(f"{title}: не подтверждены условия ТЗ — {', '.join(incomplete_conditions)}.")
        if offer.get("unit_inference") and not _manual_confirmation(offer, "unit"):
            warnings.append("Для части предложений единица дополнительно интерпретирована; проверьте условия продажи.")
    missing = request.get("missing_critical_fields") or []
    if missing:
        warnings.insert(0, "Потребность задана неполно: " + ", ".join(LABELS.get(str(key), str(key)) for key in missing) + ". Модели могут быть несопоставимы.")
    if not candidates:
        return {"unit_price": None, "total": None, "n": 0, "offer_ids": [],
                "warnings": list(dict.fromkeys(warnings)),
                "reason": ("Не подтверждено, что найденные ценовые предложения относятся к предмету закупки." if unidentified else "Нет предложений с одновременно подтверждёнными ценой, валютой и сопоставимой единицей продажи.")}
    sellers = {_verified_identity(offer) or _supplier_name(offer) for offer, _ in candidates} - {None, ""}
    if len(sellers) < minimum:
        warnings.append(f"Для профиля требуется минимум {minimum} поставщика; в предварительной оценке установлено {len(sellers)}.")
    if quantity is None or quantity <= 0:
        warnings.append("Количество закупки не подтверждено; общая стоимость не рассчитана.")
    with localcontext() as context:
        context.prec = 36
        mean = sum((price for _, price in candidates), Decimal(0)) / Decimal(len(candidates))
        total = mean * quantity if quantity is not None and quantity > 0 else None
        variance = sum(((price - mean) ** 2 for _, price in candidates), Decimal(0)) / Decimal(len(candidates) - 1) if len(candidates) > 1 else None
        stddev = variance.sqrt() if variance is not None else None
        cv = stddev / mean * 100 if stddev is not None and mean else None
    return {"unit_price": _number(mean), "total": _number(total, 2), "n": len(candidates),
            "mean": _number(mean), "stddev": _number(stddev), "cv": _number(cv, 8),
            "normalized_prices": {offer["id"]: _number(price) for offer, price in candidates},
            "unit_assumptions": unit_assumptions,
            "offer_ids": [offer["id"] for offer, _ in candidates], "warnings": list(dict.fromkeys(warnings)),
            "reason": ("Предварительная оценка по подтверждённым ценам с явно указанным предположением об единице продажи. " if unit_assumptions else "Предварительная оценка по подтверждённым ценам и единицам. ") + "Указанные пробелы требуют проверки; это не готовая НМЦД.",
            "formula": "Среднее указанных цен в единице закупки с учётом явно перечисленных предположений × количество."}


def _unique_condition_labels(checks):
    return list(dict.fromkeys(check["label"] for check in checks if check["status"] == "unconfirmed"))


def analyze_offers(request: dict, offers: list[dict], policy: dict | None = None) -> dict:
    """Compare hard requirements, preserve uncertainty, then apply the profile."""
    if not isinstance(request, dict) or not isinstance(offers, list):
        raise ValueError("Нужны объект потребности и список предложений.")
    policy = policy or {}
    minimum = policy.get("min_suppliers", 3)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("Минимум поставщиков должен быть положительным целым числом.")
    request = deepcopy(request)
    warnings = ["Профиль расчёта и итоговое обоснование требуют проверки специалистом.",
                "Стоимость доставки не включена в итог; условия и конечная сумма требуют подтверждения.",
                "Разные домены не доказывают независимость поставщиков. Группировка использует ИНН при наличии, иначе название продавца; полная аффилированность не проверена.",
                "CV (%) = выборочное стандартное отклонение / среднее × 100. При n < 2 CV не определён."]
    missing = request.get("missing_critical_fields") or []
    invalid_request = "Не заданы обязательные характеристики потребности: " + ", ".join(str(x) for x in missing) + "." if missing else None
    if invalid_request:
        warnings.append(invalid_request)
    requirements = request.get("requirements") or []
    parsed = []
    # Names join offers across domains even where only one record has an INN.
    identities_by_name = {}
    for raw in offers:
        if isinstance(raw, dict) and _supplier_name(raw) and _verified_identity(raw):
            identities_by_name.setdefault(_supplier_name(raw), set()).add(_verified_identity(raw))
    seen_models, seen_urls = {}, {}
    for index, raw in enumerate(offers):
        if not isinstance(raw, dict):
            raw = {"id": f"invalid-{index + 1}", "title": "Некорректная запись"}
        offer = deepcopy(raw)
        offer.setdefault("id", f"offer-{index + 1}")
        offer["reasons"] = []
        offer["normalized_price"] = None
        excluded, uncertain = [], []
        attrs = offer.get("attributes") if isinstance(offer.get("attributes"), dict) else {}
        offer["attributes"] = attrs
        if invalid_request:
            uncertain.append(invalid_request)
        identity_check = product_identity_assessment(request, offer)
        offer["product_identity"] = identity_check
        if identity_check["status"] == "mismatch":
            excluded.append(identity_check["reason"])
        elif request.get("category") not in {"oscilloscope", "paper", "service"} and identity_check["status"] != "matches":
            uncertain.append("Соответствие предмету закупки не подтверждено источником; карточка сохранена для проверки.")
        if offer.get("source_issues"):
            excluded.append("Противоречия первичного источника: " + " ".join(str(x) for x in offer["source_issues"]))
        if attrs.get("is_listing") or attrs.get("page_type") in {"listing", "category", "search"} or offer.get("is_listing"):
            excluded.append("Страница содержит список товаров; цена не привязана к одному подтверждённому предложению.")
        model = attrs.get("model")
        title_model = _extract_model(offer.get("title", ""))
        if model and title_model and _model(model) != _model(title_model):
            excluded.append(f"Модель в названии источника ({title_model}) отличается от заявленной ({model}).")
        evidence = offer.get("evidence") if isinstance(offer.get("evidence"), dict) else {}
        model_evidence = evidence.get("attributes.model", {})
        evidence_model = _extract_model(model_evidence.get("quote", "")) if isinstance(model_evidence, dict) else None
        if model and evidence_model and _model(model) != _model(evidence_model):
            excluded.append("Модель в доказательстве источника не совпадает с заявленной моделью.")
        offer["requirement_checks"] = []
        accepted_fields = []
        for requirement in requirements:
            if not isinstance(requirement, dict) or not requirement.get("key"):
                uncertain.append("Некорректное обязательное требование; сравнение не выполнено.")
                continue
            key = requirement["key"]
            label = requirement.get("label") or LABELS.get(key, key)
            actual = attrs.get(key)
            field = "attributes." + key
            acceptance = _manual_acceptance(offer, field)
            match = _attribute_match(offer, requirement) if actual is not None and actual != "" else None
            raw_status = "mismatch" if match is False else "matches" if match is True and _field_confirmed(offer, field) else "unconfirmed"
            check = {"key": key, "field": field, "label": label, "expected": requirement.get("value"), "actual": actual, "status": raw_status}
            if acceptance and raw_status != "matches":
                check["acceptance"] = deepcopy(acceptance)
                accepted_fields.append(field)
            offer["requirement_checks"].append(check)
            if acceptance:
                continue
            if actual is None or actual == "":
                uncertain.append(f"{label}: значение не извлечено; проверьте исходную карточку.")
                continue
            match = _attribute_match(offer, requirement)
            if match is False:
                operator = {"eq": "равно", "=": "равно", "gte": "не менее", ">=": "не менее",
                            "lte": "не более", "<=": "не более", "gt": "больше", "lt": "меньше",
                            "in": "одно из", "contains": "содержит", "between": "в диапазоне"}.get(requirement.get("op", "eq"), "")
                excluded.append(f"{label}: {actual}; требуется {operator} {requirement.get('value')} {requirement.get('unit') or ''}.".strip())
            elif match is None:
                uncertain.append(f"{label}: значение или операция сравнения не поддерживаются.")
            if not _field_confirmed(offer, "attributes." + key):
                uncertain.append(f"{label}: нет цитаты и ссылки, подтверждающих значение.")
        offer["condition_checks"] = _condition_checks(request, offer)
        for check in offer["condition_checks"]:
            field = "conditions." + check["key"]
            acceptance = _manual_acceptance(offer, field)
            if acceptance:
                if check["status"] != "matches":
                    check["acceptance"] = deepcopy(acceptance)
                    accepted_fields.append(field)
                continue
            if check["status"] == "mismatch":
                excluded.append(check["label"] + ": условия поставщика не соответствуют ТЗ.")
            elif check["status"] == "unconfirmed":
                uncertain.append(check["label"] + ": выполнение условия ТЗ не подтверждено источником.")
        offer["manual_acceptance_fields"] = accepted_fields
        price, problem = _normalized(offer, request)
        if problem:
            uncertain.append(problem)
        if not _field_confirmed(offer, "price"):
            uncertain.append("Цена не подтверждена цитатой и ссылкой источника.")
        if _unit(offer.get("unit")) != _unit(request.get("unit")) and not _field_confirmed(offer, "pack_size"):
            uncertain.append("Коэффициент пересчёта упаковки не подтверждён источником.")
        if _clean(offer.get("currency")) not in {"rub", "руб", "руб.", "₽"}:
            uncertain.append("Валюта не подтверждена как RUB; курс и конвертация не заданы.")
        if offer.get("tax") == "excluded":
            uncertain.append("Цена указана без НДС; ставка и итог с налогом не установлены.")
        elif offer.get("tax") != "included":
            uncertain.append("Неизвестно, включён ли НДС в цену.")
        elif not _field_confirmed(offer, "tax"):
            uncertain.append("Включение НДС не подтверждено цитатой источника.")
        details = offer.get("supplier_details") if isinstance(offer.get("supplier_details"), dict) else {}
        supplier_evidence = details.get("evidence") if isinstance(details.get("evidence"), dict) else {}
        verification = details.get("verification") if isinstance(details.get("verification"), dict) else {}
        identity_conflicts = []
        for field in ("inn", "legal_name"):
            proof = supplier_evidence.get(field)
            rows = proof.get("conflicts", []) if isinstance(proof, dict) else []
            values = {str(row.get("value")).casefold() for row in rows if isinstance(row, dict) and row.get("value")}
            if len(values) > 1:
                identity_conflicts.append(field)
        platform = (offer.get("source_group") == "platforms" or
                    source_metadata(offer.get("url") or offer.get("domain"))["source_group"] == "platforms")
        manual_seller = _manual_confirmation(offer, "supplier")
        seller_proven = bool(_has_evidence(offer, "supplier") or _verified_identity(offer))
        if identity_conflicts:
            offer["reason_supplier"] = verification.get("reason") or "В источнике противоречивые реквизиты; личность продавца неоднозначна."
            uncertain.append(offer["reason_supplier"])
        elif platform and (not seller_proven or verification.get("status") == "conflict"):
            if manual_seller and _supplier_name(offer):
                offer["reason_supplier"] = "Продавец установлен ручным подтверждением пользователя; источник не подтверждает его личность."
            else:
                offer["reason_supplier"] = verification.get("reason") or "Отдельный продавец на площадке не установлен; платформа не считается независимым поставщиком."
                uncertain.append(offer["reason_supplier"])
        elif verification.get("status") == "conflict":
            offer["reason_supplier"] = verification.get("reason") or "Личность продавца требует отдельной проверки."
            uncertain.append(offer["reason_supplier"])
        if not _supplier_name(offer):
            uncertain.append("Поставщик не установлен.")
        if not offer.get("url") or not offer.get("retrieved_at") or offer.get("provenance") not in {"live", "snapshot", "synthetic"}:
            uncertain.append("Не хватает ссылки, даты сохранения или происхождения предложения.")
        if offer.get("provenance") == "synthetic":
            warnings.append("Синтетические учебные предложения не являются рыночными данными.")
        if offer.get("unit_inference") and not _manual_confirmation(offer, "unit"):
            warnings.append(f"Единица цены «{offer.get('unit')}» для «{offer.get('supplier') or offer['id']}» — вывод: {offer['unit_inference']} Это не явное указание источника цены за единицу; требуется подтверждение.")
        identity = _verified_identity(offer)
        name = _supplier_name(offer)
        name_ids = identities_by_name.get(name, set())
        if len(name_ids) > 1 and not identity:
            uncertain.append("Одинаковое название связано с разными ИНН; поставщик этой записи не установлен.")
        seller = attrs.get("supplier_group") or identity or (next(iter(name_ids)) if len(name_ids) == 1 else name)
        offer["_supplier_key"] = str(seller or "")
        model_key = _model(model or title_model) if model or title_model else _clean(offer.get("title"))
        pair = (seller, model_key)
        canonical_url = _canonical_url(offer.get("url"))
        previous = seen_models.get(pair) if all(pair) else None
        previous = previous or (seen_urls.get(canonical_url) if canonical_url else None)
        # An unusable earlier record must not suppress a later evidenced quote.
        if previous and not excluded and not uncertain:
            excluded.append("Повтор того же поставщика и модели; повторная цена не увеличивает выборку.")
            offer["duplicate_of"] = previous
        if not excluded and not uncertain:
            seen_models[pair] = offer["id"]
            if canonical_url:
                seen_urls[canonical_url] = offer["id"]
        if accepted_fields:
            warnings.append("Есть принятые пользователем отклонения; исходные значения и неполнота доказательств сохранены.")
        if offer.get("manual_confirmations"):
            warnings.append("Есть ручные подтверждения пользователя; они не являются цитатами источника и не подтверждают юридическую обоснованность НМЦК.")
        offer["status"] = "excluded" if excluded else "uncertain" if uncertain else "included"
        offer["reasons"] = excluded + uncertain
        if offer["status"] == "included":
            offer["reasons"] = ["Обязательные характеристики и цена с НДС подтверждены; единицы приведены к потребности."]
            if accepted_fields:
                offer["reasons"] = ["Допущено пользователем с отклонениями: " + ", ".join(accepted_fields) + ". Исходные факты не изменены."]
            if offer.get("manual_confirmations"):
                offer["reasons"] = ["Принято по выбранному профилю с ручными подтверждениями пользователя: " + ", ".join(sorted(offer["manual_confirmations"])) + "."]
        offer["normalized_price"] = _number(price) if price is not None else None
        offer["_decimal_price"] = price
        if not identity and offer["status"] == "included":
            offer["reasons"].append("Поставщик сгруппирован по названию; ИНН и аффилированность не подтверждены.")
        parsed.append(offer)
    included = [offer for offer in parsed if offer["status"] == "included"]
    with localcontext() as context:
        context.prec = 36
        calculation = _calculation(request, included, minimum, invalid_request)
        sensitivity = []
        for removed in included:
            remaining = [offer for offer in included if offer is not removed]
            alternative = _calculation(request, remaining, minimum, invalid_request)
            base = _decimal(calculation["unit_price"])
            new = _decimal(alternative["unit_price"])
            delta = (new - base) / base * 100 if base and new is not None else None
            sensitivity.append({"offer_id": removed["id"], "supplier": removed.get("supplier"),
                                "status": alternative["status"], "unit_price": alternative["unit_price"],
                                "total": alternative["total"], "n": alternative["n"],
                                "independent_suppliers": alternative["independent_suppliers"],
                                "delta_percent": _number(delta, 4), "reason": alternative["reason"]})
        warnings.append(_iqr_warning(included))
        calculation["provisional"] = provisional_calculation(request, parsed, minimum)
    if any(offer.get("delivery") is None for offer in parsed):
        warnings.append("Для части предложений условия доставки неизвестны; неизвестность не означает бесплатную доставку.")
    for offer in parsed:
        offer.pop("_decimal_price", None)
        offer.pop("_supplier_key", None)
    return {"request": request, "offers": parsed, "calculation": calculation,
            "sensitivity": sensitivity, "warnings": list(dict.fromkeys(warnings))}

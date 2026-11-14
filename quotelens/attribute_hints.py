"""Qualified source context, deliberately separate from confirmed attributes."""
import re

from .price_context import _primary_scope, primary_product_context, _locator, _space


def explicit_paper_color(document):
    """A literal color in this product's verified title is a source fact."""
    context = primary_product_context(document)
    title = context["primary_title"] if context else ""
    if not context or not re.search(r"\bбумаг[аи]\b|\bpaper\b", title, re.I):
        return None
    if re.search(r"\b(?:не|not)\s+(?:белая|белый|white)\b", title, re.I):
        return None
    match = re.search(r"\b(?:белая|белый|white)\b", title, re.I)
    if not match or title not in _space(document.get("text")):
        return None
    return {"value": "белый", "quote": title, "raw_label": "Цвет из названия товара", "raw_value": match.group(),
            "basis": "primary_product_title_attribute", "locator": "primary-title", "normalization": "explicit_color_word"}


def contextual_attribute_hints(document, request, attributes, evidence=None):
    result = {}
    context = primary_product_context(document)
    if not context:
        return result
    text = _space(document.get("text"))
    base = {"url": document.get("url"), "document_id": document.get("document_id") or document.get("id"),
            "source_hash": document.get("sha256"), "retrieved_at": document.get("retrieved_at"), "basis": "qualified_source_context"}
    wanted = {r.get("key") for r in request.get("requirements", [])}
    def add(key, value, quote, reason, locator):
        if key in wanted and attributes.get(key) is None and _space(quote) in text:
            result.setdefault(key, []).append({**base, "value": value, "quote": _space(quote), "reason": reason, "locator": locator, "confirmed_for_product": False})
    countries = {"России": "Россия", "Китае": "Китай", "Германии": "Германия"}
    for match in re.finditer(r"[^.!?]{0,220}(?:производится|изготавливается|произведен[ао]?|изготовлен[ао]?)\s+в\s+(России|Китае|Германии)[^.!?]{0,120}[.!?]?", context["text"], re.I):
        if re.search(r"(?:\bне|никогда\s+не)\s+(?:производится|изготавливается|произведен[ао]?|изготовлен[ао]?)", match.group(), re.I):
            continue
        country = next(value for key, value in countries.items() if key.casefold() == match[1].casefold())
        add("country", country, match.group(), "Из описания бренда; происхождение конкретной поставки требует подтверждения.", "primary-description")
    bound = _primary_scope(document)
    if bound and "color" in wanted and attributes.get("color") is None:
        for node in bound[0].nodes:
            breadcrumb = any(re.search(r"breadcrumb", part.label, re.I) or "BreadcrumbList" in part.attrs.get("itemtype", "") for part in node.ancestors())
            if node.tag in {"a", "span"} and breadcrumb and not node.hidden:
                quote = node.text()
                if len(quote) <= 120 and re.search(r"\bбумага\s+(?:форматная\s+)?белая\b", quote, re.I):
                    add("color", "белый", quote, "Из категории сайта; отдельная характеристика цвета не найдена.", _locator(node))
    # CIE describes whiteness, rather than an independently stated color. Use
    # it only as an explicit suggestion for review, keeping the two facts apart.
    title = context["primary_title"]
    colored = re.search(r"\b(?:цветная|красная|синяя|голубая|зел[её]ная|ж[её]лтая|розовая|ч[её]рная|colou?red|red|blue|green|yellow|pink|black)\b|\b(?:не|not)\s+(?:белая|белый|white)\b", title, re.I)
    cie = attributes.get("whiteness_cie")
    values = cie if isinstance(cie, (list, tuple)) else [cie]
    proof = (evidence or {}).get("attributes.whiteness_cie") or {}
    if ("color" in wanted and attributes.get("color") is None and not result.get("color")
            and request.get("category") == "paper" and not colored
            and values and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in values)
            and proof.get("quote") and proof.get("url") and proof.get("document_id") and proof.get("source_hash")):
        result["color"] = [{**proof, "value": "белый", "basis": "inferred_from_CIE_whiteness", "inferred": True,
                             "related_field": "attributes.whiteness_cie", "confirmed_for_product": False,
                             "reason": "Предположение по показателю белизны CIE; цвет отдельно в карточке не указан."}]
    # Do not fill a manufacturer from a trademark or assign a CIE scale to an
    # unlabelled title percentage. Such fields keep their genuinely missing role.
    return result

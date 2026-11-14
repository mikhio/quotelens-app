"""Primary visible price blocks derived from captured markup, never model hints."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from html.parser import HTMLParser
import re
from urllib.parse import urljoin


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


_NUMBER = r"(?<![\w\d])(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?"
_MONEY = r"(?:₽|р\s*уб(?:\.|лей|ля)?|RUB\b|USD\b|EUR\b|\$|€|р\.)"
_AMOUNT = re.compile(r"(?P<number>" + _NUMBER + r")\s*" + _MONEY, re.I)
_PRICE = re.compile(r"price|цен[аы]", re.I)
_BAD_CLASS = re.compile(r"(?:^|[\s_-])(?:old(?=$|[\s_-]|price)|previous|was[-_]?price|cross|strike|recommend|related|similar|accessor|warranty|installment|credit|monthly|рассроч|гарант|поверк)", re.I)
_OLD_CLASS = re.compile(r"(?:^|[\s_-])(?:old(?=$|[\s_-]|price)|previous|was[-_]?price|strike)", re.I)
_BAD_TEXT = re.compile(r"старая\s+цена|old\s+price|в\s+месяц|per\s+month|рассроч|кредит|гаранти|поверк|минимальн\w*\s+(?:цен|сумм\w*\s+заказ)|добавьте\s+товары\s+на\s+сумму", re.I)
_RELATED = re.compile(r"Рекомендуем(?:ые|\s+также)|Вас\s+также\s+может|Похожие\s+товары|Другие\s+товары\s+из\s+категории|С\s+этим\s+товаром\s+покупают|Сопутствующие\s+товары|related\s+products|you\s+may\s+also\s+like", re.I)
_TAX = re.compile(r"без\s*НДС|НДС\s*не\s*включ|excl(?:uding)?\.?\s*VAT|(?:\bс\s*НДС|вкл\.?\s*НДС|включая\s*НДС|НДС\s*включ|incl(?:uding)?\.?\s*VAT)", re.I)
_TIER = re.compile(r"(?:при\s+(?:заказе|покупке)\s*(?:(?:от|до|не\s+менее|не\s+более|свыше|более|менее)\s*)?|(?:от|до|не\s+менее|не\s+более|свыше|более|менее)\s*)\d+(?:[.,]\d+)?\s*(?:шт|штук|упак|пач|короб|лист|час|м[²2]|pieces|packs|boxes)", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}
_PURCHASE = re.compile(r"product[-_]?(?:top|detail|sidebar|purchase|main|single|view)|productRow|catalog[-_](?:element|detail)|pay[-_]?block|add2cart|cart[-_]form", re.I)
_SEPARATE_PRODUCT = re.compile(r"related|releated|recommend|similar|accessor|cross[-_]?sell|recently[-_]viewed|product[-_](?:item|card|thumb|grid)(?![-_]detail)|product[-_]?Block|catalog[-_]item", re.I)
_PRIMARY_ROOT = re.compile(r"(?:^|\s)(?:main[-_]product|single[-_]product(?:[-_]page|[-_]content)?|type[-_]product)(?=\s|$)", re.I)
_ATTRIBUTE_LABEL = re.compile(r"name|label|title|key|(?:^|[-_])prop(?=$|[\s_-])", re.I)
_ATTRIBUTE_VALUE = re.compile(r"value|val(?:ue)?$|data", re.I)


class _Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.parts, self.content = [], [], []
        self.hidden = bool(parent and parent.hidden) or tag in _SKIP or "hidden" in attrs or attrs.get("aria-hidden") == "true" or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I)) or bool(set(attrs.get("class", "").split()) & {"hide", "hidden", "d-none", "hidden-xs-up"})

    @property
    def label(self):
        return " ".join(self.attrs.get(key, "") for key in ("class", "id", "data-entity"))

    def text(self):
        return _space(" ".join(self.parts))

    def ancestors(self):
        node = self
        while node:
            yield node
            node = node.parent


class _DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack, self.nodes = [self.root], []

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) >= 30000 or len(self.stack) >= 100:
            raise ValueError("Captured DOM exceeds deterministic extraction limits")
        node = _Node(tag, {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack[-1].hidden:
            self.stack[-1].content.append(data)
            for node in self.stack:
                node.parts.append(data)


@lru_cache(maxsize=2)
def _parsed_dom(html):
    # Both validators inspect the same immutable capture repeatedly. Keep two
    # private parsed trees; derived quotations/contexts are new values per call.
    dom = _DOM()
    try:
        dom.feed(html)
    except (ValueError, RecursionError):
        return None
    return dom


def _decimal(value):
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _identity(value):
    return re.sub(r"[^a-zа-яё0-9]", "", _space(value).casefold())


def _primary_scope(document):
    html = document.get("html")
    primary = _space(document.get("primary_title") or document.get("title"))
    if not isinstance(html, str) or not html or not primary:
        return None
    dom = _parsed_dom(html)
    if dom is None:
        return None
    headings = [node for node in dom.nodes if node.tag == "h1" and not node.hidden and node.text()]
    if not headings or _identity(headings[0].text()) != _identity(primary):
        return None
    matches = []
    for node in dom.nodes:
        if "schema.org/Product" not in node.attrs.get("itemtype", "") or node.hidden or _SEPARATE_PRODUCT.search(node.label):
            continue
        names = [part.attrs.get("content") or part.text() for part in dom.nodes if part.attrs.get("itemprop") == "name" and node in part.ancestors()]
        inside = any(node in heading.ancestors() for heading in headings)
        if inside or any(_identity(name) == _identity(primary) for name in names):
            if not names or any(_identity(name) == _identity(primary) for name in names):
                matches.append(node)
    if matches:
        scope = min(matches, key=lambda node: (not any(node in heading.ancestors() for heading in headings), len(node.text())))
        return dom, scope, primary, True
    # Common product templates identify the whole item without microdata.
    # Bind to an ancestor of the verified H1, retaining its specifications as
    # well as the buy box; a small summary alone may omit technical tabs.
    for parent in headings[0].ancestors():
        if (parent.tag not in {"body", "html", "root"} and len(parent.text()) <= 50000
                and _PRIMARY_ROOT.search(parent.label) and not _SEPARATE_PRODUCT.search(parent.label)):
            return dom, parent, primary, True
    for parent in headings[0].ancestors():
        if parent.tag in {"body", "html", "root"} or len(parent.text()) > 50000:
            continue
        purchase = any(_PURCHASE.search(node.label) and not _SEPARATE_PRODUCT.search(node.label)
                       and parent in node.ancestors() for node in dom.nodes)
        if parent.tag in {"main", "article"} or purchase:
            return dom, parent, primary, purchase
    return None


def _excluded(node, scope=None, for_price=False):
    return (node.hidden or node.tag in {"nav", "header", "footer", "del", "s", "strike"}
            or bool((_BAD_CLASS if for_price else _OLD_CLASS).search(node.label)) or bool(_SEPARATE_PRODUCT.search(node.label))
            or bool(re.search(r"text-decoration\s*:[^;]*line-through", node.attrs.get("style", ""), re.I))
            or node is not scope and node.tag == "li" and "type-product" in node.attrs.get("class", "").split()
            or for_price and "tab-pane" in node.attrs.get("class", "").split() and "active" not in node.attrs.get("class", "").split()
            or node is not scope and "schema.org/Product" in node.attrs.get("itemtype", ""))


def _foreign_container(node, primary):
    if not primary or len(node.text()) > 2000:
        return False
    for child in node.children:
        labelled = child.attrs.get("itemprop") == "name" or re.search(r"(?:^|[\s_-])(?:title|name)(?=$|[\s_-])", child.label, re.I)
        model_heading = child.tag in {"h2", "h3", "h4"} and re.search(r"\b[a-zа-яё]+[-_/]?\d+[a-zа-яё0-9]*\b", child.text(), re.I)
        if child.hidden or not (labelled or model_heading):
            continue
        title = next((part.text() for part in child.children if part.tag in {"p", "a", "h2", "h3"} and part.text()), child.text())
        if len(title) > 15 and re.search(r"\d", title) and len(re.findall(r"[a-zа-яё]{3,}", title, re.I)) > 1:
            if _identity(primary) not in _identity(title) and _identity(title) not in _identity(primary):
                return True
    return False


def _active_text(node, scope=None, primary=None, for_price=False):
    if _excluded(node, scope, for_price) or node is not scope and _foreign_container(node, primary):
        return ""
    return _space(" ".join(_active_text(part, scope, primary, for_price) if isinstance(part, _Node) else part for part in node.content))


def _foreign_local_title(node, scope, primary):
    for parent in list(node.ancestors())[1:]:
        if parent is scope or len(parent.text()) > 2000:
            break
        if _foreign_container(parent, primary):
            return True
    return False


def _foreign_price_owner(node, scope, primary):
    """Keep a price's explicit owner even beyond the short context limit.

    Only contiguous direct text/inline siblings belong to this owner line.
    A separate paragraph, menu or independently bound buy block cannot lend
    its foreign-product declaration to the primary price.
    """
    inline = {"span", "a", "b", "strong", "em", "small", "i", "u", "br"}
    child = node
    for parent in list(node.ancestors())[1:]:
        if scope not in parent.ancestors():
            break
        index = parent.content.index(child)
        left, right = index, index + 1

        def local(part):
            return isinstance(part, str) or (part.tag in inline and all(item.tag in inline for item in _descendants(part)))

        while left > 0 and local(parent.content[left - 1]):
            left -= 1
        while right < len(parent.content) and local(parent.content[right]):
            right += 1
        text = _space(" ".join(node.text() if part is child else part if isinstance(part, str)
                               else _active_text(part, scope, primary, True) for part in parent.content[left:right]))
        for declaration in re.finditer(r"цена\s+(?:для|модели)|price\s+for", text, re.I):
            named = re.split(r"[;:]", text[declaration.end():], maxsplit=1)[0]
            models = re.findall(r"\b[a-zа-яё]+[-_/]?\d+[a-zа-яё0-9]*(?:[-_/][a-zа-яё0-9]+)*\b", named, re.I)
            if any(_identity(model) not in _identity(primary) for model in models):
                return True
        if parent is scope or _PRICE.search(parent.label) or _PURCHASE.search(parent.label) or _PRIMARY_ROOT.search(parent.label):
            break
        child = parent
    return False


def primary_product_context(document):
    """Bind product facts to the captured H1 and exclude separate item widgets.

    Return None when markup cannot establish a primary scope, preserving the
    original strict text validation. Returned text is only an additional scope
    check: every accepted quotation must also occur in captured visible text.
    """
    bound = _primary_scope(document)
    if bound is None:
        return None
    dom, scope, primary, strong = bound

    parts, rows, stopped = [], [], False

    def visit(node):
        nonlocal stopped
        if stopped or _excluded(node, scope) or node is not scope and _foreign_container(node, primary):
            return
        row_start = len(parts)
        for item in node.content:
            if isinstance(item, _Node):
                visit(item)
            else:
                cut = _RELATED.search(item) if node.tag in {"h2", "h3", "h4", "h5"} else None
                parts.append(item[:cut.start()] if cut else item)
                if cut:
                    stopped = True
                    return
            if stopped:
                return
        if node.tag == "tr":
            row = _space(" ".join(parts[row_start:]))
            if row and row in _space(document.get("text")):
                rows.append(row)

    visit(scope)
    text = _space(" ".join(parts))
    if primary not in text:
        text = primary + " " + text
    return {"text": text, "rows": rows, "primary_title": primary, "strong_identity": strong}


def _locator(node):
    parts = []
    for part in node.ancestors():
        if part.parent is None:
            break
        siblings = [child for child in part.parent.children if child.tag == part.tag]
        parts.append(f"{part.tag}:nth-of-type({siblings.index(part) + 1})")
    return " > ".join(reversed(parts))


def _primary_nodes(document, predicate=None):
    """Yield only nodes actually retained by the primary visible context."""
    bound = _primary_scope(document)
    context = primary_product_context(document)
    if not bound or not context:
        return
    dom, scope, primary, _ = bound
    for node in dom.nodes:
        if predicate is not None and not predicate(node):
            continue
        if scope not in node.ancestors():
            continue
        lineage = list(node.ancestors())
        if any(_excluded(parent, scope) for parent in lineage[:lineage.index(scope) + 1]):
            continue
        if _foreign_local_title(node, scope, primary):
            continue
        text = _active_text(node, scope, primary)
        if text and text in context["text"]:
            yield node, scope, primary, text


def _attribute_row_candidate(node):
    if node.tag in {"tr", "dt"}:
        return True
    if node.tag not in {"div", "li", "p"}:
        return False
    return (not node.children or all(child.tag in {"span", "a", "b", "strong", "em", "small", "i"} for child in node.children)
            or len(node.children) == 2 and _ATTRIBUTE_LABEL.search(node.children[0].label)
            and _ATTRIBUTE_VALUE.search(node.children[1].label))


def _reachable_attribute_tab(node, scope):
    """An inactive technical panel is evidence only through its own control.

    Explicitly hidden descendants remain excluded by the normal DOM traversal.
    This does not turn hidden prices, arbitrary panels or script data visible.
    """
    for panel in node.ancestors():
        if panel is scope:
            break
        panel_id = panel.attrs.get("id")
        if not panel_id or panel.hidden:
            continue
        if not (panel.attrs.get("role") == "tabpanel" or re.search(r"(?:^|\s)tab-pane(?:\s|$)", panel.label)):
            continue
        for control in _descendants(scope):
            if (control.hidden or not re.search(r"характеристик|параметр|specification|technical|features|attributes", control.text(), re.I)):
                continue
            if (control.attrs.get("href") == "#" + panel_id
                    or control.attrs.get("role") == "tab" and panel_id in control.attrs.get("aria-controls", "").split()):
                if any(_excluded(part, scope) for part in control.ancestors() if scope in part.ancestors()):
                    continue
                return {"basis": "primary_product_dom_attribute", "visibility": "reachable_tab",
                        "tab_id": panel_id, "tab_label": control.text()}
    return None


def _descendants(node):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _captured_quote(text, visible):
    """Return the actual captured spelling after a CSS case transformation."""
    if text in visible:
        return text
    match = re.search(re.escape(text), visible, re.I)
    return match.group() if match else None


def primary_attribute_rows(document):
    """Structured label/value observations, with exact visible quotations.

    Tables, definition lists and labelled property blocks share this contract.
    A free number in the description is deliberately not a property row.
    """
    result, seen = [], set()
    visible = _space(document.get("text"))
    for node, scope, primary, text in _primary_nodes(document, _attribute_row_candidate) or []:
        label = value = None
        if node.tag == "tr":
            cells = [child for child in node.children if child.tag in {"td", "th"}]
            if len(cells) == 2 and all(child.attrs.get("colspan", "1") == "1" for child in cells):
                label, value = [_active_text(child, scope, primary) for child in cells]
        elif node.tag == "dt" and node.parent:
            siblings = node.parent.children
            index = siblings.index(node)
            if index + 1 < len(siblings) and siblings[index + 1].tag == "dd":
                label, value = text, _active_text(siblings[index + 1], scope, primary)
                text = _space(label + " " + value)
        elif node.tag in {"div", "li", "p"}:
            children = [child for child in node.children if not child.hidden and _active_text(child, scope, primary)]
            if len(children) == 2 and _ATTRIBUTE_LABEL.search(children[0].label) and _ATTRIBUTE_VALUE.search(children[1].label):
                label, value = [_active_text(child, scope, primary) for child in children]
            elif len(text) <= 500 and (not children or re.search(r"characteristic|propert|specification|attribute", node.label, re.I)
                                      or len(children) == 1 and children[0].tag in {"b", "strong"}):
                match = re.fullmatch(r"([^:—]{2,120})\s*[:—]\s*(.{1,1000})", text)
                if match:
                    label, value = match.groups()
        label, value = _space(label), _space(value)
        captured = _captured_quote(text, visible)
        tab = _reachable_attribute_tab(node, scope) if captured is None else None
        if not label or not value or len(label) > 120 or len(value) > 1000 or captured is None and not tab:
            continue
        # A two-column price list is not a technical attribute table.
        if re.search(r"цен[аы]|стоимость|price|артикул|наличи|остаток", label, re.I):
            continue
        identity = (label.casefold(), value.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        result.append({"raw_label": label, "raw_value": value, "quote": captured or text,
                       "locator": _locator(node), "basis": "primary_product_attribute", **(tab or {})})
    return result


def _attribute_concept(label):
    """Conservative supplier label aliases, independent of domains and values."""
    text = _space(label).casefold().replace("ё", "е").strip(" :;—-")
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    aliases = [
        ("country", r"^(?:страна(?:\s+(?:происхождения|производител[ья]|производства))?|country(?:\s+of\s+origin)?)$"),
        ("manufacturer", r"^(?:производительь?|manufacturer)$"),
        ("brand", r"^(?:бренд|торговая\s+марка|brand)$"),
        ("format", r"^(?:формат(?:\s+(?:бумаги|листов|листа))?|paper[ _]format)$"),
        ("paper_grade", r"^(?:марка(?:\s+бумаги)?|класс(?:\s+бумаги)?|paper[ _]grade)$"),
        ("whiteness_cie", r"^(?:белизна\s+(?:по\s+)?cie|whiteness[ _]cie)$"),
        ("whiteness_iso", r"^белизна\s+(?:по\s+)?iso$"),
        ("density_gsm", r"^(?:плотность(?:\s+бумаги)?(?:\s*,.*)?|масса\s+листа(?:\s+бумаги)?\s+площадью\s+1\s*(?:кв\.?\s*м|м[²2])|density[ _]gsm)$"),
        ("thickness_um", r"^(?:толщина(?:\s+(?:бумаги|листа))?(?:\s*,.*)?|thickness[ _]um)$"),
        ("sheets_per_pack", r"^(?:количество\s+листов(?:\s+в\s+(?:пачке|упаковке))?|sheets[ _]per[ _]pack)$"),
        ("color", r"^(?:цвет(?:\s+бумаги)?|colou?r)$"),
        ("layer", r"толщин\w*\s+слоя|layer\s+(?:height|thickness)"),
        ("filament_diameter", r"диаметр\s+(?:(?:используемой|пластиковой)\s+)?(?:нити|филамента)|filament\s+diameter"),
        ("enclosure", r"^(?:тип\s+корпуса|корпус(?:\s+принтера)?|enclosure)$"),
        ("materials", r"поддерживаемые\s+(?:пластики|материалы)|supported\s+materials"),
        ("platform_material", r"материал\s+(?:рабочей\s+|печатной\s+)?платформы|build\s+plate\s+material"),
        ("dryer_temperature", r"(?:нагрев|температур)\w*.*(?:сушк|сушильн)|dryer\s+temperature"),
        ("bed_temperature", r"(?:нагрев|температур)\w*.*(?:платформ|стола)|bed\s+temperature"),
        ("chamber_temperature", r"(?:нагрев|температур)\w*.*камер|chamber\s+temperature"),
        ("extruder_temperature", r"(?:нагрев|температур)\w*.*экструдер|extruder\s+temperature"),
        ("speed", r"скорость\s+печати|print(?:ing)?\s+speed"),
        ("interfaces", r"интерфейс\w*\s+подключения|connectivity|interfaces?"),
        ("dimensions", r"^(?:габариты\s+(?:принтера\s+без\s+упаковки(?:\s*\([^)]*\))?|товара|изделия)|размеры\s+(?:товара|изделия)|product\s+dimensions)$"),
        ("weight", r"^(?:вес\s+(?:принтера\s+без\s+упаковки|нетто)|масса\s+нетто|net\s+weight)$"),
    ]
    for concept, pattern in aliases:
        if re.search(pattern, text, re.I):
            return concept
    return re.sub(r"[^a-zа-я0-9]", "", text)


_MEASURE_UNIT = re.compile(r"(?<![a-zа-яё])(?:(?:г|g)\s*/\s*(?:[мm]\s*[²2]|кв\.?\s*м)|gsm|мм\s*/\s*с|mm\s*/\s*s|мкм|[µμ]m|um|мм|mm|см|cm|мг|mg|мкг|[µμ]g|ug|кг|kg|г|g|кВт|kW|Вт|W|МГц|MHz|Гц|Hz|мл|ml|л|l|кВ|kV|В|V|мА|mA|А|A|%|°\s*[СC]|град(?:ус\w*)?|лист(?:ов|а)?)(?![a-zа-яё])", re.I)


def _measure_unit(value):
    value = re.sub(r"\s+", "", str(value or "")).casefold().replace("μ", "µ")
    if re.fullmatch(r"(?:г|g)/(?:[мm][²2]|кв\.?м)|gsm", value):
        return "area_density", Decimal(1), "г/м²"
    groups = {
        "мм": ("length", Decimal(1), "мм"), "mm": ("length", Decimal(1), "мм"),
        "см": ("length", Decimal(10), "см"), "cm": ("length", Decimal(10), "см"),
        "мкм": ("length", Decimal(".001"), "мкм"), "µm": ("length", Decimal(".001"), "мкм"), "um": ("length", Decimal(".001"), "мкм"),
        "кг": ("mass", Decimal(1000), "кг"), "kg": ("mass", Decimal(1000), "кг"),
        "г": ("mass", Decimal(1), "г"), "g": ("mass", Decimal(1), "г"),
        "мг": ("mass", Decimal(".001"), "мг"), "mg": ("mass", Decimal(".001"), "мг"),
        "мкг": ("mass", Decimal(".000001"), "мкг"), "µg": ("mass", Decimal(".000001"), "мкг"), "ug": ("mass", Decimal(".000001"), "мкг"),
        "вт": ("power", Decimal(1), "Вт"), "w": ("power", Decimal(1), "Вт"),
        "квт": ("power", Decimal(1000), "кВт"), "kw": ("power", Decimal(1000), "кВт"),
        "гц": ("frequency", Decimal(1), "Гц"), "hz": ("frequency", Decimal(1), "Гц"),
        "мгц": ("frequency", Decimal(1000000), "МГц"), "mhz": ("frequency", Decimal(1000000), "МГц"),
        "мм/с": ("speed", Decimal(1), "мм/с"), "mm/s": ("speed", Decimal(1), "мм/с"),
        "мл": ("volume", Decimal(".001"), "мл"), "ml": ("volume", Decimal(".001"), "мл"),
        "л": ("volume", Decimal(1), "л"), "l": ("volume", Decimal(1), "л"),
        "в": ("voltage", Decimal(1), "В"), "v": ("voltage", Decimal(1), "В"),
        "кв": ("voltage", Decimal(1000), "кВ"), "kv": ("voltage", Decimal(1000), "кВ"),
        "а": ("current", Decimal(1), "А"), "a": ("current", Decimal(1), "А"),
        "ма": ("current", Decimal(".001"), "мА"), "ma": ("current", Decimal(".001"), "мА"),
        "%": ("percent", Decimal(1), "%"),
        "лист": ("sheet_count", Decimal(1), "листов"), "листа": ("sheet_count", Decimal(1), "листов"),
        "листов": ("sheet_count", Decimal(1), "листов"),
    }
    if value.startswith("град") or value in {"°с", "°c"}:
        return "temperature", Decimal(1), "°C"
    return groups.get(value)


def _observed_units(text, *, label=False):
    units = set()
    for match in _MEASURE_UNIT.finditer(text):
        # Single-letter Russian units are also ordinary prepositions. In a
        # full quotation «листов в пачке» does not introduce voltage В.
        if len(match.group().strip()) == 1 and match.group() != "%":
            prefix = text[:match.start()]
            if not re.search(r"\d\s*$", prefix) and not (label and re.search(r"[,(\[]\s*$", prefix)):
                continue
        unit = _measure_unit(match.group())
        if unit:
            units.add(unit)
    return units


_NUMERIC_PART = r"[+−-]?\d+(?:[.,]\d+)?"


def _explicit_numbers(value):
    return [_decimal(number.replace("−", "-"))
            for number in re.findall(r"(?<![\w.,])" + _NUMERIC_PART, value)]


def _numeric_segment(value):
    """Read a scalar or a written interval, without selecting a target bound."""
    value = _space(value).strip(" ()")
    scalar = re.fullmatch(_NUMERIC_PART, value)
    if scalar:
        return [_decimal(value.replace("−", "-"))], "scalar"
    interval = re.fullmatch(r"(?:от\s+)?(" + _NUMERIC_PART + r")\s*(?:[-–—]|\.\.|до)\s*(" + _NUMERIC_PART + r")", value, re.I)
    if interval:
        bounds = [_decimal(part.replace("−", "-")) for part in interval.groups()]
        return sorted(bounds), "range"
    return None, None


def _numeric_observation(raw, source_label, requirement):
    # Remove unit tokens before reading numbers: the 2 in г/м2 is not data.
    clean = _space(_MEASURE_UNIT.sub("", raw)).replace("−", "-")
    tolerance = re.split(r"\s*(?:±|\+\s*/\s*-|\+\s*-)\s*", clean)
    concept = _attribute_concept(source_label)
    metadata = {}
    if len(tolerance) == 2:
        nominal, kind = _numeric_segment(tolerance[0])
        offsets, offset_kind = _numeric_segment(tolerance[1])
        if not nominal or not offsets or min(offsets) < 0:
            return None
        radius = max(offsets)
        # A percentage tolerance of a count/mass is relative. Whiteness on
        # the named CIE/ISO scale, or an already percent-valued measurement,
        # retains absolute percentage points.
        raw_nominal = re.split(r"±|\+\s*/\s*-|\+\s*-", raw)[0]
        relative = (bool(re.search(r"(?:±|\+\s*/\s*-|\+\s*-)\s*\d+(?:[.,]\d+)?\s*%", raw))
                    and "%" not in raw_nominal and concept not in {"whiteness_cie", "whiteness_iso"})
        if relative:
            low, high = nominal[0] * (1 - radius / 100), nominal[-1] * (1 + radius / 100)
        else:
            low, high = nominal[0] - radius, nominal[-1] + radius
        number = [low, high]
        operation = "explicit_relative_tolerance" if relative else "explicit_tolerance_bounds"
        metadata.update(nominal=float(nominal[0]) if kind == "scalar" else [float(n) for n in nominal],
                        tolerance=float(offsets[0]) if offset_kind == "scalar" else [float(n) for n in offsets],
                        tolerance_kind="relative_percent" if relative else "absolute")
        if kind == "range" or offset_kind == "range":
            operation = "explicit_range_tolerance_envelope"
            metadata["normalization_reason"] = "Источник одновременно задаёт диапазон и допуск; сохранены внешние границы без выбора номинального значения."
            metadata["source_ambiguity"] = "range_with_tolerance"
    elif len(tolerance) == 1:
        number, kind = _numeric_segment(clean)
        if number:
            if kind == "scalar":
                number, operation = number[0], "single_explicit_value"
            else:
                operation = "explicit_range_bounds"
        else:
            numbers = _explicit_numbers(clean)
            if not numbers or any(n is None or not n.is_finite() for n in numbers):
                return None
            label = str(requirement.get("label") or requirement.get("key") or "")
            # An explicitly requested maximum/minimum of listed settings is
            # meaningful; an ordinary fact never selects a convenient member.
            if re.search(r"максим|maximum|\bmax\b", label, re.I):
                number, operation = max(numbers), "maximum_of_explicit_values"
            elif re.search(r"миним|minimum|\bmin\b", label, re.I):
                number, operation = min(numbers), "minimum_of_explicit_values"
            elif len(set(numbers)) == 1:
                number, operation = numbers[0], "equal_explicit_values"
            else:
                return None
        # A speed range states the maximum as its upper end, preserving the
        # established maximum/minimum attribute semantics and its source range.
        label = str(requirement.get("label") or requirement.get("key") or "")
        if isinstance(number, list) and re.search(r"максим|maximum|\bmax\b|миним|minimum|\bmin\b", label, re.I):
            metadata["source_range"] = [float(n) for n in number]
            is_max = bool(re.search(r"максим|maximum|\bmax\b", label, re.I))
            number, operation = (number[-1] if is_max else number[0]), ("maximum_of_explicit_values" if is_max else "minimum_of_explicit_values")
    else:
        return None
    values = number if isinstance(number, list) else [number]
    if any(n is None or not n.is_finite() for n in values):
        return None
    return number, operation, metadata


def _normal_observation(row, requirement):
    raw, label = row["raw_value"], str(requirement.get("label") or requirement.get("key") or "")
    expected = requirement.get("value")
    numeric = isinstance(expected, (float, int)) and not isinstance(expected, bool) or (
        isinstance(expected, (list, tuple)) and all(isinstance(v, (float, int)) and not isinstance(v, bool) for v in expected))
    result = dict(row)
    if numeric:
        source_label = row.get("raw_label", "")
        parsed = _numeric_observation(raw, source_label, requirement)
        if parsed is None:
            return None
        number, operation, metadata = parsed
        result.update(metadata)
        target = _measure_unit(requirement.get("unit"))
        units = _observed_units(raw)
        label_units = _observed_units(source_label, label=True)
        concept = _attribute_concept(source_label)
        if row.get("basis") == "primary_product_title_attribute" and row.get("source_concept") in {"format", "paper_grade", "sheets_per_pack"}:
            concept = row["source_concept"]
        unit_basis = "row_value"
        if metadata.get("tolerance_kind") == "relative_percent":
            units = {unit for unit in units if unit[0] != "percent"}
        if concept == "density_gsm" and any(unit[0] == "mass" and unit[2] == "г" for unit in units) and re.search(r"площадью\s+1\s*(?:кв\.?\s*м|м[²2])", source_label, re.I):
            units = {_measure_unit("г/м²")}
            unit_basis = "row_label_and_value"
        elif not units and concept == "sheets_per_pack":
            units, unit_basis = {_measure_unit("листов")}, "row_label"
            if row.get("basis") == "primary_product_title_attribute" and re.fullmatch(r"л\.?", source_label, re.I):
                unit_basis = "paper_title_sheet_abbreviation"
        elif not units and label_units:
            units, unit_basis = label_units, "row_label"
        # CIE names the scale itself; a percent sign is often absent. Keep
        # that source unit explicit instead of inventing a percent token.
        if not units and concept == "whiteness_cie":
            result.update(source_unit="CIE", normalized_unit="CIE", unit_basis="named_CIE_scale")
        elif requirement.get("unit") and not units:
            result.update(source_unit=None, normalized_unit=None, unit_reason="source_unit_not_stated")
        if requirement.get("unit"):
            if not units:
                pass
            elif target is None:
                # An unfamiliar unit can only pass verbatim in an otherwise
                # scalar/range value; no guessed conversion or partial suffix.
                literal = re.escape(_space(requirement["unit"]))
                if not re.fullmatch(r"(?:до\s+|от\s+)?\d+(?:[.,]\d+)?(?:\s*(?:[-–—/]|до|или)\s*\d+(?:[.,]\d+)?)?\s*" + literal, raw, re.I):
                    return None
                result.update(source_unit=requirement["unit"], normalized_unit=requirement["unit"], unit_basis=unit_basis)
            else:
                if len(units) != 1:
                    return None
                source = next(iter(units))
                if source[0] != target[0]:
                    return None
                number = [n * source[1] / target[1] for n in number] if isinstance(number, list) else number * source[1] / target[1]
                result.update(source_unit=source[2], normalized_unit=requirement["unit"], unit_basis=unit_basis)
                if source[1] != target[1]:
                    operation += ";unit_conversion"
        elif len(units) == 1:
            result.update(source_unit=next(iter(units))[2], normalized_unit=next(iter(units))[2], unit_basis=unit_basis)
        result.update(value=[float(n) for n in number] if isinstance(number, list) else float(number), normalization=operation)
    else:
        value = raw
        operations = []
        if _attribute_concept(label) == "format" and re.fullmatch(r"[aа][0-6]", raw, re.I):
            value = "A" + raw[-1]
            operations.append("paper_format_alphabet")
        if _attribute_concept(label) == "paper_grade" and re.fullmatch(r"[abcавс]", raw, re.I):
            value = raw.upper().translate(str.maketrans({"А": "A", "В": "B", "С": "C"}))
            operations.append("paper_grade_alphabet")
        if _attribute_concept(label) == "dimensions":
            value = re.sub(r"(?<=\d)\s*[хx×]\s*(?=\d)", "x", value, flags=re.I)
            operations.append("dimension_separator")
        if _attribute_concept(label) == "materials":
            value = re.sub(r"\bи\s+др\.$", "и другие", value, flags=re.I)
            operations.append("written_abbreviation")
        result.update(value=value, normalization=";".join(operations) or "visible_text")
    return result


def _description_attribute_rows(document):
    context = primary_product_context(document)
    if not context:
        return []
    text, rows = context["text"], []
    # These explicit clauses state the material/temperature, rather than a
    # number adjacent to an unrelated mention of a platform or drying system.
    patterns = [
        ("Материал рабочей платформы", r"(?:печатная|рабочая)\s+платформа\s+[^.]{0,150}\.\s+Она\s+выполнена\s+из\s+(?P<value>[^.]{3,350})"),
        ("Максимальная температура отсека для сушки", r"Для\s+(?:предварительной\s+)?сушки\s+(?:филамента|пластика)\s+при\s+температуре\s+(?P<value>\d+(?:[.,]\d+)?(?:\s*(?:или|/|-)\s*\d+(?:[.,]\d+)?)?\s*°\s*[СC])\s+[^.]{0,120}отсек"),
    ]
    for label, pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            quote = match.group()
            if quote in _space(document.get("text")):
                rows.append({"raw_label": label, "raw_value": match["value"], "quote": quote,
                             "locator": "primary-description", "basis": "primary_product_attribute"})
    return rows


def _paper_title_attribute_rows(document):
    """Explicit paper facts in the verified H1, used only as a row fallback."""
    bound = _primary_scope(document)
    if not bound:
        return []
    dom, scope, primary, _ = bound
    if not re.search(r"\b(?:фото)?бумаг[аи]\b|\bpaper\b", primary, re.I):
        return []
    heading = next((node for node in dom.nodes if node.tag == "h1" and not node.hidden), None)
    visible = _space(document.get("text"))
    if heading is None or _identity(heading.text()) != _identity(primary) or not _captured_quote(heading.text(), visible):
        return []
    title, result = heading.text(), []
    patterns = [
        ("format", r"(?<![a-zа-яё0-9])(?P<value>[aа][0-6])(?![a-zа-яё0-9+])"),
        ("paper_grade", r"(?P<label>(?:марка|класс)(?:\s+бумаги)?)\s*[:—-]?\s*[«\"']?(?P<value>[abcавс])(?=$|[\s,.;»\"'/–—-])"),
        ("sheets_per_pack", r"(?<![\w.,])(?P<value>\d+)\s*(?P<label>лист(?:ов|а)?\b|л\.(?=$|[\s,;])|л(?=$|[\s,;]))"),
    ]
    for concept, pattern in patterns:
        matches = list(re.finditer(pattern, title, re.I))
        ambiguous = False
        for match in matches:
            before, after = title[:match.start()], title[match.end():]
            if concept == "paper_grade":
                ambiguous = ambiguous or bool(re.match(r"\s*(?:[/–—-]|или\b|or\b)\s*[abcавс](?!\w)", after, re.I))
            elif concept == "sheets_per_pack":
                ambiguous = ambiguous or bool(re.search(r"\d\s*(?:[-–—/]|или|or)\s*$", before, re.I)
                                              or re.match(r"\s*(?:±|\+\s*/\s*-|[-–—/]\s*\d|(?:или|or)\s+\d)", after, re.I))
        if ambiguous:
            continue
        for match in matches:
            quote = _captured_quote(match.group(), visible)
            if quote:
                result.append({"raw_label": match.groupdict().get("label") or match["value"],
                               "raw_value": match["value"], "quote": quote,
                               "locator": _locator(heading), "basis": "primary_product_title_attribute",
                               "source_concept": concept})
    return result


def primary_requirement_observations(document, request):
    """Map observed supplier facts to the request's actual keys, never its values."""
    rows = primary_attribute_rows(document)
    prose = _description_attribute_rows(document)
    titles = _paper_title_attribute_rows(document)
    result = {}
    for requirement in (request.get("requirements") or []):
        if not isinstance(requirement, dict) or not requirement.get("key"):
            continue
        concept = _attribute_concept(requirement.get("label") or requirement["key"])
        matches = [row for row in rows if _attribute_concept(row["raw_label"]) == concept]
        if not matches:
            matches = [row for row in prose if _attribute_concept(row["raw_label"]) == concept]
        if not matches:
            matches = [row for row in titles if row["source_concept"] == concept]
        # A generic speed range does not establish a recommended/optimal speed.
        if re.search(r"оптимальн|рекомендуем|optimal|recommended", str(requirement.get("label")), re.I):
            matches = [row for row in matches if re.search(r"оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        elif re.search(r"максим|maximum|\bmax\b", str(requirement.get("label")), re.I):
            matches = [row for row in matches if not re.search(r"миним|minimum|\bmin\b|оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        elif re.search(r"миним|minimum|\bmin\b", str(requirement.get("label")), re.I):
            matches = [row for row in matches if not re.search(r"максим|maximum|\bmax\b|оптимальн|рекомендуем|optimal|recommended", row["raw_label"], re.I)]
        facts = [fact for row in matches if (fact := _normal_observation(row, requirement)) is not None]
        if facts and len({repr(fact["value"]) for fact in facts}) == 1:
            result[requirement["key"]] = facts[0]
    return result


def primary_product_images(document):
    """Return safe primary image DOM attributes, not invented text quotations."""
    from .acquisition import AcquisitionError, _normalize_url
    bound = _primary_scope(document)
    html = document.get("html")
    if bound:
        dom, scope, primary, _ = bound
    elif isinstance(html, str) and (dom := _parsed_dom(html)):
        # Minimal H1 + one image captures have no surrounding purchase root.
        # Admit only that unambiguous shape, never a catalogue or navigation.
        primary = _space(document.get("primary_title") or document.get("title"))
        headings = [n for n in dom.nodes if n.tag in {"h1", "h2", "h3"} and not n.hidden]
        images = [n for n in dom.nodes if n.tag == "img" and not n.hidden]
        if len(headings) != 1 or _identity(headings[0].text()) != _identity(primary) or len(images) != 1:
            return []
        scope = dom.root
    else:
        return []
    results, seen = [], set()
    for node in dom.nodes:
        if node.tag != "img" or scope not in node.ancestors():
            continue
        lineage = list(node.ancestors())
        if any(_excluded(part, scope) for part in lineage[:lineage.index(scope) + 1]) or _foreign_local_title(node, scope, primary):
            continue
        labels = " ".join(part.label for part in lineage[:lineage.index(scope) + 1])
        if re.search(r"logo|icon|loader|payment|banner|avatar|certificate|sertifikat|brand[-_]", labels, re.I):
            continue
        gallery = bool(re.search(r"(?:product|detail|item)[-_]?(?:gallery|image|photo)|gallery", labels, re.I))
        named = _identity(primary) and _identity(primary) in _identity(node.attrs.get("alt") or node.attrs.get("title"))
        if scope is not dom.root and not (gallery or named or node.attrs.get("itemprop") == "image"):
            continue
        attrs = [(node, key) for key in ("data-src", "src") if node.attrs.get(key)]
        if gallery and node.parent and node.parent.tag == "a" and node.parent.attrs.get("href"):
            attrs.insert(0, (node.parent, "href"))
        for owner, attribute in attrs:
            raw = owner.attrs[attribute]
            try:
                url = _normalize_url(urljoin(document.get("url", ""), raw))
            except AcquisitionError:
                continue
            if not re.search(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", url, re.I) or re.search(r"(?:^|[/_.-])(?:logo|icon|loader|placeholder|no[-_]?photo|no[-_]?image)(?:[/_.-]|$)", url, re.I):
                continue
            if url not in seen:
                seen.add(url)
                results.append({"url": url, "basis": "primary_product_dom", "locator": _locator(owner),
                                "attribute": attribute, "raw_value": raw})
            break
    return results


_PACK_MASS = re.compile(r"(?<![\w\d])(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>мкг|µg|μg|ug|мг|mg)\b", re.I)


def requested_variant_mass(request):
    for requirement in request.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        label = str(requirement.get("label") or requirement.get("key") or "")
        unit = str(requirement.get("unit") or "").lower()
        value = _decimal(str(requirement.get("value")))
        if value is not None and value.is_finite() and re.search(r"вес|масс|фасов|mass", label, re.I) and unit in {"мкг", "µg", "μg", "ug", "мг", "mg"}:
            return value * (1000 if unit in {"мг", "mg"} else 1), requirement.get("op", "eq")
    return None


def source_variant_issues(document):
    """Surface contradictory mass labels, without inventing which one is right."""
    context = primary_product_context(document)
    if not context:
        return []
    bound = _primary_scope(document)
    conflicts = set()
    if bound:
        dom, scope, primary, _ = bound
        _table_price_contexts(dom, scope, primary, context["text"], conflicts)
    if conflicts:
        return ["Наименование в ценовой таблице не связано с основным товаром: «" +
                _active_text(row, bound[1])[:240] + "». Цена требует проверки." for row in conflicts]
    heading = list(_PACK_MASS.finditer(context["primary_title"]))
    sku = re.search(r"(?:кат\.?\s*(?:№|номер)|артикул|sku)\s*[:№]?\s*(\S+(?:\s*(?:мкг|µg|μg|ug|мг|mg))?)", context["text"], re.I)
    packing = _PACK_MASS.search(sku[1]) if sku else None
    if len(heading) == 1 and packing:
        a, b = heading[0], packing
        first = _decimal(a["amount"]) * (1000 if a["unit"].lower() in {"мг", "mg"} else 1)
        second = _decimal(b["amount"]) * (1000 if b["unit"].lower() in {"мг", "mg"} else 1)
        if first != second:
            return [f"Фасовка источника противоречива: в заголовке «{a.group()}», в каталожном артикуле «{b.group()}». Цена не связана с однозначной фасовкой."]
    return []


def _table_price_contexts(dom, scope, primary, focus, conflicts=None):
    results = []
    for table in (n for n in dom.nodes if n.tag == "table" and scope in n.ancestors()):
        if any(_excluded(n, scope, True) for n in table.ancestors() if scope in n.ancestors()):
            continue
        rows = [n for n in dom.nodes if n.tag == "tr" and next((a for a in n.ancestors() if a.tag == "table"), None) is table]
        price_column, currency_quote, currency, name_column, header_row = None, None, None, None, None
        for row in rows:
            cells = [n for n in row.children if n.tag in {"td", "th"}]
            column = 0
            for cell in cells:
                label = _active_text(cell, scope)
                if re.fullmatch(r"наименование(?:\s+(?:товара|продукции))?|название(?:\s+товара)?|product(?:\s+name)?|name", label, re.I):
                    name_column = column
                if re.search(r"цен[аы]|price", label, re.I):
                    currency = "RUB" if re.search(r"руб|RUB|₽", label, re.I) else "USD" if re.search(r"USD|\$", label) else "EUR" if re.search(r"EUR|€", label) else None
                    if currency:
                        price_column, currency_quote = column, label
                try:
                    column += min(20, max(1, int(cell.attrs.get("colspan", "1"))))
                except ValueError:
                    column += 1
            if price_column is not None:
                header_row = row
                break
        if price_column is None:
            continue
        for row in rows:
            if row is header_row:
                continue
            cells = [n for n in row.children if n.tag == "td"]
            slots, column = {}, 0
            for cell in cells:
                try:
                    span = min(20, max(1, int(cell.attrs.get("colspan", "1"))))
                except ValueError:
                    span = 1
                for index in range(column, column + span):
                    slots[index] = cell
                column += span
            price_cell = slots.get(price_column)
            if price_cell is None or len(cells) < 2:
                continue
            if name_column is not None:
                name = _active_text(slots[name_column], scope) if name_column in slots else ""
                expected, observed = _identity(primary), _identity(name)
                if len(observed) < 4 or not (observed in expected or expected in observed):
                    if conflicts is not None:
                        conflicts.add(row)
                    continue
            before = cells[:cells.index(price_cell)]
            row_quote = _active_text(row, scope)
            quote = _active_text(price_cell, scope)
            if not row_quote or row_quote not in focus or not re.fullmatch(_NUMBER, quote):
                continue
            sku = _active_text(cells[0], scope)
            packing = next((_PACK_MASS.search(_active_text(cell, scope)) for cell in before if _PACK_MASS.search(_active_text(cell, scope))), None)
            if not re.search(r"[a-zа-яё].*\d|\d.*[a-zа-яё]", sku, re.I) or not packing:
                continue
            amount = _decimal(quote)
            if amount is None or amount <= 0:
                continue
            mass = _decimal(packing["amount"]) * (1000 if packing["unit"].lower() in {"мг", "mg"} else 1)
            results.append({"quote": quote, "context": row_quote, "amount": amount,
                            "currency": currency, "currency_quote": currency_quote,
                            "method": "primary_visible_table", "primary_title": primary,
                            "variant": {"sku": sku, "packing": packing.group(), "mass_mcg": float(mass), "quote": row_quote}})
    return results


def primary_price_contexts(document):
    """Return only visible monetary blocks associated with the captured H1.

    A Product item scope or a product-detail purchase container binds a price
    to the primary item. Recommendation/card scopes, struck/hidden amounts,
    supplementary services and alternate product links are excluded. Schema
    metadata alone never supplies a price or currency.
    """
    html, visible = document.get("html"), _space(document.get("text"))
    if not isinstance(html, str) or not html or not visible:
        return []
    bound = _primary_scope(document)
    product = primary_product_context(document)
    if bound is None or product is None:
        return []
    dom, primary_scope, primary, strong = bound
    focus = product["text"]
    conflicting_rows = set()
    results = [block for block in _table_price_contexts(dom, primary_scope, primary, focus, conflicting_rows)
               if block["quote"] in visible]
    for node in dom.nodes:
        value = node.text()
        if node.hidden or not value or len(value) > 500 or not (_PRICE.search(node.label) or node.attrs.get("itemprop") == "price"):
            continue
        amounts = list(_AMOUNT.finditer(value))
        if not amounts or len({_decimal(match["number"]) for match in amounts}) != 1:
            continue
        chain = list(node.ancestors())
        if any(part in conflicting_rows for part in chain):
            continue
        if primary_scope not in chain or any(_excluded(part, primary_scope, True) for part in chain if primary_scope in part.ancestors()):
            continue
        if _foreign_local_title(node, primary_scope, primary):
            continue
        if _foreign_price_owner(node, primary_scope, primary):
            continue
        # A product card is a separate item, even if rendered before the main
        # buy block (for example an offered analogue).
        if any(re.search(r"product[-_](?:item|card)(?![-_]detail)|productBlock|catalog[-_]item", part.label, re.I) for part in chain):
            continue
        scope = next((part for part in chain if "schema.org/Product" in part.attrs.get("itemtype", "")), None)
        purchase = next((part for part in chain if _PURCHASE.search(part.label) or _PRIMARY_ROOT.search(part.label)), None)
        if scope:
            names = [part.text() or part.attrs.get("content", "") for part in dom.nodes if part.attrs.get("itemprop") == "name" and scope in part.ancestors()]
            names = [name for name in names if name]
            if names and not any(_identity(name) == _identity(primary) for name in names):
                continue
        if not scope and not purchase:
            # Small standalone pages can bind H1 and price in the same article.
            article = next((part for part in chain if part.tag in {"article", "main"} and any(child.tag == "h1" for child in part.children)), None)
            if not article or len(article.text()) > 2000:
                continue
        if node.parent and _BAD_TEXT.search(node.parent.text()) and len(node.parent.text()) <= 500:
            continue
        context = value
        bounded_group = bool(_PRICE.search(node.label))
        tax_quote = None
        # Include the actual price line and its immediate terms, but do not
        # absorb separate warranty widgets or unrelated page paragraphs.
        for parent in chain[1:4]:
            expanded = parent.text()
            if not expanded or len(expanded) > 700 or _BAD_TEXT.search(expanded):
                if not bounded_group and len(expanded) > 700:
                    context = ""
                break
            prices = {_decimal(match["number"]) for match in _AMOUNT.finditer(expanded)}
            if prices != {_decimal(amounts[0]["number"])}:
                # Several prices in the same local row are ambiguous. Only an
                # explicitly current-price element can exclude a struck old
                # amount whose own subtree was already discarded above.
                active_prices = {_decimal(match["number"]) for match in _AMOUNT.finditer(_active_text(parent, primary_scope, primary, True))}
                if active_prices != {_decimal(amounts[0]["number"])}:
                    context = ""
                break
            if expanded in focus:
                context = expanded
                bounded_group = bounded_group or bool(_PRICE.search(parent.label))
            taxes = {match.group() for match in _TAX.finditer(expanded) if match.group() in focus}
            if len(taxes) == 1:
                tax_quote = next(iter(taxes))
            elif len(taxes) > 1:
                tax_quote = None
            if _PRICE.search(parent.label):
                break
            if parent is scope or parent is purchase:
                break
        if not context or context not in focus or _BAD_TEXT.search(context):
            continue
        # Include tier labels from the enclosing row/short purchase block;
        # a short amount-only descendant cannot hide an order threshold.
        for ancestor in chain[1:]:
            expanded = ancestor.text()
            tier = _TIER.search(expanded)
            if tier:
                # Preserve a nearby order threshold even when an excluded
                # add-to-cart widget interrupts the active-text string. Use
                # a contiguous captured span, stopping before delivery lists.
                start = expanded.find(value)
                bounded = _space(expanded[start:tier.end()]) if start >= 0 and tier.end() >= start + len(value) else expanded
                if len(bounded) > 2000 or not _captured_quote(bounded, visible):
                    context = ""
                else:
                    context = bounded
                break
            if ancestor is scope or ancestor is purchase:
                break
        if not context:
            continue
        for match in amounts:
            quote = match.group()
            captured = _captured_quote(quote, visible)
            if quote not in focus or captured is None or re.search(r"(?:\bот|\bfrom)\s*$", value[:match.start()], re.I):
                continue
            result = {"quote": captured, "context": _captured_quote(context, visible) or context, "amount": _decimal(match["number"]), "method": "primary_visible_dom", "primary_title": primary}
            if tax_quote:
                result["tax_quote"] = tax_quote
            if result not in results:
                results.append(result)
    return results

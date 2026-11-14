"""Deterministic page-shape classification; source contents remain untrusted.

A category's first tile is discovery evidence, never the category's own price.
URL paths alone do not establish a page type: product pages also use /catalog/.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


_MODEL = re.compile(
    r"(?<!\w)(?:[a-zа-яё]+[-_/][a-zа-яё0-9]*\d[a-zа-яё0-9]*(?:[-_/.][a-zа-яё0-9]+)*"
    r"|[a-z]+\d[a-z0-9]*(?:[-_/.][a-zа-яё0-9]+)*"
    r"|(?-i:[A-ZА-ЯЁ]{2,})\s+\d{2,}(?:[-_/.][a-zа-яё0-9]+)*)(?!\w)", re.I)
_NON_MODELS = {"гб", "gb", "мгц", "mhz", "ghz", "ram", "ssd", "ddr", "rub", "usd", "eur", "шт", "цена", "price"}
_RELATED = re.compile(r"Рекомендуем(?:ые|\s+также)|Вас\s+также\s+может|Похожие\s+товары|С\s+этим\s+товаром\s+покупают|Сопутствующие\s+товары|related\s+products|you\s+may\s+also\s+like", re.I)
_LIST_HEADING = re.compile(r"\bкаталог\b|результаты\s+поиска|поиск\s+товаров|выбрать\s+и\s+заказать|product\s+search|search\s+results", re.I)
_SORT = re.compile(r"сортиров\w*|сортировать|показывать\s+по|показать\s+по|sort\s+by|items\s+per\s+page", re.I)
_FACET = re.compile(r"\bфильтр\w*|подбор\s+по\s+параметрам|сбросить\s+(?:все\s+)?фильтры|filter\s+by", re.I)
_COUNT = re.compile(r"(?:найдено|показано|всего)\s*(?:товаров\s*[:—-]?\s*)?\d+|\d+\s*[-–—]\s*\d+\s+из\s+\d+\s+товар|(?:found|showing)\s+\d+\s+(?:products|results|items)\b", re.I)
_EDITORIAL_HEADING = re.compile(r"\b(?:знакомимся|знакомство|обзор|обзоры|сравниваем|сравнение|тестирование|распаковка|новости|анонс|инструкция|unboxing|review|comparison|introducing)\b|\bкак\s+(?:выбрать|работает|использовать)|\bчто\s+такое", re.I)
_EDITORIAL_TYPES = {"Article", "BlogPosting", "NewsArticle", "TechArticle", "ReportageNewsArticle"}
_PRODUCT_CLASS = re.compile(r"(?:product|tovar|goods|catalog)[_-](?:item|title|name)|(?:item|card)[_-](?:product|title)|product(?:-link|-name|-title)|catalog(?:Card|[-_]list[-_]item)", re.I)
_PRODUCT_TITLE_CLASS = re.compile(r"(?:product|tovar|goods|catalog|item|card)[\w-]*(?:title|name)|(?:title|name)[\w-]*(?:product|tovar|goods|catalog|item|card)", re.I)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _models(value):
    result = set()
    for match in _MODEL.finditer(_space(value)):
        if re.fullmatch(r"[aа][0-6]", match.group(), re.I):
            continue  # A paper format is not a distinctive product model.
        prefix = re.sub(r"\W", "", re.split(r"\d", match.group())[0].casefold())
        if prefix not in _NON_MODELS:
            result.add(re.sub(r"[^a-zа-яё0-9]", "", match.group().casefold()))
    return result


class _ShapeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1, self.h1_seen, self.heading = False, False, []
        self.json_script, self.script_parts, self.schemas = False, [], []
        self.anchor, self.links = None, []
        self.itemlist = False
        self.stack = []
        self.related_seen = False
        self.editorial_meta = False
        self.article_markup = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and str(attrs.get("property") or attrs.get("name") or "").casefold() == "og:type":
            self.editorial_meta |= str(attrs.get("content") or "").casefold() == "article"
        self.article_markup |= tag == "article" and not bool(re.search(r"product|catalog[-_]detail", str(attrs.get("class") or ""), re.I))
        parent_product = self.stack[-1][1] if self.stack else False
        parent_navigation = self.stack[-1][2] if self.stack else False
        parent_title = self.stack[-1][3] if self.stack else False
        product_context = parent_product or bool(_PRODUCT_CLASS.search(attrs.get("class", "")))
        product_title = parent_title or bool(_PRODUCT_TITLE_CLASS.search(attrs.get("class", "")))
        navigation = parent_navigation or tag in {"nav", "header", "footer"}
        if tag not in _VOID_TAGS:
            self.stack.append((tag, product_context, navigation, product_title))
        if tag == "h1" and not self.h1_seen:
            self.h1, self.h1_seen = True, True
        if tag == "script" and attrs.get("type", "").casefold().split(";", 1)[0] == "application/ld+json":
            self.json_script, self.script_parts = True, []
        if "schema.org/ItemList" in attrs.get("itemtype", ""):
            self.itemlist = True
        if tag == "a" and attrs.get("href"):
            self.anchor = [attrs["href"], [], product_context, self.h1_seen, navigation, product_title, self.related_seen]

    def handle_endtag(self, tag):
        if tag == "h1":
            self.h1 = False
        if tag == "script" and self.json_script:
            try:
                self.schemas.append(json.loads("".join(self.script_parts)))
            except (ValueError, RecursionError):
                pass
            self.json_script = False
        if tag == "a" and self.anchor:
            href, parts, product_class, after_heading, navigation, product_title, related = self.anchor
            self.links.append({"url": href, "title": _space(" ".join(parts)), "product_class": product_class, "after_heading": after_heading, "navigation": navigation, "product_title": product_title, "related": related})
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.json_script and _RELATED.search(data):
            self.related_seen = True
        if self.json_script:
            self.script_parts.append(data)
        elif self.h1:
            self.heading.append(data)
        if self.anchor and not self.json_script:
            self.anchor[1].append(data)


def _schema_nodes(value):
    # Iteration bounds malformed/unusually deep source markup without recursion.
    pending, seen = [value], 0
    while pending and seen < 10000:
        node = pending.pop()
        seen += 1
        if isinstance(node, dict):
            yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def _types(node):
    value = node.get("@type", [])
    return {str(item).rsplit("/", 1)[-1] for item in (value if isinstance(value, list) else [value])}


def _link_url(base, value):
    try:
        parsed = urlsplit(urljoin(base, str(value or "")))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if re.search(r"\.(?:pdf|png|jpe?g|gif|webp|zip|docx?)(?:$)", parsed.path, re.I):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


def classify_page(document):
    """Return {kind, reason, product_links} without mutation, I/O, or guessing URLs.

    A specific primary product identity wins over recommendation widgets. A
    generic heading needs independent listing structure, so an incomplete
    genuine product page can remain unknown and still be validated separately.
    """
    document = document if isinstance(document, dict) else {}
    parser = _ShapeParser()
    try:
        parser.feed(str(document.get("html") or ""))
    except (ValueError, RecursionError):
        pass
    heading = _space(document.get("primary_title") or " ".join(parser.heading) or document.get("title"))
    base = str(document.get("url") or "")
    text = _space(document.get("text"))
    heading_start = text.find(heading) if heading else -1
    if heading_start >= 0:
        text = text[heading_start:]
    # Recommendations do not turn the primary product into a catalogue.
    cut = _RELATED.search(text)
    main_text = text[:cut.start()] if cut else text
    products, itemlist, editorial_schema = [], parser.itemlist, False
    for schema in parser.schemas:
        for node in _schema_nodes(schema):
            types = _types(node)
            editorial_schema |= bool(types & _EDITORIAL_TYPES) and _space(node.get("headline") or node.get("name")).casefold() == heading.casefold()
            itemlist |= bool(types & {"ItemList", "CollectionPage", "SearchResultsPage"})
            if "Product" in types and _space(node.get("name")):
                products.append(node)
    product_names = {_space(product["name"]).casefold() for product in products}
    heading_models = _models(heading)
    heading_normal = heading.casefold()
    list_controls = bool(_SORT.search(main_text) or _COUNT.search(main_text))
    matching_schema = any(
        heading_normal and heading_normal == _space(product["name"]).casefold()
        and (heading_models or product.get("sku") or product.get("mpn") or product.get("productID") or len(heading.split()) >= 2)
        for product in products
    ) and not list_controls

    source_links = [item for item in document.get("links", []) if isinstance(item, dict)] if isinstance(document.get("links"), list) else []
    parsed_urls = {_link_url(base, link["url"]) for link in parser.links}
    source_links = parser.links + [link for link in source_links if _link_url(base, link.get("url")) not in parsed_urls]
    has_product_cards = any(link.get("product_class") and link.get("title") for link in parser.links)
    found, seen, main_models = [], set(), set()
    main_product_count = 0
    own_url = _link_url(base, base)
    for link in source_links:
        url, title = _link_url(base, link.get("url")), _space(link.get("title"))
        if not url or url == own_url or url in seen or not title:
            continue
        # Labels/model identities and captured product markup establish detail
        # candidates. Navigation URL paths alone cannot establish a product.
        score = 6 if title.casefold() in product_names else 3 if _models(title) else 1 if link.get("product_title") else 0
        if link.get("navigation") or has_product_cards and not link.get("product_class") and title.casefold() not in product_names:
            score = 0
        if re.search(r"/(?:filter|search)(?:/|$)", urlsplit(url).path, re.I):
            score = 0
        if score:
            score += 3 * bool(link.get("after_heading")) + 2 * bool(link.get("product_class"))
        if score and not _LIST_HEADING.search(title):
            found.append((score, {"title": title[:240], "url": url}))
            seen.add(url)
            if not link.get("related"):
                main_models.update(_models(title))
                main_product_count += 1
    found.sort(key=lambda pair: -pair[0])
    product_links = [link for _, link in found[:80]]
    result = {"kind": "unknown", "reason": "Недостаточно признаков отдельного товара или списка товаров.", "product_links": product_links}
    # A concrete primary purchase block belongs to the H1's own product.
    # Large store headers/recommendations can also contain sorting controls
    # and unrelated model links; those do not turn the detail into a listing.
    from .price_context import primary_product_context, primary_price_contexts, _primary_scope
    context = primary_product_context(document)
    bound = _primary_scope(document)
    detail_bound = bool(bound and (any(node.tag == "h1" and bound[1] in node.ancestors() for node in bound[0].nodes)
        or re.search(r"catalog[-_]detail|product[-_]detail|catalog[-_]element|single[-_]product", bound[1].label, re.I)))
    own_purchase = bool(context and context.get("strong_identity") and detail_bound and primary_price_contexts(document)
                        and (not list_controls or len(heading.split()) >= 3))
    editorial_path = bool(re.search(r"/(?:blog|blogs|news|articles?|obzory|reviews?)(?:/|$)", urlsplit(base).path, re.I))
    editorial = editorial_schema or parser.editorial_meta or bool(_EDITORIAL_HEADING.search(heading)) or editorial_path and parser.article_markup
    if document.get("is_listing") or str(document.get("page_type")) in {"listing", "category", "search"}:
        result.update(kind="listing", reason="Источник помечен как каталог или результаты поиска.")
    elif _LIST_HEADING.search(heading):
        result.update(kind="listing", reason="Основной заголовок обозначает каталог или результаты поиска.")
    elif editorial and not own_purchase:
        result.update(kind="article", reason="Редакционная статья или обзор, без собственного подтверждённого блока покупки; ссылки используются для исследования.")
    elif heading_models or matching_schema or own_purchase:
        result.update(kind="product", reason="Основной заголовок привязан к конкретной товарной карточке и её блоку покупки." if own_purchase else "Основной заголовок содержит конкретную модель или совпадает с Product schema.")
    elif itemlist and (not (cut or parser.related_seen) or main_product_count >= 2):
        result.update(kind="listing", reason="При общем заголовке разметка содержит список товаров или страницу коллекции.")
    elif heading and list_controls:
        result.update(kind="listing", reason="При общем заголовке есть сортировка или количество найденных товаров.")
    elif heading and _FACET.search(main_text) and main_product_count >= 2:
        result.update(kind="listing", reason="При общем заголовке есть фильтры и несколько ссылок на товары.")
    elif heading and len(main_models) >= 2:
        result.update(kind="listing", reason="При общем заголовке найдены ссылки на несколько разных моделей.")
    elif not heading and len(product_names) == 1 and not itemlist:
        result.update(kind="product", reason="Разметка Product описывает единственный товар.")
    return result

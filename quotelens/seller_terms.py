"""Commercial clauses observed in immutable seller captures.

An observed clause is not acceptance of a procurement contract.  In particular,
general delivery, payment and warranty text never populates ``conditions``.
Related pages are usable only when the primary capture visibly links to that
same seller's public page.  This module performs no network or storage writes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from .acquisition import AcquisitionError, _normalize_url
from .price_context import primary_price_contexts
from .source_policy import source_metadata


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}
_BLOCK = {"p", "li", "dt", "dd", "tr", "td", "th", "h2", "h3", "h4", "h5", "h6", "div", "section", "article"}
_FOREIGN = re.compile(r"(?:^|[\s_-])(?:related|recommend\w*|similar|accessor\w*|cross[-_]?sell|product[-_](?:item|card|thumb)|catalog[-_]item)(?:$|[\s_-])", re.I)
_DELIVERY = re.compile(r"\b(?:достав\w*|самовывоз|отгруз\w*|разгруз\w*|shipping|delivery)\b", re.I)
_PAYMENT = re.compile(r"оплат\w*|оплач\w*|плат[её]ж\w*|безналич\w*|отсроч\w*|предоплат\w*|постоплат\w*|payment", re.I)
_WARRANTY = re.compile(r"\bгаранти(?:я|и|ю|ей|йный|йная|йное|йные|йного|йных|йном|йным)\b|\bwarranty\b", re.I)
_TERMS = re.compile(r"условия\s+(?:продажи|покупки|поставки)|публичн\w*\s+оферт\w*|terms\s+(?:and\s+conditions|of\s+sale)|^(?:для\s+организаций|юридическим\s+лицам|обмен\s+и\s+возврат)$", re.I)
_LINK_LABEL = re.compile(r"^(?:(?:подробнее\s+)?(?:об?\s+)?(?:условия|способы|информация\s+о)\s+)?(?:доставк|оплат|гаранти|самовывоз|shipping|delivery|payment|warranty|terms|публичн\w*\s+оферт)|^условия\s+(?:продажи|покупки|поставки)|^(?:для\s+организаций|юридическим\s+лицам|обмен\s+и\s+возврат)$", re.I)
_TAX_INCLUDED = re.compile(r"(?<!\w)с\s+НДС\b|включая\s+НДС\b|вкл\.?\s*НДС\b|НДС\s+включ[её]н|включа\w*\s+НДС\b|incl(?:uding)?\.?\s*VAT\b", re.I)
_TAX_EXCLUDED = re.compile(r"без\s+НДС\b|НДС\s+не\s+включ[её]н|не\s+включа\w*\s+НДС\b|excl(?:uding)?\.?\s*VAT\b", re.I)
_ALL_PRICES = re.compile(r"\b(?:все|указанные|представленные|публикуемые)\s+цены\b|\bцены\s+(?:на\s+все\s+товары|на\s+сайте|в\s+(?:каталоге|интернет-магазине))\b|\ball\s+prices\b", re.I)
_LEGAL = re.compile(r"персональн\w*\s+(?:данн|информац)\w*|конфиденциальност|cookie|не\s+является\s+публичной\s+офертой", re.I)


class _Node:
    def __init__(self, tag, attrs, parent, order):
        self.tag, self.attrs, self.parent, self.order = tag, attrs, parent, order
        self.parts, self.children = [], []
        self.has_block_descendant = False
        marker = " ".join(attrs.get(key, "") for key in ("class", "id", "role"))
        # Tab panels are captured content a user can open; hidden navigation
        # links and arbitrary hidden spans are not visible source evidence.
        tab = attrs.get("role") == "tabpanel" or bool(re.search(r"(?:^|\s)(?:wc-tab|tab-pane|tab-panel)(?:\s|$)", marker))
        local_hidden = "hidden" in attrs or attrs.get("aria-hidden") == "true" or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I)) or bool(set(attrs.get("class", "").split()) & {"hidden", "hide", "d-none"})
        self.hidden = bool(parent and parent.hidden) or tag in _SKIP or (local_hidden and not tab)
        self.foreign = bool(parent and parent.foreign) or bool(_FOREIGN.search(marker))
        self.navigation = bool(parent and parent.navigation) or tag == "nav" or attrs.get("role") == "navigation"

    def text(self):
        return _space(" ".join(self.parts))


class _VisibleTerms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes = [], []

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) >= 30000 or len(self.stack) >= 100:
            raise ValueError("Seller terms DOM exceeds extraction limits")
        node = _Node(tag, {key: value or "" for key, value in attrs}, self.stack[-1] if self.stack else None, len(self.nodes))
        self.nodes.append(node)
        if self.stack:
            self.stack[-1].children.append(node)
        if tag in _BLOCK:
            for ancestor in self.stack:
                ancestor.has_block_descendant = True
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if not self.stack or self.stack[-1].hidden:
            return
        for node in self.stack:
            if not node.hidden:
                node.parts.append(value)


def _parsed(document):
    parser = _VisibleTerms()
    try:
        parser.feed(str(document.get("html") or ""))
    except (ValueError, RecursionError):
        return []
    return parser.nodes


def _public_url(value, base=None):
    try:
        result = _normalize_url(urljoin(base or "", value))
        host = urlsplit(result).hostname or ""
        # Acquisition validates DNS immediately before I/O.  Link discovery is
        # pure, but also rejects unqualified local host names here.
        return result if "." in host or ":" in host else None
    except (AcquisitionError, ValueError, TypeError):
        return None


def _host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def seller_terms_links(document):
    """Discover visible navigation to this seller's public commercial pages."""
    if not isinstance(document, dict):
        return []
    primary = _public_url(document.get("url"))
    text = _space(document.get("text"))
    if not primary:
        return []
    nodes = _parsed(document)
    links = [(node.attrs.get("href"), node.text()) for node in nodes
             if node.tag == "a" and not node.hidden and not node.foreign]
    if not document.get("html"):
        links = [(link.get("url"), _space(link.get("title"))) for link in document.get("links", []) if isinstance(link, dict)]
    found = []
    for href, label in links:
        if not href or not label or len(label) > 160 or label not in text or not _LINK_LABEL.search(label):
            continue
        kinds = [kind for kind, pattern in (("delivery", _DELIVERY), ("payment", _PAYMENT), ("warranty", _WARRANTY), ("terms", _TERMS)) if pattern.search(label)]
        if not kinds:
            continue
        url = _public_url(href, primary)
        if not url or _host(url) != _host(primary) or url == primary:
            continue
        if re.search(r"/(?:auth|login|personal|account|cart|basket|checkout|order)(?:[/.?]|$)", urlsplit(url).path, re.I):
            continue
        entry = {"url": url, "kind": kinds[0] if len(kinds) == 1 else "terms"}
        if entry not in found:
            found.append(entry)
    return found


def _proof(document, quote, **extra):
    return {"quote": quote, "url": document.get("url"),
            "document_id": document.get("document_id") or document.get("id"),
            "source_hash": document.get("sha256") or hashlib.sha256(str(document.get("text") or "").encode()).hexdigest(),
            "retrieved_at": document.get("retrieved_at"), **extra}


def _clauses(document, nodes):
    text = _space(document.get("text"))
    if not document.get("html"):
        # A plain-text capture has no visibility/layout claims to add. Retain
        # complete source lines or sentences, never arbitrary text windows.
        values = re.split(r"\n+|(?<=[.!?])\s+(?=[А-ЯA-Z])", str(document.get("text") or ""))
        return list(dict.fromkeys(_space(value) for value in values if 8 <= len(_space(value)) <= 1200 and _space(value) in text))
    # Browser and HTTP extraction may render case/spacing around inline price
    # spans differently. Match their characters, then quote the actual capture
    # text, preserving its spelling and punctuation rather than the DOM copy.
    compact, positions = [], []
    for index, char in enumerate(text):
        if not char.isspace():
            folded = char.casefold()
            compact.extend(folded)
            positions.extend([index] * len(folded))
    compact = "".join(compact)
    values = []
    for node in nodes:
        if node.hidden or node.foreign or node.navigation or node.tag not in _BLOCK:
            continue
        # Prefer paragraphs/leaf containers, so a broad parent cannot combine
        # unrelated footer, product cards and contract text into one clause.
        if node.has_block_descendant and node.tag != "tr":
            continue
        if node.tag in {"td", "th"} and node.parent and node.parent.tag == "tr":
            continue
        value = node.text()
        if not 8 <= len(value) <= 1200:
            continue
        start = compact.find("".join(value.casefold().split()))
        if start >= 0:
            length = len("".join(value.casefold().split()))
            quote = text[positions[start]:positions[start + length - 1] + 1]
            if quote not in values:
                values.append(quote)
    return values


def _tax_value(quote):
    excluded = bool(_TAX_EXCLUDED.search(quote))
    included = bool(_TAX_INCLUDED.search(_TAX_EXCLUDED.sub("", quote)))
    return "excluded" if excluded and not included else "included" if included and not excluded else None


def _kind(quote):
    if _LEGAL.search(quote):
        return []
    kinds = []
    if (_DELIVERY.search(quote)
            and not re.fullmatch(r"бесплатн\w*\s+достав\w*\s*[*!.,]*", quote, re.I)
            and re.search(r"бесплат|стоим|осуществ|доставим|доставля|доставлен|возмож|оговарива|[0-9]|по\s+(?:России|город|г\.|област)|МКАД|курьер|транспортн", quote, re.I)):
        kinds.append("delivery")
    if _PAYMENT.search(quote) and re.search(r"осуществ|предоплат|постоплат|отсроч|безнал|сч[её]т|карт|оплачив|оплачива|наличн|payment\s+(?:by|in)|[0-9]", quote, re.I):
        kinds.append("payment")
    if _WARRANTY.search(quote) and re.search(r"срок|предостав|распростран|действ|месяц|\bмес\.?|\bлет\b|\bгод\w*|замен|возврат", quote, re.I):
        kinds.append("warranty")
    if re.search(r"(?:товар\w*|продукци\w*).{0,70}(?:\bнов(?:ый|ая|ое|ые)\b|не\s+бывш\w*\s+в\s+употреблении)|(?:не\s+бывш\w*\s+в\s+употреблении).{0,70}товар", quote, re.I):
        kinds.append("terms")
    return kinds


def seller_terms(document):
    """Return exact source clauses, without evaluating requested conditions."""
    if not isinstance(document, dict) or not _public_url(document.get("url")):
        return {}
    if document.get("method") == "search_api" or document.get("page_kind") in {"search", "blocked", "error"}:
        return {}
    text = _space(document.get("text"))
    if not text:
        return {}
    # An operator's terms cannot be assigned to an unidentified marketplace
    # merchant. Dedicated seller-block support can be added separately.
    if source_metadata(document.get("url"))["source_group"] == "platforms" or re.search(r"\bмаркетплейс\b|\bmarketplace\b", text, re.I):
        return {}
    nodes = _parsed(document)
    clauses = _clauses(document, nodes)
    result, taxes = {}, []
    previous = None
    for clause in clauses:
        if _ALL_PRICES.search(clause):
            value = _tax_value(clause)
            uncertain = re.search(r"\bне\s+(?:все|всегда|указан\w*)|\b(?:могут|может|возможно|если|кроме)\b|за\s+исключением|при\s+условии", clause, re.I)
            if value and not uncertain and not re.search(r"цен\w*\s+(?:на\s+)?(?:достав|сборк|установк|ремонт|услуг)", clause, re.I):
                taxes.append(_proof(document, clause, value=value, scope="all_prices"))
        for kind in _kind(clause):
            quote = clause
            # Keep an adjacent geographic/audience heading in the actual
            # quotation; a free local service must retain its stated scope.
            if (previous and len(previous) <= 180 and not re.search(r"[0-9₽.!?]", previous)
                    and re.search(r"^(?:для\s+(?:юридических|физических)\s+лиц|(?:бесплатная\s+)?доставка\b|условия\s+доставки\b)", previous, re.I)):
                combined = previous + " " + clause
                if len(combined) <= 1200 and combined in text:
                    quote = combined
            observation = _proof(document, quote, value=quote, status="unconfirmed", basis="seller_commercial_clause")
            if observation not in result.setdefault(kind, []):
                result[kind].append(observation)
        previous = clause
    # A local VAT token is usable only inside an independently bounded primary
    # purchase-price block. Unrelated footer tokens never enter this branch.
    for context in primary_price_contexts(document):
        quote = context.get("tax_quote")
        value = _tax_value(quote or "")
        if value and quote in text:
            taxes.append(_proof(document, quote, value=value, scope="primary_price", context=context.get("context")))
    if taxes and len({tax["value"] for tax in taxes}) == 1:
        result["tax"] = taxes[0]
    elif taxes:
        result["tax_conflicts"] = taxes
    return result


def enrich_offer(offer, request, primary, related_documents=()):
    """Attach observed facts and applicable VAT; never accept a TЗ condition.

    The request is intentionally unused: requested values cannot supply source
    facts. Related captures require their own immutable provenance and a direct
    visible link in the primary capture. Inputs are never mutated.
    """
    result = deepcopy(offer)
    if not isinstance(primary, dict):
        return result
    linked = {item["url"] for item in seller_terms_links(primary)}
    documents = [primary]
    for document in related_documents or ():
        if not isinstance(document, dict):
            continue
        url = _public_url(document.get("url"))
        if (url in linked and _host(url) == _host(str(primary.get("url") or ""))
                and (document.get("document_id") or document.get("id"))
                and document.get("sha256") and document.get("retrieved_at")):
            documents.append(document)
    observed, taxes = {}, []
    for document in documents:
        terms = seller_terms(document)
        if terms.get("tax") and (document is primary or terms["tax"].get("scope") == "all_prices"):
            taxes.append(terms["tax"])
        taxes.extend(terms.get("tax_conflicts", []))
        for kind in ("delivery", "payment", "warranty", "terms"):
            for observation in terms.get(kind, []):
                if observation not in observed.setdefault(kind, []):
                    observed[kind].append(observation)
    if observed:
        result["observed_terms"] = observed
    else:
        result.pop("observed_terms", None)
    values = {proof["value"] for proof in taxes}
    existing_tax = result.get("tax")
    if len(values) == 1 and (existing_tax in {None, "unknown"} or existing_tax in values):
        proof = next(proof for proof in taxes if proof["value"] in values)
        result["tax"] = proof["value"]
        result.setdefault("evidence", {})["tax"] = {key: value for key, value in proof.items() if key != "value"}
    elif taxes and (len(values) > 1 or existing_tax not in {None, "unknown", *values}):
        result["observed_terms"] = observed | {"tax_conflicts": taxes}
        result["tax"] = "unknown"
        result.setdefault("evidence", {}).pop("tax", None)
    return result

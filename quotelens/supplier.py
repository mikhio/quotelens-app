"""Visible supplier contacts from one immutable capture; no registry lookup.

Contact/footer text can describe the site operator, while a marketplace footer
usually describes the platform. Marketplace identities therefore require an
explicit visible seller block. Schema metadata and model-supplied identities
are deliberately not used here.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from .acquisition import AcquisitionError, _normalize_url
from .source_policy import source_metadata


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


_CONTACT = re.compile(r"contact|requisit|rekviz|legal|company[-_]?details|supplier[-_]?details|контакт|реквизит", re.I)
_SELLER = re.compile(r"(?:seller|vendor|merchant|supplier)[-_](?:info|details|contact|requisit)|продавец|поставщик", re.I)
_SKIP = {"script", "style", "noscript", "template", "svg"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Contacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocks, self.skip = [], [], 0
        self.contact_markup, self.nodes = False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
        marker = " ".join(str(attrs.get(key) or "") for key in ("class", "id", "itemprop"))
        role = "seller" if _SELLER.search(marker) or attrs.get("itemprop") == "seller" else "contact" if tag in {"footer", "address"} or _CONTACT.search(marker) else None
        self.contact_markup |= bool(role)
        # Nested footer/contact wrappers often describe the same text. Keep one
        # outer collector per role so every character is copied at most once to
        # contacts and once to a dedicated seller, rather than once per ancestor.
        # Distinct sibling blocks and seller-vs-platform separation remain intact.
        if role and any(frame[1] == role for frame in self.stack):
            role = None
        self.nodes += 1
        if self.nodes > 30000 or len(self.stack) > 100:
            raise ValueError("Supplier DOM exceeds extraction limits")
        hidden = bool(self.stack and self.stack[-1][3]) or "hidden" in attrs or attrs.get("aria-hidden") == "true" or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I)) or bool(set(str(attrs.get("class") or "").split()) & {"hide", "hidden", "d-none"})
        if tag not in _VOID:
            self.stack.append((tag, role, [], hidden))

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for _, role, parts, hidden in self.stack[index:]:
                    if role and parts and not hidden:
                        self.blocks.append((role, _space(" ".join(parts))))
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.skip and not (self.stack and self.stack[-1][3]):
            for _, role, parts, hidden in self.stack:
                if role and not hidden:
                    parts.append(data)

    def finish(self):
        for _, role, parts, hidden in self.stack:
            if role and parts and not hidden:
                self.blocks.append((role, _space(" ".join(parts))))
        return self.blocks


_INN = re.compile(r"(?<!\w)(?:ИНН|tax\s*id)\s*[:№=—-]?\s*(\d(?:[\d \u00a0\u202f-]{0,24}\d)?)(?!\d)", re.I)
_LEGAL_LABEL = r"(?:ИНН|КПП|ОГРН|адрес|телефон|тел\.?|e-?mail|почта|сайт|контакты)\b"
_LEGAL = re.compile(
    r"(?<!\w)(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|LLC|LTD)\s+(?:"
    # The closing quote completes the name on its own. Copyright years and
    # disclaimers that follow it must never make us retry the unquoted branch.
    r'«[^«»\n]{2,140}»|„[^„“”\n]{2,140}[“”]|“[^“”\n]{2,140}”|"[^"\n]{2,140}"'
    r"|(?![«„\"“])[^;.,\n«»„“”\"()©!?]{2,140}?"
    r"(?=\s*\(?\s*" + _LEGAL_LABEL +
    r"|\s+(?:19|20)\d{2}(?:\s*[-–—]\s*(?:19|20)\d{2})?\s*(?:©|copyright\b)"
    r"|\s*©|[;.,]|$))", re.I)
_LEGAL_PROSE = re.compile(
    r"\b(?:внимание|сведения|справочн\w*|информаци\w*|предлагает|оказывает|осуществляет|"
    r"производит|поставляет|занимается|прода[её]т|является|являются|представляет|"
    r"публичн\w*|оферт\w*|copyright|все\s+права|all\s+rights|политик\w*|"
    r"пользовательск\w*|защищены|надежн\w*\s+партнер\w*|надёжн\w*\s+партнёр\w*)\b|[@/:]", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}(?![\w-])", re.I)
_PHONE = re.compile(r"(?:телефон|тел\.?|phone|telephone)\s*[:=—-]?\s*(\+?\d[\d ()\u00a0-]{7,24}\d)(?!\d)", re.I)
_WEBSITE = re.compile(r"(?:сайт|website|web\s*site)\s*[:=—-]?\s*(https://[^\s<>\"»]+)", re.I)


def _legal_observations(context):
    for match in _LEGAL.finditer(context):
        quote = match.group().strip()
        quoted = bool(re.search(r'\s[«„“"]', quote))
        # An unquoted sentence may begin with an organisation's name, but its
        # remaining prose does not establish the name's exact boundary.
        if not quoted and _LEGAL_PROSE.search(quote):
            continue
        yield quote, quote


def supplier_details(document):
    """Recompute identity and evidence exclusively from the captured document.

    ``format_valid`` means ten/twelve decimal digits, not a checksum, a legal
    status, ownership of the page, or a completed official-registry check.
    Missing and conflicting values are never filled from a proposed offer.
    """
    document = document if isinstance(document, dict) else {}
    text = _space(document.get("text"))
    url = str(document.get("url") or "")
    evidence = {}
    result = {key: None for key in ("legal_name", "inn", "email", "phone", "website")}
    result.update(verification={"status": "not_checked", "registry_url": None,
                                "reason": "Сведения из источника; проверка по государственному реестру не выполнялась."}, evidence=evidence)
    if not text:
        return result
    base = {"url": url, "document_id": document.get("document_id") or document.get("id"),
            "source_hash": document.get("sha256") or hashlib.sha256(str(document.get("text") or "").encode()).hexdigest(),
            "retrieved_at": document.get("retrieved_at")}
    parser = _Contacts()
    try:
        parser.feed(str(document.get("html") or ""))
        blocks = [(role, value) for role, value in parser.finish() if value and value in text]
    except (ValueError, RecursionError):
        blocks = []
    sellers = [value for role, value in blocks if role == "seller"]
    platform = source_metadata(url)["source_group"] == "platforms" or bool(re.search(r"маркетплейс|marketplace", text, re.I))
    if platform and not sellers:
        result["verification"].update(status="conflict", reason="Реквизиты платформы не подтверждают личность продавца; отдельный блок продавца не найден.")
        return result
    contacts = sellers or [value for role, value in blocks if role == "contact"]
    if not contacts and not parser.contact_markup:
        # Unstructured text needs an explicit seller/contact label. A mere
        # mention of a manufacturer in product specifications is insufficient.
        labels = list(re.finditer(r"(?:продавец|поставщик|реквизиты|юридическое\s+лицо|контакты|seller)\s*[:=—-]?", text, re.I))
        contacts = [text[match.start():match.start() + 600] for match in labels]
        if not contacts:
            contacts = [text[match.start():match.start() + 600] for match in _LEGAL.finditer(text)
                        if _INN.search(text[match.end():match.end() + 250])
                        and not re.search(r"производител|изготовител|manufacturer", text[max(0, match.start() - 60):match.start()], re.I)]
    if not contacts:
        return result

    def publish(field, observations):
        observations = list(dict.fromkeys(observations))
        values = {value.casefold() for value, _ in observations}
        if len(values) == 1:
            result[field], quote = observations[0]
            evidence[field] = {"quote": quote, **base}
        elif len(values) > 1:
            evidence[field] = {"quote": observations[0][1], **base,
                               "conflicts": [{"value": value, "quote": quote} for value, quote in observations]}
        return len(values)

    # A dedicated seller block establishes whose details these are. Otherwise
    # all INNs on the page must agree, so an operator/footer cannot hide another
    # visible organisation with different requisites.
    inn_contexts = sellers or [text]
    inns = [(re.sub(r"[\s-]", "", match[1]), match.group())
            for context in inn_contexts for match in _INN.finditer(context)]
    inn_count = publish("inn", inns)
    if inn_count > 1:
        result["verification"].update(status="conflict", reason="В источнике несколько разных ИНН; однозначная организация не установлена.")
        return result
    if result["inn"]:
        valid = bool(re.fullmatch(r"\d{10}|\d{12}", result["inn"]))
        result["verification"].update(status="format_valid" if valid else "invalid",
            registry_url="https://egrul.nalog.ru/" if valid else None,
            reason="Проверен только формат ИНН (10 или 12 цифр); проверка по реестру не выполнялась." if valid else "ИНН в источнике не соответствует формату 10 или 12 цифр; проверка по реестру не выполнялась.")
    legal_count = publish("legal_name", [fact for context in contacts for fact in _legal_observations(context)])
    if legal_count > 1:
        result["verification"].update(status="conflict", reason="В контактных блоках несколько юридических наименований; организация не установлена однозначно.")
        result["inn"] = None
        return result
    publish("email", [(match.group(), match.group()) for context in contacts for match in _EMAIL.finditer(context)])
    phones = [(match[1].strip(), match.group()) for context in contacts for match in _PHONE.finditer(context)
              if 10 <= len(re.sub(r"\D", "", match[1])) <= 15]
    publish("phone", phones)
    websites = []
    for context in contacts:
        for match in _WEBSITE.finditer(context):
            raw = match[1].rstrip(".,;)")
            try:
                normalized = _normalize_url(raw)
            except AcquisitionError:
                continue
            if urlsplit(normalized).hostname:
                websites.append((raw, match.group()))
    publish("website", websites)
    return result

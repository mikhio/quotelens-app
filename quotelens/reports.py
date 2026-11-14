"""Self-contained recipient reports from the saved, current calculation.

HTML, PDF and editable DOCX share one document model. No report fetches a site,
executes supplier HTML, reserves research quota or uses a language model.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


_PDF_SLOT = threading.BoundedSemaphore(1)
_STATUS = {"included": "В расчёте", "excluded": "Исключено", "uncertain": "Требует проверки"}
_UNIT = {"piece": "шт.", "шт": "шт.", "pack": "пачка", "box": "коробка", "sheet": "лист", "m2": "м²", "hour": "час", "service": "услуга"}
_OP = {"eq": "=", "=": "=", "gte": "≥", ">=": "≥", "lte": "≤", "<=": "≤", "gt": ">", "lt": "<", "contains": "содержит", "in": "одно из", "between": "в диапазоне"}
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML = "http://www.w3.org/XML/1998/namespace"
ET.register_namespace("w", _W)
ET.register_namespace("r", _R)


def _text(value):
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return "; ".join(_text(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _text(value["text"])
        return "; ".join(f"{key}: {_text(item)}" for key, item in value.items() if item is not None)
    return str(value)


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value, currency="RUB"):
    amount = _decimal(value)
    if amount is None:
        return "Не рассчитано"
    symbol = "₽" if currency == "RUB" else _text(currency)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " " + symbol


def _unit_amount_display(amount):
    """Show meaningful fractions; a positive unit price must not become zero."""
    fractional_places = max(0, -amount.as_tuple().exponent)
    places = max(2, min(12, fractional_places))
    if amount and abs(amount) < Decimal("1e-12"):
        places = max(places, 5 - amount.adjusted())
    rendered = f"{amount:,.{places}f}"
    if places > 2:
        whole, fraction = rendered.rsplit(".", 1)
        rendered = whole + "." + fraction.rstrip("0").ljust(2, "0")
    return rendered


def _unit_money(value, currency="RUB"):
    amount = _decimal(value)
    if amount is None:
        return "Не рассчитано"
    symbol = "₽" if currency == "RUB" else _text(currency)
    return _unit_amount_display(amount).replace(",", " ").replace(".", ",") + " " + symbol


def _unit_display_is_rounded(value):
    amount = _decimal(value)
    return amount is not None and Decimal(_unit_amount_display(amount).replace(",", "")) != amount


def _number(value):
    amount = _decimal(value)
    if amount is None:
        return "—"
    return format(amount.normalize(), "f").replace(".", ",")


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return value
    except ValueError:
        return None


def _unique(values):
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def _section(title, paragraphs=None, table=None, level=2):
    return {"title": title, "paragraphs": paragraphs or [], "table": table, "level": level}


def _rows_for_conditions(conditions):
    rows = []
    if isinstance(conditions, str):
        conditions = [conditions]
    for condition in conditions or []:
        if isinstance(condition, dict):
            label = condition.get("label") or condition.get("key") or "Условие"
            value = condition.get("text") or condition.get("quote") or condition.get("value")
            rows.append([_text(label), _text(value)])
        else:
            rows.append(["Условие", _text(condition)])
    return rows


def _items(result):
    items = result.get("items")
    if isinstance(items, list) and items:
        found = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Accept a legacy nested producer too, without losing item metadata.
            combined = {**item, **item.get("result", {})} if isinstance(item.get("result"), dict) else item
            found.append(combined)
        return found
    return [result]


def _calculation_content(calculation, offers, quantity):
    calculation = calculation or {}
    provisional = calculation.get("provisional") or {}
    ready = calculation.get("status") == "ready" and calculation.get("total") is not None
    available = provisional.get("total") is not None
    label = "Стоимость по выбранному профилю" if ready else "Предварительная стоимость" if available else "Итоговая стоимость не определена"
    total = calculation.get("total") if ready else provisional.get("total")
    paragraphs = [label + (": " + _money(total) if total is not None else "."), ("Строгий профиль: " if not ready else "") + _text(calculation.get("reason"))]
    identifiers = None if ready else set(provisional.get("offer_ids") or [])
    selected = [offer for offer in offers if offer.get("status") == "included"] if ready else [offer for offer in offers if offer.get("id") in identifiers]
    prices = []
    for offer in selected:
        price = offer.get("normalized_price") if ready else (provisional.get("normalized_prices") or {}).get(offer.get("id"), offer.get("normalized_price"))
        if price is None and offer.get("unit"):
            price = offer.get("price")
        if _decimal(price) is not None:
            prices.append(price)
    rounded_operands = any(_unit_display_is_rounded(price) for price in prices)
    if prices and _decimal(quantity) is not None:
        formula = "(" + " + ".join(_unit_money(price) for price in prices) + ") / " + str(len(prices)) + " × " + _number(quantity)
        paragraphs.append("Расчёт: " + formula + (" ≈ " if rounded_operands else " = ") + _money(total))
    stats = calculation if ready else provisional
    if stats.get("n"):
        paragraphs.append(("Предложений в сопоставимой выборке" if ready else "Предложений в предварительной выборке") + ": " + _number(stats["n"]) + ".")
        if stats.get("mean") is not None:
            paragraphs.append("Среднее: " + _unit_money(stats["mean"]) + "; выборочное стандартное отклонение: " + (_unit_money(stats["stddev"]) if stats.get("stddev") is not None else "не определено") + "; разброс цен (коэффициент вариации, CV): " + (_number(stats["cv"]) + "%" if stats.get("cv") is not None else "не определён") + ".")
    rounded_statistics = any(_unit_display_is_rounded(stats.get(key)) for key in ("mean", "stddev"))
    if rounded_operands or rounded_statistics:
        paragraphs.append("Промежуточные цены и показатели показаны с ограниченной точностью. Расчёт выполнен без промежуточного округления; итоговая сумма округлена до копеек.")
    elif total is not None:
        paragraphs.append("Расчёт выполнен без промежуточного округления; итоговая сумма округлена до копеек.")
    if not ready:
        if provisional.get("reason"):
            paragraphs.append(_text(provisional["reason"]))
        paragraphs.extend(_unique(provisional.get("warnings") or []))
        if provisional.get("known_subtotal") is not None and not available:
            paragraphs.append("Подытог только по позициям с рассчитанной стоимостью: " + _money(provisional["known_subtotal"]) + ". Это не сумма всей закупки.")
        if provisional.get("missing_item_ids"):
            paragraphs.append("Не рассчитаны позиции: " + ", ".join(map(str, provisional["missing_item_ids"])) + ".")
    return label, total, paragraphs


def document_model(result):
    """Build plain, escaped-at-render content without touching the input result."""
    if not isinstance(result, dict):
        raise ValueError("Нужен сохранённый результат расчёта.")
    result = deepcopy(result)
    request, calc = result.get("request") or {}, result.get("calculation") or {}
    search = result.get("search") or {}
    revision = result.get("review", {}).get("revision", result.get("revision"))
    title = request.get("title") or request.get("product") or str(request.get("description") or "Закупка").splitlines()[0]
    title = str(title).strip()[:250] or "Закупка"
    items = _items(result)
    procurement = bool(result.get("items"))
    if procurement:
        # The aggregate contains prices for different goods. Only line totals
        # may be added here; means and quantities belong to each line below.
        display_calc = deepcopy(calc)
        names = {str(item.get("id")): _text(item.get("title") or item.get("request", {}).get("product") or "Позиция без названия") for item in items}
        provisional = display_calc.get("provisional") or {}
        if provisional.get("missing_item_ids"):
            provisional["missing_item_ids"] = [names.get(str(i), "Позиция без названия") for i in provisional["missing_item_ids"]]
        _, total, summary = _calculation_content(display_calc, [], None)
        line_totals = []
        ready = calc.get("status") == "ready" and calc.get("total") is not None
        for item in items:
            line = item.get("calculation") or {}
            value = line.get("total")
            if value is None and not ready:
                value = (line.get("provisional") or {}).get("total")
            if _decimal(value) is not None:
                line_totals.append(value)
        known = total if total is not None else provisional.get("known_subtotal")
        if line_totals and known is not None:
            formula = " + ".join(_money(value) for value in line_totals) + " = " + _money(known)
            summary.insert(2, ("Сумма стоимостей позиций: " if total is not None else "Расчёт известной части: ") + formula)
    else:
        _, total, summary = _calculation_content(calc, result.get("offers") or [], request.get("quantity"))
    sections = [_section("Результат", summary)]
    if search.get("scenario_kind") == "synthetic" or any(o.get("provenance") == "synthetic" for o in result.get("offers") or []):
        sections.insert(0, _section("Происхождение данных", ["Учебные данные: цены и поставщики в отмеченных предложениях вымышлены. Они не подтверждают рыночную стоимость."]))
    meta = []
    if search.get("started_at"):
        meta.append("Исследование начато: " + _text(search["started_at"]))
    if search.get("finished_at"):
        meta.append("Срез результата: " + _text(search["finished_at"]))
    if revision is not None:
        meta.append("Ревизия результата: " + _text(revision))
    if calc.get("rule"):
        meta.append("Профиль расчёта: " + _text(calc["rule"]))
    if meta:
        sections.append(_section("Основание расчёта", meta))
    if len(items) > 1:
        rows = []
        for index, item in enumerate(items, 1):
            req = item.get("request") or item
            item_calc = item.get("calculation") or {}
            ready = item_calc.get("status") == "ready" and item_calc.get("total") is not None
            estimate = item_calc.get("provisional") or {}
            rows.append([str(index), _text(item.get("title") or req.get("product") or req.get("description")),
                         _number(item.get("quantity", req.get("quantity"))) + " " + _text(item.get("unit", req.get("unit"))),
                         _money(item_calc.get("total") if ready else estimate.get("total")),
                         "По профилю" if ready else "Предварительно" if estimate.get("total") is not None else "Не рассчитано"])
        sections.append(_section("Состав закупки", table={"headers": ["№", "Позиция", "Количество", "Стоимость", "Статус"], "rows": rows}))
    for item_index, item in enumerate(items, 1):
        req, item_calc = item.get("request") or {}, item.get("calculation") or {}
        offers = item.get("offers") or []
        item_title = item.get("title") or req.get("product") or req.get("description") or title
        if len(items) > 1:
            sections.append(_section(f"Позиция {item_index}. {item_title}", level=2))
        description = req.get("description")
        input_paragraphs = ["Количество: " + _number(item.get("quantity", req.get("quantity"))) + " " + _UNIT.get(str(item.get("unit", req.get("unit"))), _text(item.get("unit", req.get("unit"))))]
        if description:
            input_paragraphs.append(str(description))
        sections.append(_section("Потребность", input_paragraphs, level=3 if len(items) > 1 else 2))
        requirements = []
        for requirement in req.get("requirements") or []:
            if not isinstance(requirement, dict):
                continue
            value = requirement.get("value")
            if requirement.get("op") == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
                value = f"{_number(value[0])} … {_number(value[1])}"
            label = _text(requirement.get("label") or requirement.get("key"))
            if requirement.get("manual_override"):
                label += " (изменено пользователем)"
            requirements.append([label,
                                 _OP.get(requirement.get("op", "eq"), _text(requirement.get("op"))) + " " + _text(value),
                                 _text(requirement.get("unit"))])
        if requirements:
            sections.append(_section("Требования к сопоставимости", table={"headers": ["Характеристика", "Требуется", "Единица"], "rows": requirements}, level=3))
        overrides = [r for r in req.get("requirements", []) if isinstance(r, dict) and r.get("manual_override")]
        if overrides:
            paragraphs = ["Отмеченные требования изменены пользователем после разбора ТЗ. Правки показаны отдельно от первоначального источника."]
            for requirement in overrides:
                source = requirement.get("source") if isinstance(requirement.get("source"), dict) else {}
                label = _text(requirement.get("label") or requirement.get("key"))
                if source.get("kind") == "user_edit":
                    if requirement.get("quote"):
                        paragraphs.append("Текст правки («" + label + "»): " + _text(requirement["quote"]))
                    if source.get("document_name") or source.get("document_id"):
                        paragraphs.append("Источник правки: " + _text(source.get("document_name") or source.get("document_id")))
                    if requirement.get("original_quote"):
                        paragraphs.append("Исходная цитата ТЗ («" + label + "»): " + _text(requirement["original_quote"]))
                    original = source.get("original") if isinstance(source.get("original"), dict) else {}
                    if original.get("document_name") or original.get("document_id"):
                        paragraphs.append("Первоначальный источник: " + _text(original.get("document_name") or original.get("document_id")))
                elif requirement.get("quote"):
                    paragraphs.append("Исходный текст («" + _text(requirement.get("label") or requirement.get("key")) + "»): " + _text(requirement["quote"]))
            sections.append(_section("Правки требований", paragraphs, level=3))
        conditions = _rows_for_conditions(item.get("conditions") or req.get("conditions"))
        if conditions:
            sections.append(_section("Условия закупки", table={"headers": ["Условие", "Значение"], "rows": conditions}, level=3))
        if procurement:
            _, _, calculation_text = _calculation_content(item_calc, offers, item.get("quantity", req.get("quantity")))
            sections.append(_section("Расчёт позиции", calculation_text, level=3))
        group = item.get("target_group") or {}
        if group:
            paragraphs = ["Целевых предложений: " + _number(group.get("count")) + " из " + _number(group.get("required", 3)) + ".",
                "Разброс цен (коэффициент вариации, CV) = выборочное стандартное отклонение / среднее × 100: " + (_number(group["cv"]) + "%" if group.get("cv") is not None else "не определён") + "; порог профиля " + _number(group.get("cv_limit", 33)) + "%.",
                _text(group.get("reason") or "")]
            if group.get("manual_count"):
                paragraphs.append("С принятыми пользователем отклонениями: " + _number(group["manual_count"]) + ". Исходные значения сохранены.")
            sections.append(_section("Достаточность выборки", paragraphs, level=3))
        rows = []
        for index, offer in enumerate(offers, 1):
            source = _text(offer.get("supplier")) + "\n" + _text(offer.get("retrieved_at"))
            url = _safe_url(offer.get("url"))
            if url:
                source += "\n" + url
            price = _unit_money(offer.get("price"), offer.get("currency") or "валюта не подтверждена")
            if offer.get("unit"):
                price += " / " + _UNIT.get(str(offer["unit"]), str(offer["unit"]))
            else:
                price += "; единица цены не подтверждена"
                assumption = offer.get("provisional_unit")
                if isinstance(assumption, dict) and assumption.get("assumption"):
                    price += "\nУсловная оценка: " + str(assumption["assumption"])
            tax = {"included": "НДС включён", "excluded": "Без НДС", "unknown": "НДС не указан"}.get(offer.get("tax"), "НДС не указан")
            price += "\n" + tax
            reasons = _unique(offer.get("reasons") or [])
            decision = _STATUS.get(offer.get("status"), "Требует проверки")
            if offer.get("manual_confirmations"):
                decision += "; есть ручные подтверждения"
            if offer.get("manual_acceptances"):
                decision += "; отклонения приняты пользователем"
            if offer.get("duplicate_of"):
                decision += "; дубль " + str(offer["duplicate_of"])
            decision += "\n" + "\n".join(reasons)
            normalized = offer.get("normalized_price")
            normalized_text = _unit_money(normalized)
            if normalized is None:
                conditional_price = (item_calc.get("provisional", {}).get("normalized_prices") or {}).get(offer.get("id"))
                if conditional_price is not None:
                    normalized_text = _unit_money(conditional_price) + " (условно)"
            rows.append([str(index) + ". " + _text(offer.get("title")), source, price,
                         normalized_text, decision])
        if rows:
            sections.append(_section("Предложения и решения", table={"headers": ["Предложение", "Источник / дата", "Цена для расчёта", "Цена в единице закупки", "Решение и основание"], "rows": rows}, level=3))
        else:
            sections.append(_section("Предложения", ["Подтверждённые предложения для этой позиции не получены."], level=3))
        for index, offer in enumerate(offers, 1):
            supplier = offer.get("supplier_details") or {}
            attrs = offer.get("attributes") or {}
            source_url = _safe_url(offer.get("url"))
            rows = [["Карточка предложения", source_url or "Ссылка не указана"],
                    ["Поставщик / источник", _text(offer.get("supplier") or offer.get("domain") or "Не указан")],
                    ["Производитель", _text(attrs.get("manufacturer") or attrs.get("producer") or "Нет подтверждённых сведений")]]
            if attrs.get("brand"):
                rows.append(["Бренд", _text(attrs["brand"])])
            for key, label in (("legal_name", "Юридическое наименование"), ("inn", "ИНН"),
                               ("email", "Email"), ("phone", "Телефон"), ("website", "Сайт поставщика")):
                value = supplier.get(key)
                if key == "website":
                    value = _safe_url(value)
                rows.append([label, _text(value) if value else "Нет подтверждённых сведений"])
            paragraphs = ["Реквизиты и контакты показаны при наличии подтверждения в сохранённых данных. Проверка организации по государственному реестру не выполнялась."]
            for proof in (supplier.get("evidence") or {}).values():
                if isinstance(proof, dict) and _safe_url(proof.get("url")):
                    paragraphs.append("Источник реквизитов:\n" + proof["url"])
                    break
            registry = _safe_url((supplier.get("verification") or {}).get("registry_url"))
            if registry:
                paragraphs.append("Реестр ФНС:\n" + registry)
            sections.append(_section(f"Поставщик и товар {item_index}.{index}", paragraphs,
                {"headers": ["Сведения", "Значение / ссылка"], "rows": rows}, level=3))
            if offer.get("manual_acceptances"):
                rows = [[field, _text(value.get("reason")), _text(value.get("at")), _text(value.get("revision"))] for field, value in offer["manual_acceptances"].items()]
                sections.append(_section(f"Принятые отклонения {item_index}.{index}", ["Пользователь принял указанные отклонения. Исходный факт не изменён и не считается подтверждением соответствия источником."],
                    {"headers": ["Требование", "Основание решения", "Дата", "Ревизия"], "rows": rows}, level=3))
            confirmations = offer.get("manual_confirmations") or {}
            if confirmations:
                manual_rows = []
                for field, confirmation in confirmations.items():
                    if not isinstance(confirmation, dict):
                        continue
                    proof = confirmation.get("supporting_source") or {}
                    basis = _text(confirmation.get("reason"))
                    if confirmation.get("origin") == "accepted_suggestion":
                        basis = "Показанное значение принято пользователем."
                        if proof.get("inferred"):
                            basis += " Предположение по белизне CIE; отдельного указания цвета нет."
                        if proof.get("quote"):
                            basis += "\nЦитата: " + _text(proof["quote"])
                        if _safe_url(proof.get("url")):
                            basis += "\nИсточник: " + proof["url"]
                        if proof.get("source_hash"):
                            basis += "\nSHA-256: " + _text(proof["source_hash"])
                    manual_rows.append([_text(field), _text(confirmation.get("original_value")), _text(confirmation.get("value")),
                                basis, _text(confirmation.get("at")) + "; ревизия " + _text(confirmation.get("revision"))])
                sections.append(_section(f"Ручные подтверждения {item_index}.{index}: {offer.get('title') or offer.get('id')}",
                    ["Значения введены или приняты пользователем. Решения пользователя не являются цитатами сайта; их основания показаны отдельно от исходных фактов. Исходные сведения и документы сохранены. Ручная проверка не подтверждает юридическую обоснованность НМЦК."],
                    {"headers": ["Поле", "Исходное значение", "Для расчёта", "Причина", "Проверка"], "rows": manual_rows}, level=3))
            proofs = []
            for field, proof in (offer.get("evidence") or {}).items():
                if not isinstance(proof, dict) or not proof.get("quote"):
                    continue
                value = offer.get("attributes", {}).get(field[11:]) if str(field).startswith("attributes.") else offer.get(field)
                if field in confirmations:
                    value = confirmations[field].get("original_value")
                quote = _text(proof["quote"])
                if proof.get("visibility") == "reachable_tab":
                    quote += "\nСохранённый HTML, вкладка «" + _text(proof.get("tab_label") or "Характеристики") + "»."
                if proof.get("url") and proof["url"] != offer.get("url"):
                    quote += "\nИсточник этого поля:\n" + _text(proof["url"]) + "\nПолучен: " + _text(proof.get("retrieved_at")) + "\nSHA-256: " + _text(proof.get("source_hash"))
                proofs.append([_text(field), _text(value), quote])
            if not proofs:
                continue
            proof_meta = []
            safe_url = _safe_url(offer.get("url"))
            if safe_url:
                proof_meta.append("Источник:\n" + safe_url)
            if offer.get("retrieved_at"):
                proof_meta.append("Получен: " + str(offer["retrieved_at"]))
            if offer.get("document_id"):
                proof_meta.append("Сохранённый документ: " + str(offer["document_id"]))
            if offer.get("source_hash"):
                proof_meta.append("SHA-256 источника: " + str(offer["source_hash"]))
            sections.append(_section(f"Подтверждения {item_index}.{index}: {offer.get('title') or offer.get('id')}", proof_meta,
                                     {"headers": ["Поле", "Найдено", "Точная цитата"], "rows": proofs}, level=3))
    stop = search.get("stop") or {}
    stop_paragraphs = []
    if stop.get("label"):
        stop_paragraphs.append("Причина остановки: " + _text(stop["label"]))
    if stop.get("detail"):
        stop_paragraphs.append(_text(stop["detail"]))
    policy = search.get("search_policy") or search.get("policy") or request.get("search_policy") or {}
    if policy.get("mode") == "offers":
        stop_paragraphs.append("Выбранное условие: до " + _text(policy.get("max_offers")) + " уникальных предложений.")
    elif policy.get("mode") == "time":
        stop_paragraphs.append("Выбранное время исследования: " + _number(policy.get("max_seconds")) + " с.")
    elif policy.get("mode") == "target":
        stop_paragraphs.append("Цель: три соответствующих сопоставимых предложения на позицию; время прохода до " + _number(policy.get("max_seconds")) + " с.")
    if stop_paragraphs:
        sections.append(_section("Завершение исследования", stop_paragraphs))
    audit = search.get("audit") or {}
    if audit.get("queries"):
        sections.append(_section("Выполненные поисковые запросы", audit["queries"]))
    if audit.get("checked_pages"):
        rows = [[_text(p.get("title") or p.get("url")), _text(p.get("url")), _text(p.get("retrieved_at")), _text(p.get("sha256"))] for p in audit["checked_pages"]]
        sections.append(_section("Проверенные страницы", ["Вывод относится к проверенным источникам; отсутствие товара на всём рынке не установлено."], {"headers": ["Страница", "Ссылка", "Получена", "SHA-256"], "rows": rows}))
    budget = search.get("budget") or {}
    metrics = []
    for key, label in (("pages_used", "Прочитано страниц"), ("queries_used", "Поисковых запросов"),
                       ("model_calls", "Обращений к модели"), ("elapsed_seconds", "Длительность, с")):
        if budget.get(key) is not None:
            metrics.append([label, _number(budget[key])])
    usage = budget.get("actual_usage") or {}
    known_usage = budget.get("actual_usage_known") is True or budget.get("model_calls") == 0 or (usage.get("total_tokens") or 0) > 0
    for key, label in (("prompt_tokens", "Измеренные входные токены"), ("completion_tokens", "Измеренные выходные токены"), ("total_tokens", "Измеренные токены, всего")):
        if known_usage and usage.get(key) is not None:
            metrics.append([label, _number(usage[key])])
    if usage.get("cost") is not None and (budget.get("actual_cost_known") is True or usage.get("cost_reported") is True or budget.get("model_calls") == 0 or (usage.get("cost") or 0) > 0):
        metrics.append(["Измеренный расход провайдера, USD", _number(usage["cost"])])
    elif budget.get("model_calls"):
        metrics.append(["Измеренный расход провайдера, USD", "Полные сведения не предоставлены"])
    if metrics:
        sections.append(_section("Объём и расход исследования", table={"headers": ["Показатель", "Значение"], "rows": metrics}))
    documents = request.get("input_documents") or result.get("input_documents") or []
    if documents:
        rows = [[_text(d.get("name")), _text(d.get("sha256"))] for d in documents if isinstance(d, dict)]
        sections.append(_section("Исходные файлы закупки", table={"headers": ["Файл", "SHA-256"], "rows": rows}))
    warnings = _unique((result.get("warnings") or []) + ([result["validation_notice"]] if result.get("validation_notice") else []))
    if warnings:
        sections.append(_section("Ограничения и сведения для проверки", warnings))
    return {"title": "Расчёт и обоснование цены закупки", "subject": title, "revision": revision, "sections": sections}


def report_html(result):
    model = document_model(result)
    parts = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>' + escape(model["title"]) + '</title><style>',
             'body{font:13px/1.5 Arial,sans-serif;color:#24211f;background:#fff;margin:0}main{max-width:1150px;margin:auto;padding:32px 24px}h1{font-family:Georgia,serif;color:#702c40;font-size:27px;line-height:1.2;margin:0 0 12px}h2{font-size:19px;margin:28px 0 10px}h3{font-size:15px;margin:20px 0 8px}p{white-space:pre-wrap;overflow-wrap:anywhere;margin:7px 0}table{border-collapse:collapse;width:100%;table-layout:fixed;margin:12px 0 20px;font-size:11px}th,td{text-align:left;vertical-align:top;border:1px solid #ddd7d0;padding:8px;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#f7f4ef;font-weight:700}thead{display:table-header-group}tr{break-inside:avoid}a{color:#702c40}h2,h3{break-after:avoid}.subject{font-size:18px;font-weight:600}.section{margin-bottom:16px}@page{size:A4;margin:15mm 12mm}@media print{main{padding:0}body{font-size:10pt}table{font-size:8pt}a{color:inherit;text-decoration:none}h1{font-size:20pt}h2{font-size:15pt}}',
             '</style></head><body><main><h1>' + escape(model["title"]) + '</h1><p class="subject">' + escape(model["subject"]) + '</p>']
    for section in model["sections"]:
        level = section["level"]
        parts.append(f'<section class="section"><h{level}>' + escape(section["title"]) + f'</h{level}>')
        for paragraph in section["paragraphs"]:
            parts.append('<p>' + _html_text(paragraph) + '</p>')
        table = section.get("table")
        if table:
            parts.append('<table><thead><tr>' + ''.join('<th>' + escape(_text(h)) + '</th>' for h in table["headers"]) + '</tr></thead><tbody>')
            for row in table["rows"]:
                parts.append('<tr>' + ''.join('<td>' + _html_text(cell) + '</td>' for cell in row) + '</tr>')
            parts.append('</tbody></table>')
        parts.append('</section>')
    parts.append('</main></body></html>')
    return ''.join(parts)


def _html_text(value):
    text = _text(value)
    # Links are added only for standalone, safe HTTPS URLs. Supplier data never
    # becomes markup, style, an image, or a script in the exported document.
    parts = []
    for line in text.splitlines() or [""]:
        url = _safe_url(line)
        parts.append('<a href="' + escape(url, quote=True) + '">' + escape(line) + '</a>' if url else escape(line))
    return '\n'.join(parts)


def report_pdf(result):
    """Render only the saved local document with all network requests denied."""
    if not _PDF_SLOT.acquire(timeout=30):
        raise RuntimeError("Формируется другой PDF-отчёт. Повторите скачивание.")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = os.getenv("BROWSER_EXECUTABLE_PATH")
            if not executable and Path('/usr/bin/google-chrome').is_file():
                executable = '/usr/bin/google-chrome'
            kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if executable:
                kwargs["executable_path"] = executable
            browser = playwright.chromium.launch(**kwargs)
            try:
                context = browser.new_context(java_script_enabled=False, offline=True, service_workers="block")
                context.route('**/*', lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(25000)
                page.set_content(report_html(result), wait_until="domcontentloaded", timeout=25000)
                return page.pdf(format='A4', print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    finally:
        _PDF_SLOT.release()


def _w(tag, parent=None, attrib=None, text=None):
    element = ET.Element('{'+_W+'}'+tag, attrib or {}) if parent is None else ET.SubElement(parent, '{'+_W+'}'+tag, attrib or {})
    if text is not None:
        element.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(text))
    return element


def _paragraph(parent, text, style=None, links=None):
    p = _w('p', parent)
    if style:
        props = _w('pPr', p)
        _w('pStyle', props, {'{'+_W+'}val': style})
    lines = _text(text).splitlines() or ['']
    for index, line in enumerate(lines):
        if index:
            _w('br', _w('r', p))
        target = _safe_url(line.strip()) if links is not None else None
        if target:
            identifier = links.setdefault(target, 'rIdLink' + str(len(links) + 1))
            hyperlink = _w('hyperlink', p, {'{'+_R+'}id': identifier})
            run = _w('r', hyperlink)
            props = _w('rPr', run)
            _w('color', props, {'{'+_W+'}val': '702C40'})
            _w('u', props, {'{'+_W+'}val': 'single'})
        else:
            run = _w('r', p)
        _w('t', run, {'{'+_XML+'}space': 'preserve'}, line)
    return p


def _table(parent, table, links=None):
    tbl = _w('tbl', parent)
    props = _w('tblPr', tbl)
    _w('tblW', props, {'{'+_W+'}w': '0', '{'+_W+'}type': 'auto'})
    borders = _w('tblBorders', props)
    for side in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
        _w(side, borders, {'{'+_W+'}val': 'single', '{'+_W+'}sz': '4', '{'+_W+'}color': 'DDD7D0'})
    grid = _w('tblGrid', tbl)
    columns = max(len(table["headers"]), 1)
    for _ in range(columns):
        _w('gridCol', grid, {'{'+_W+'}w': str(9600//columns)})
    for index, row in enumerate([table['headers']] + table['rows']):
        tr = _w('tr', tbl)
        trpr = _w('trPr', tr)
        if index == 0:
            _w('tblHeader', trpr)
        for cell in row:
            tc = _w('tc', tr)
            tcpr = _w('tcPr', tc)
            _w('tcW', tcpr, {'{'+_W+'}w': str(9600//columns), '{'+_W+'}type': 'dxa'})
            if index == 0:
                _w('shd', tcpr, {'{'+_W+'}fill': 'F7F4EF'})
            _paragraph(tc, cell, 'TableText', links)
    return tbl


def report_docx(result):
    """Return an editable OOXML document, without macros or external resources."""
    model = document_model(result)
    links = {}
    document = _w('document')
    body = _w('body', document)
    _paragraph(body, model['title'], 'Title')
    _paragraph(body, model['subject'], 'Subtitle')
    for section in model['sections']:
        _paragraph(body, section['title'], 'Heading'+str(section['level'] - 1))
        for paragraph in section['paragraphs']:
            _paragraph(body, paragraph, links=links)
        if section.get('table'):
            _table(body, section['table'], links)
    section_props = _w('sectPr', body)
    _w('pgSz', section_props, {'{'+_W+'}w': '11906', '{'+_W+'}h': '16838'})
    _w('pgMar', section_props, {'{'+_W+'}top': '850', '{'+_W+'}bottom': '850', '{'+_W+'}left': '680', '{'+_W+'}right': '680'})
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="21"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100"/></w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:rPr><w:sz w:val="27"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:keepNext/><w:spacing w:before="250"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr><w:keepNext/><w:spacing w:before="180"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:rPr><w:sz w:val="18"/></w:rPr></w:style></w:styles>'''
    types = '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    relationships_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    relationships = ET.Element('Relationships', {'xmlns': relationships_ns})
    ET.SubElement(relationships, 'Relationship', {'Id': 'rIdStyles', 'Type': _R + '/styles', 'Target': 'styles.xml'})
    for target, identifier in links.items():
        ET.SubElement(relationships, 'Relationship', {'Id': identifier, 'Type': _R + '/hyperlink',
            'Target': target, 'TargetMode': 'External'})
    doc_rels = ET.tostring(relationships, encoding='utf-8', xml_declaration=True)
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', types)
        archive.writestr('_rels/.rels', rels)
        archive.writestr('word/document.xml', ET.tostring(document, encoding='utf-8', xml_declaration=True))
        archive.writestr('word/styles.xml', styles)
        archive.writestr('word/_rels/document.xml.rels', doc_rels)
    return output.getvalue()

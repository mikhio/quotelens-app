"""Read procurement specifications without executing document content.

The parser keeps exact text and source coordinates.  Recognised tables become
editable items; a quantity or a condition it cannot establish stays unknown.
It does not fetch links, call a model, or interpret document instructions.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


MAX_FILES = 8
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_TOTAL_TEXT_CHARS = 400_000
MAX_ITEMS = 100
MAX_ITEM_CHARS = 25_000
MAX_ZIP_ENTRIES = 256
MAX_ZIP_UNCOMPRESSED = 24 * 1024 * 1024
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 60

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_UNIT = re.compile(r"^(?:шт\.?|штук[аи]?|пачк[аи]?|пачек|уп\.?|упак\.?|упаковк[аи]?|коробк[аи]?|компл\.?|комплект[а-я]*|м[²2]|кв\.?\s*м\.?|л|кг|г|услуг[аи]?|час[а-я]*)$", re.I)
_NUMBER = r"[+-]?\s*\d+(?:[.,]\d+)?"
_INDEX = re.compile(r"^\s*(\d{1,3})[.)]?\s*$")


def _space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_text(value):
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in value.splitlines()).strip()


def _quantity(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Количество позиции должно быть положительным числом.")
    try:
        number = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError("Количество позиции должно быть положительным числом.") from None
    if not number.is_finite() or not 0 < number <= 1_000_000:
        raise ValueError("Количество позиции должно быть больше нуля и не больше 1 000 000.")
    return int(number) if number == number.to_integral_value() else float(number)


def _unit(value):
    value = _space(value).rstrip(".")
    aliases = {"штука": "шт", "штуки": "шт", "штук": "шт", "пачки": "пачка", "пачек": "пачка",
               "уп": "упаковка", "упак": "упаковка", "упаковки": "упаковка", "коробки": "коробка",
               "компл": "комплект"}
    return aliases.get(value.lower(), value) or None


def _filename(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise ValueError("У файла должно быть короткое имя с расширением.")
    if value in {".", ".."} or any(ch in value for ch in ("/", "\\", "\x00")) or any(ord(ch) < 32 for ch in value):
        raise ValueError("Имя файла не должно содержать путь или управляющие символы.")
    return value.strip()


def _xml(raw):
    if len(raw) > MAX_XML_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I):
        raise ValueError("В документе недопустимая XML-структура.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError("Не удалось прочитать структуру DOCX.") from None
    if sum(1 for _ in root.iter()) > 250_000:
        raise ValueError("Структура DOCX слишком сложная.")
    return root


def _paragraph(element):
    pieces = []
    for node in element.iter():
        if node.tag == W + "t":
            pieces.append(node.text or "")
        elif node.tag == W + "tab":
            pieces.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _cell(element):
    return "\n".join(value for p in element.iter(W + "p") if (value := _paragraph(p)))


def _docx(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_ZIP_UNCOMPRESSED:
                raise ValueError("Распакованный DOCX превышает допустимый размер.")
            names = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (entry.filename in names or path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                        or "\x00" in entry.orig_filename or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                    raise ValueError("В DOCX недопустимые записи архива.")
                names.add(entry.filename)
                if entry.file_size > MAX_ZIP_UNCOMPRESSED or entry.file_size > max(1, entry.compress_size) * 300:
                    raise ValueError("В DOCX слишком сильно сжатые данные.")
                if "vbaproject" in entry.filename.lower() or entry.filename.lower().endswith(".bin"):
                    raise ValueError("DOCX с макросами или встроенными исполняемыми объектами не поддерживается.")
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise ValueError("Файл не является документом DOCX.")
            types = archive.read("[Content_Types].xml")
            _xml(types)
            if b"macroEnabled" in types:
                raise ValueError("Документы с макросами не поддерживаются.")
            root = _xml(archive.read("word/document.xml"))
            body = root.find(W + "body")
            if body is None:
                raise ValueError("В DOCX нет основного текста.")
            blocks, rendered, table_number = [], [], 0
            for element in body:
                if element.tag == W + "p":
                    value = _paragraph(element)
                    if value:
                        blocks.append({"kind": "paragraph", "text": value})
                        rendered.append(value)
                elif element.tag == W + "tbl":
                    table_number += 1
                    rows = [[_cell(c) for c in row.findall(W + "tc")] for row in element.findall(W + "tr")]
                    blocks.append({"kind": "table", "number": table_number, "rows": rows})
                    rendered.extend(" | ".join(row) for row in rows)
            # Headers, footnotes and comments are retained as source text only;
            # they must not create procurement items or alter table semantics.
            extras = []
            for name in sorted(names):
                if re.fullmatch(r"word/(?:header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name):
                    part = _xml(archive.read(name))
                    values = [_paragraph(p) for p in part.iter(W + "p")]
                    substantive = [v for v in values if v and not re.fullmatch(r"\d+", v)]
                    if substantive:
                        extras.append(name + ":\n" + "\n".join(substantive))
            text = _clean_text("\n".join(rendered + extras))
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError("Текст документа слишком большой; разделите ТЗ на файлы.")
            return text, blocks
    except (BadZipFile, RuntimeError, KeyError, EOFError, OSError, NotImplementedError):
        raise ValueError("Не удалось прочитать DOCX; проверьте файл и отсутствие пароля.") from None


def _pdf(raw):
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Файл с расширением PDF не является PDF-документом.")
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([sys.executable, "-m", "quotelens.spec_ingest", "--pdf-worker"],
                                input=raw, capture_output=True, timeout=12, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("Не удалось разобрать PDF за допустимое время; попробуйте DOCX или текстовый файл.") from None
    try:
        output = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Не удалось безопасно прочитать PDF; попробуйте DOCX или текстовый файл.") from None
    if result.returncode or output.get("error"):
        raise ValueError(output.get("error") or "Не удалось прочитать PDF.")
    text = _clean_text(output["text"])
    if not text:
        raise ValueError("В PDF не найден текст. Для скана нужен распознанный DOCX или текстовый файл.")
    return text, [{"kind": "paragraph", "text": line} for line in text.splitlines() if line.strip()]


def _pdf_worker():
    # Resource limits belong to this child only, not to the application server.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    try:
        from pypdf import PdfReader
        import pypdf.filters
        pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = 16 * 1024 * 1024
        raw = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("PDF превышает допустимый размер.")
        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("PDF с паролем не поддерживается.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("В PDF слишком много страниц; разделите документ.")
        pages, length = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            length += len(text)
            if length > MAX_TEXT_CHARS:
                raise ValueError("Текст PDF слишком большой; разделите документ.")
            pages.append(text)
        print(json.dumps({"text": "\n\n".join(pages)}, ensure_ascii=False))
    except ImportError:
        print(json.dumps({"error": "Чтение PDF не установлено на сервере; используйте DOCX или TXT."}, ensure_ascii=False))
        return 1
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else "Не удалось прочитать PDF; файл повреждён или не поддерживается."
        print(json.dumps({"error": error[:250]}, ensure_ascii=False))
        return 1
    return 0


def _document(upload):
    if not isinstance(upload, dict):
        raise ValueError("Некорректное вложение ТЗ.")
    name = _filename(upload.get("name"))
    encoded = upload.get("data")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise ValueError("Файл превышает допустимый размер 5 МиБ.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Не удалось прочитать содержимое вложения.") from None
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("Файл пустой или превышает допустимый размер 5 МиБ.")
    extension = Path(name).suffix.lower()
    if extension == ".docx":
        text, blocks = _docx(raw)
    elif extension == ".pdf":
        text, blocks = _pdf(raw)
    elif extension in {".txt", ".md"}:
        text = None
        for encoding in ("utf-8-sig", "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp1251"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                pass
        if text is None or "\x00" in text or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise ValueError("Текстовый файл должен быть в UTF-8, UTF-16 или Windows-1251 и содержать обычный текст.")
        text = _clean_text(text)
        blocks = [{"kind": "paragraph", "text": line} for line in text.splitlines() if line.strip()]
    else:
        raise ValueError("Поддерживаются файлы DOCX, PDF с текстом, TXT и MD.")
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError("В файле нет текста или он слишком большой.")
    digest = hashlib.sha256(raw).hexdigest()
    return {"id": digest[:20], "name": name, "sha256": digest, "text": text,
            "size": len(raw), "format": extension.removeprefix(".")}, blocks


def _source(document, table=None, row=None, quote=None):
    result = {"document_id": document["id"], "document_name": document["name"]}
    if table is not None:
        result["table"] = table
    if row is not None:
        result["row"] = row
    if quote:
        result["quote"] = quote
    return result


def _condition_kind(text):
    lower = _space(text).lower()
    for pattern, kind in [(r"транспорт|поставк|доставк|разгруз|занос|перевоз", "delivery"),
                          (r"хранени|срок.*годност|годност", "storage"),
                          (r"гаранти|неисправност|дефект|замен|возврат", "warranty"),
                          (r"сертификат|документац|инструкц|сервисн", "documentation")]:
        if re.search(pattern, lower):
            return kind
    return None


def _condition(text, document, table=None, row=None, label=None):
    return {"key": _condition_kind(text) or "general", "label": label or "Условия закупки",
            "text": text.strip(), "quote": text.strip(), "source": _source(document, table, row, text.strip())}


def _header_indices(rows):
    for row in rows[:5]:
        normal = [_space(v).lower() for v in row]
        names = [i for i, v in enumerate(normal) if "наименован" in v]
        amounts = [i for i, v in enumerate(normal) if "количеств" in v]
        units = [i for i, v in enumerate(normal) if "единиц" in v and "измерен" in v]
        if names and amounts and units:
            return {"name": names[0], "quantity": amounts[0], "unit": units[0],
                    "position": next((i for i, v in enumerate(normal) if "номер" in v or "№" in v), 0),
                    "okpd2": next((i for i, v in enumerate(normal) if "окпд" in v), None)}
    return None


def _is_index_row(cells):
    values = [c for c in cells if _space(c)]
    return bool(values) and all(re.fullmatch(r"\d{1,2}", _space(c)) for c in values)


def _new_item(document, position, title, quantity, unit, source):
    return {"id": document["id"] + "-" + str(position), "title": _space(title), "description": title.strip(),
            "quantity": quantity, "unit": _unit(unit), "requirements": [], "conditions": [],
            "source": source, "warnings": [], "_position": str(position), "_parts": [title.strip()]}


def _numeric_value(raw):
    return float(Decimal(re.sub(r"\s+", "", raw).replace(",", ".")))


def _constraints(value):
    """Return only explicit scalar/bounded numbers, preserving open bounds."""
    value = _space(value).replace("−", "-")
    match = re.fullmatch(r"(" + _NUMBER + r")\s*\(\s*±\s*(" + _NUMBER + r")\s*%\s*\)", value)
    if match:
        center, percentage = _numeric_value(match[1]), _numeric_value(match[2])
        delta = abs(center) * percentage / 100
        return [("between", [center - delta, center + delta])]
    match = re.fullmatch(r"(" + _NUMBER + r")\s*±\s*(" + _NUMBER + r")\s*(%)?", value)
    if match:
        center, delta = _numeric_value(match[1]), _numeric_value(match[2])
        # A trailing percent is the value unit, not an implicit relative margin.
        return [("between", [center - delta, center + delta])]
    match = re.fullmatch(r"от\s*(" + _NUMBER + r")\s*до\s*(" + _NUMBER + r")", value, re.I)
    if match:
        return [("between", [_numeric_value(match[1]), _numeric_value(match[2])])]
    match = re.fullmatch(r"(" + _NUMBER + r")\s*[-–]\s*(" + _NUMBER + r")", value)
    if match:
        return [("between", [_numeric_value(match[1]), _numeric_value(match[2])])]
    matches = list(re.finditer(r"(не\s+менее|не\s+более|не\s+ниже|не\s+выше|более|менее|выше|ниже|>=|<=|≥|≤|>|<)\s*(" + _NUMBER + r")", value, re.I))
    if matches and len(matches) <= 2:
        residual = value
        for match in reversed(matches):
            residual = residual[:match.start()] + residual[match.end():]
        if not re.search(r"\d", residual):
            operators = {"не менее": "gte", "не ниже": "gte", "не более": "lte", "не выше": "lte",
                         "более": "gt", "выше": "gt", "менее": "lt", "ниже": "lt",
                         ">=": "gte", "≤": "lte", "<=": "lte", "≥": "gte", ">": "gt", "<": "lt"}
            return [(operators[_space(m[1]).lower()], _numeric_value(m[2])) for m in matches]
    if re.fullmatch(_NUMBER, value):
        return [("eq", _numeric_value(value))]
    return []


def _key(label):
    lower = _space(label).lower()
    known = [(r"формат.*(?:бумаг|лист)|^формат$", "format"),
             (r"плотност|масса\s+листа.*1\s*м", "density_gsm"),
             (r"количеств.*лист.*(?:пачк|упак)|листов\s+в\s+пачке", "sheets_per_pack"),
             (r"чистота", "purity_percent"), (r"эндотоксин", "endotoxin_eu_per_ug"),
             (r"ed\s*50", "ed50_ng_ml"), (r"общий\s+вес\s+1\s*шт", "mass_ug"),
             (r"молекулярн.*вес", "molecular_weight_kda"),
             (r"белизна.*cie", "whiteness_cie"), (r"толщин.*бумаг|^толщина$", "thickness_um"),
             (r"марка\s+бумаги", "paper_grade"), (r"^цвет$", "color"),
             (r"производител", "manufacturer"), (r"страна\s+происхожд", "country")]
    return next((key for pattern, key in known if re.search(pattern, lower)),
                "spec_" + hashlib.sha256(lower.encode()).hexdigest()[:12])


def _add_requirement(item, label, value, unit, quote, source):
    label, value, unit = _space(label), _space(value), _space(unit) or None
    if not label or not value or value.lower() in {"соответствие", "наличие"}:
        return
    key = _key(label)
    # A unit repeated in a prose value is separated before numeric parsing.
    numeric = value
    if not unit:
        match = re.search(r"\s*(мкг|мкм|кДа|кг|мм/с|мкг/мл|нг/мл|мг/мл|град(?:усов)?|Вт|Гц|мм|μm|В|%)\s*$", numeric, re.I)
        if match and re.search(r"\d", numeric[:match.start()]):
            unit, numeric = match[1], numeric[:match.start()].strip()
    if unit:
        numeric = re.sub(r"\s*" + re.escape(unit) + r"\s*$", "", numeric, flags=re.I).strip()
    constraints = _constraints(numeric)
    if not constraints:
        # The source's text is a constraint too, without inventing a number.
        constraints = [("eq", value)]
    for op, scalar in constraints:
        requirement = {"key": key, "label": label, "op": op, "value": scalar,
                       "unit": unit, "quote": quote, "source": source}
        if not any(all(old.get(k) == requirement.get(k) for k in ("key", "op", "value")) for old in item["requirements"]):
            item["requirements"].append(requirement)


def _add_spec_line(item, line, source):
    line = line.strip()
    if not line:
        return
    kind = _condition_kind(line)
    if kind and (kind in {"storage", "delivery"} or re.search(r"гаранти|сертификат|документац", line, re.I)):
        item["conditions"].append({"key": kind, "label": "Условия позиции", "text": line, "quote": line, "source": source})
        return
    if line not in item["_parts"]:
        item["_parts"].append(line)
    # Keep colon-containing specifications as explicit fields. The exact line
    # remains in description even when its units/ranges are not recognised.
    split = line.split(":", 1) if ":" in line else None
    if split is None:
        match = re.match(r"^(Формат(?:\s+(?:листов|бумаги))?|Плотность\s+листа\s+бумаги|Вид\s+бумаги|Белизна|Яркость\s+бумаги|Количество\s+листов\s+в\s+упаковке|Производитель)\s+(.+)$", line, re.I)
        if match:
            split = [match[1], match[2]]
    if split:
        label, value = split
        if not label.strip() or not value.strip():
            return
        if re.fullmatch(r"количество|потребность", label.strip(), re.I):
            return
        unit = None
        key = _key(label)
        patterns = {"density_gsm": r"\s*(г\s*/\s*(?:м[²2]|кв\.?\s*м))\s*$",
                    "sheets_per_pack": r"\s*(?:шт\.?|лист(?:ов|а)?)\s*$",
                    "thickness_um": r"\s*(мкм)\s*$", "whiteness_cie": r"\s*(%)\s*$"}
        if key in patterns:
            match = re.search(patterns[key], value, re.I)
            if match:
                unit = {"density_gsm": "г/м²", "sheets_per_pack": "листов", "thickness_um": "мкм", "whiteness_cie": "%"}[key]
                value = value[:match.start()].strip()
        if key == "sheets_per_pack" and unit is None:
            unit = "листов"
        if key == "format":
            value = re.sub(r"^[аa]\s*([0-6])$", lambda m: "A" + m[1], value.strip(), flags=re.I)
        _add_requirement(item, label, value, unit, line, source)


def _spec_lines(lines):
    """Join an explicit label with its following value without losing quotes."""
    pending = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if pending is not None:
            if ":" not in line:
                yield pending + "\n" + line
                pending = None
                continue
            yield pending
            pending = None
        if line.endswith(":"):
            pending = line
        else:
            yield line
    if pending is not None:
        yield pending


def _table_items(document, blocks):
    items, by_position, conditions, ambiguous_positions = [], {}, [], set()
    for block in blocks:
        if block["kind"] != "table":
            continue
        rows, number = block["rows"], block["number"]
        indices = _header_indices(rows)
        if not indices:
            continue
        for row_number, row in enumerate(rows, 1):
            if _is_index_row(row) or max(indices[k] for k in ("name", "quantity", "unit", "position")) >= len(row):
                continue
            position = _INDEX.fullmatch(row[indices["position"]])
            title = row[indices["name"]].strip()
            unit = row[indices["unit"]].strip()
            if not position or not title or len(unit) > 80:
                continue
            try:
                quantity = _quantity(row[indices["quantity"]])
            except ValueError:
                quantity = None
            pos = position[1]
            if pos in by_position:
                # Repeated headers/tables must not duplicate an existing item.
                if _space(by_position[pos]["title"]) == _space(title):
                    continue
                ambiguous_positions.add(pos)
                by_position[pos]["warnings"].append("В ТЗ повторяется номер позиции " + pos + "; проверьте связь товара с характеристиками.")
                pos = str(len(items) + 1)
            item = _new_item(document, pos, title, quantity, unit,
                             _source(document, number, row_number, " | ".join(row)))
            if indices["okpd2"] is not None and indices["okpd2"] < len(row):
                item["okpd2"] = _space(row[indices["okpd2"]])
            if quantity is None:
                item["warnings"].append("Количество не распознано: укажите его перед поиском.")
            if not unit:
                item["warnings"].append("Единица потребности не указана: уточните её перед поиском.")
            if position[1] in ambiguous_positions:
                item["warnings"].append("В ТЗ повторяется номер позиции " + position[1] + "; проверьте связь товара с характеристиками.")
            items.append(item)
            by_position[pos] = item
    if not items:
        return [], []
    for block in blocks:
        if block["kind"] != "table" or _header_indices(block["rows"]):
            continue
        rows, number = block["rows"], block["number"]
        header = " ".join(" ".join(row) for row in rows[:3]).lower()
        delivery_column = next((i for row in rows[:3] for i, cell in enumerate(row) if "срок поставки" in _space(cell).lower()), None)
        if delivery_column is not None:
            for row_number, row in enumerate(rows, 1):
                if delivery_column < len(row) and re.search(r"(?:в\s+течение|не\s+позднее|до\s+\d)", row[delivery_column], re.I):
                    conditions.append(_condition(row[delivery_column], document, number, row_number, "Срок поставки"))
            continue
        if any(len(row) >= 3 for row in rows) and ("характеристик" in header or "требования обязательные" in header):
            current = None
            for row_number, row in enumerate(rows, 1):
                if _is_index_row(row):
                    continue
                pos = _INDEX.fullmatch(row[0]) if row else None
                if pos and pos[1] not in ambiguous_positions and pos[1] in by_position and len(row) >= 3 and row[1].strip():
                    current = by_position[pos[1]]
                elif row and (row[0].strip() or len(row) > 1 and row[1].strip()):
                    # Header rows are not continuations of the prior item.
                    current = None
                if current is None:
                    continue
                quote = " | ".join(row)
                source = _source(document, number, row_number, quote)
                if len(row) >= 4:
                    label, value = row[2].strip(), row[3].strip()
                    unit = row[4].strip() if len(row) > 4 else None
                    line = ": ".join(p for p in [label, value] if p) + (" " + unit if unit else "")
                    if _condition_kind(label) in {"storage", "delivery"}:
                        current["conditions"].append({"key": _condition_kind(label), "label": label, "text": line,
                                                       "quote": quote, "source": source})
                    else:
                        if line and line not in current["_parts"]:
                            current["_parts"].append(line)
                        _add_requirement(current, label, value, unit, quote, source)
                else:
                    # Name cells may also contain brand/country requirements.
                    for line in _spec_lines(row[1].splitlines()[1:] + row[2].splitlines()):
                        _add_spec_line(current, line, source)
            continue
        if rows and all(len(row) == 1 for row in rows):
            for row_number, row in enumerate(rows, 1):
                text = row[0].strip()
                if not text or text.endswith(":"):
                    continue
                for paragraph in text.splitlines():
                    if paragraph.strip() and not paragraph.strip().endswith(":"):
                        conditions.append(_condition(paragraph, document, number, row_number))
    return items, conditions


def _section_bounds(lines):
    end_quantity = len(lines)
    specs_start, specs_end = None, len(lines)
    for index, line in enumerate(lines):
        if re.search(r"требовани[ея].*срок.*поставк", line, re.I):
            end_quantity = min(end_quantity, index)
        if re.search(r"(?:требования к качеству.*характеристик|технические и функциональные характеристики)", line, re.I):
            specs_start = index if specs_start is None else specs_start
        if re.search(r"^\s*(?:4[.)]?\s*)?прочие\s+требования", line, re.I):
            specs_end = index
            end_quantity = min(end_quantity, index)
    return end_quantity, specs_start, specs_end


def _text_items(document):
    lines = [line.strip() for line in document["text"].splitlines() if line.strip()]
    end_quantity, specs_start, specs_end = _section_bounds(lines)
    if specs_start is not None:
        end_quantity = min(end_quantity, specs_start)
    items, used_positions = [], set()
    for index, line in enumerate(lines[:end_quantity]):
        if not _UNIT.fullmatch(line) or index + 1 >= end_quantity:
            continue
        try:
            quantity = _quantity(lines[index + 1])
        except ValueError:
            continue
        if quantity is None:
            continue
        start = next((j for j in range(index - 1, max(-1, index - 22), -1) if _INDEX.fullmatch(lines[j])), None)
        if start is None or index <= start + 1:
            continue
        position = _INDEX.fullmatch(lines[start])[1]
        title = "\n".join(lines[start + 1:index])
        if position in used_positions or len(title) > 1200 or re.search(r"наименование|единица измерения|количество товара", title, re.I):
            continue
        item = _new_item(document, position, title, quantity, line,
                         _source(document, row=start + 1, quote="\n".join(lines[start:index + 2])))
        items.append(item)
        used_positions.add(position)
    if items:
        if specs_start is not None:
            by_position = {item["_position"]: item for item in items}
            starts = []
            for index in range(specs_start, specs_end):
                match = _INDEX.fullmatch(lines[index])
                if not match or match[1] not in by_position or index + 1 >= specs_end:
                    continue
                next_line = _space(lines[index + 1]).lower()
                expected = _space(by_position[match[1]]["title"]).lower()
                prefix = expected.split("производитель")[0].strip()
                if next_line and (prefix.startswith(next_line) or next_line.startswith(prefix[:25])):
                    starts.append((index, by_position[match[1]]))
            for count, (start, item) in enumerate(starts):
                stop = starts[count + 1][0] if count + 1 < len(starts) else specs_end
                for index in range(start + 1, stop):
                    _add_spec_line(item, lines[index], _source(document, row=index + 1, quote=lines[index]))
        conditions = []
        for index, line in enumerate(lines):
            if index >= specs_end or end_quantity <= index and (specs_start is None or index < specs_start):
                if _condition_kind(line) and not line.endswith(":") and len(line) > 20:
                    conditions.append(_condition(line, document, row=index + 1))
        return items, conditions
    # Plain text without a table remains editable, with no guessed quantity.
    title = next((line for line in lines if not re.search(r"^техническое\s+задание$", line, re.I)), "Описание закупки")
    title = title[:500]
    quantity, unit = None, None
    match = re.search(r"(?:количество|потребность)\s*[:=—-]?\s*(\d+(?:[.,]\d+)?)\s*(шт\.?|пачк[аи]?|пачек|упаковк[аи]?|комплект[а-я]*)\b", document["text"], re.I)
    if match:
        quantity, unit = _quantity(match[1]), _unit(match[2])
    item = _new_item(document, "1", title, quantity, unit, _source(document, quote=document["text"]))
    item["_parts"] = []
    for index, line in enumerate(lines):
        _add_spec_line(item, line, _source(document, row=index + 1, quote=line))
    if quantity is None:
        item["warnings"].append("Количество не задано однозначно: укажите его перед поиском.")
    if len(document["text"]) > 1000 or re.search(r"номер\s+позиции|окпд2|сведения\s+о\s+поставляемом", document["text"], re.I):
        item["warnings"].append("Проверьте разбиение на позиции: в тексте не найдена однозначная таблица состава закупки.")
    return [item], []


def _identifier(value):
    return re.sub(r"[\s-]", "", value).lower().replace("beta", "b")


def _finish_items(items, conditions):
    for item in items:
        parts = item.pop("_parts", [item["description"]])
        item.pop("_position", None)
        item["description"] = "\n".join(dict.fromkeys(part.strip() for part in parts if part.strip()))
        item["conditions"].extend(deepcopy(conditions))
        for requirement in item["requirements"]:
            value = requirement.get("value")
            if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value) and value[0] > value[1]:
                item["warnings"].append("В исходном ТЗ обратный диапазон: " + requirement["quote"])
        expected = {_identifier(m) for m in re.findall(r"\brh(?:IL\s*-?\s*\d+|TGF(?:beta|b)?\d+)\b", item["title"], re.I)}
        if expected:
            for line in item["description"].splitlines():
                found = re.findall(r"\brh(?:IL\s*-?\s*\d+|TGF(?:beta|b)?\d+)\b", line, re.I)
                unexpected = [m for m in found if _identifier(m) not in expected]
                if unexpected:
                    item["warnings"].append("В характеристике этой позиции упомянут другой товар («" + unexpected[0] + "»). Уточните исходное ТЗ: " + line)
        item["warnings"] = list(dict.fromkeys(item["warnings"]))
    return normalize_items(items)


def normalize_items(items):
    """Validate edited item data without silently manufacturing missing facts."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("Добавьте от одной до ста позиций закупки.")
    normalized, ids = [], set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("Некорректная позиция закупки.")
        requirements_supplied = "requirements" in item
        try:
            item = deepcopy(item)
        except RecursionError:
            raise ValueError("Некорректная вложенность данных позиции.") from None
        title, description = item.get("title"), item.get("description")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 1200:
            raise ValueError("Укажите название позиции длиной до 1200 символов.")
        if description is None:
            description = title
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= MAX_ITEM_CHARS:
            raise ValueError("Описание позиции пустое или слишком большое.")
        unit = item.get("unit")
        if unit is not None and (not isinstance(unit, str) or len(unit) > 80 or any(ord(c) < 32 for c in unit)):
            raise ValueError("Некорректная единица измерения позиции.")
        identifier = item.get("id") or "item-" + str(index)
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier) or identifier in ids:
            raise ValueError("Идентификаторы позиций должны быть уникальными.")
        ids.add(identifier)
        for name in ("requirements", "conditions", "warnings"):
            value = item.get(name, [])
            if not isinstance(value, list) or len(value) > 300:
                raise ValueError("Некорректные требования или условия позиции.")
            item[name] = value
        for requirement in item["requirements"]:
            if (not isinstance(requirement, dict) or not isinstance(requirement.get("key"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", requirement["key"])
                    or requirement.get("op", "eq") not in {"eq", "gte", "lte", "gt", "lt", "in", "contains", "between", "=", ">=", "<="}
                    or not isinstance(requirement.get("label", ""), str)
                    or len(requirement.get("label", "")) > 2000
                    or not isinstance(requirement.get("quote", ""), str)
                    or len(requirement.get("quote", "")) > 25_000):
                raise ValueError("Некорректное требование позиции.")
            value = requirement.get("value")
            if isinstance(value, (dict, bool)) or value is None or isinstance(value, str) and len(value) > 10_000:
                raise ValueError("Некорректное значение требования.")
            if isinstance(value, list) and (len(value) > 30 or any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in value)):
                raise ValueError("Некорректный диапазон или список значений требования.")
            if requirement.get("unit") is not None and (not isinstance(requirement["unit"], str) or len(requirement["unit"]) > 80):
                raise ValueError("Некорректная единица характеристики.")
        for condition in item["conditions"]:
            if (not isinstance(condition, dict) or not isinstance(condition.get("text"), str)
                    or not 1 <= len(condition["text"]) <= 25_000):
                raise ValueError("Некорректные условия позиции.")
        if any(not isinstance(warning, str) or len(warning) > 25_000 for warning in item["warnings"]):
            raise ValueError("Некорректное предупреждение позиции.")
        if "source" in item and not isinstance(item["source"], dict):
            raise ValueError("Некорректный источник позиции.")
        # Reject arbitrary JSON nesting and oversized source annotations before
        # the item can be persisted or used in a model context.
        try:
            encoded = json.dumps(item, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("Некорректные данные позиции.") from None
        if len(encoded) > 100_000:
            raise ValueError("Данные позиции слишком большие.")
        item.update(id=identifier, title=title.strip(), description=description.strip(),
                    quantity=_quantity(item.get("quantity")), unit=_unit(unit))
        # An omitted selection delegates parsing to the research request.
        # An explicitly empty list is the user's authoritative edited choice.
        if not requirements_supplied:
            item.pop("requirements", None)
        normalized.append(item)
    return normalized


def prepare_context_note(description=""):
    """Canonicalise an edited note for already prepared file-backed items.

    A supplemental condition is not a new procurement line. This uses the same
    document/condition representation as a note supplied together with files.
    Empty text clears the note; this function performs no external or DB calls.
    """
    if not isinstance(description, str) or len(description) > MAX_TEXT_CHARS:
        raise ValueError("Описание закупки слишком большое или имеет неверный формат.")
    text = _clean_text(description)
    if not text:
        return {"description": "", "items": [], "documents": [], "conditions": [], "warnings": []}
    digest = hashlib.sha256(text.encode()).hexdigest()
    document = {"id": digest[:20], "name": "Текст запроса", "sha256": digest,
                "text": text, "size": len(text.encode()), "format": "text"}
    return {"description": description.strip(), "items": [], "documents": [document],
            "conditions": [_condition(text, document, label="Дополнение к ТЗ")], "warnings": []}


def prepare_specifications(description="", files=None):
    """Return document evidence and editable items from text and/or attachments.

    ``files`` contains objects ``{name: str, data: strict_base64_str}``.
    The function performs no application/database writes or external requests.
    """
    if not isinstance(description, str) or len(description) > MAX_TEXT_CHARS:
        raise ValueError("Описание закупки слишком большое или имеет неверный формат.")
    if files is None:
        files = []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError("Можно прикрепить не более восьми файлов ТЗ.")
    if not description.strip() and not files:
        raise ValueError("Добавьте описание закупки или прикрепите ТЗ.")
    documents, prepared, warnings, seen = [], [], [], set()
    total_bytes, total_chars = 0, len(description)
    for upload in files:
        document, blocks = _document(upload)
        total_bytes += document["size"]
        total_chars += len(document["text"])
        if total_bytes > MAX_TOTAL_BYTES or total_chars > MAX_TOTAL_TEXT_CHARS:
            raise ValueError("Суммарный размер вложений слишком большой; разделите закупку.")
        if document["sha256"] in seen:
            warnings.append("Повторное вложение «" + document["name"] + "» не добавлено второй раз.")
            continue
        seen.add(document["sha256"])
        documents.append(document)
        items, conditions = _table_items(document, blocks)
        if not items:
            items, conditions = _text_items(document)
        prepared.extend(_finish_items(items, conditions))
    if description.strip():
        text = _clean_text(description)
        digest = hashlib.sha256(text.encode()).hexdigest()
        document = {"id": digest[:20], "name": "Текст запроса", "sha256": digest,
                    "text": text, "size": len(text.encode()), "format": "text"}
        documents.insert(0, document)
        # With files an accompanying note is shared context, not an extra item.
        # The note remains exact source evidence and is visible for review.
        if files:
            note = _condition(text, document, label="Дополнение к ТЗ")
            for item in prepared:
                item["conditions"].append(deepcopy(note))
        else:
            items, conditions = _text_items(document)
            prepared.extend(_finish_items(items, conditions))
    if len(prepared) > MAX_ITEMS:
        raise ValueError("В ТЗ больше ста позиций; разделите закупку.")
    prepared = normalize_items(prepared)
    for item in prepared:
        warnings.extend(item["warnings"])
    summary = description.strip() if description.strip() else "Закупка по ТЗ: " + "; ".join(d["name"] for d in documents)
    return {"description": summary, "items": prepared, "documents": documents,
            "warnings": list(dict.fromkeys(warnings))}


if __name__ == "__main__" and sys.argv[1:] == ["--pdf-worker"]:
    raise SystemExit(_pdf_worker())

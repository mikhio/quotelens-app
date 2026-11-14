"""Real procurement inputs and adversarial document boundaries."""
from __future__ import annotations

import base64
from copy import deepcopy
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import re
import unittest
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

from quotelens.spec_ingest import (MAX_FILES, MAX_FILE_BYTES, MAX_ITEMS,
                                  normalize_items, prepare_specifications)


FIXTURES = Path(__file__).parent / "fixtures" / "specifications"


def upload(name, raw):
    return {"name": name, "data": base64.b64encode(raw).decode()}


def fixture(name):
    return upload(name, (FIXTURES / name).read_bytes())


def docx(body, extra=None):
    result = BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return result.getvalue()


def cell(value):
    return "<w:tc><w:p><w:r><w:t>" + escape(value) + "</w:t></w:r></w:p></w:tc>"


def table(rows):
    return "<w:tbl>" + "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in rows) + "</w:tbl>"


def scalar_requirements(item, key):
    return [(r["op"], r["value"]) for r in item["requirements"] if r["key"] == key]


class SpecificationInputs(unittest.TestCase):
    def test_real_printer_preserves_model_specs_and_delivery_separately(self):
        result = prepare_specifications(files=[fixture("printer.docx")])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["unit"], "шт")
        self.assertIn("CREATBOT D600 PRO", item["title"])
        self.assertIn("600х600х600", item["description"])
        self.assertIn("420", item["description"])
        self.assertNotIn("календарных дней", item["description"])
        conditions = "\n".join(c["text"] for c in item["conditions"])
        self.assertIn("45 (сорока пяти) календарных дней", conditions)
        self.assertIn("Научный пер., дом 4", conditions)
        self.assertIn("12 (двенадцати) месяцев", conditions)
        self.assertEqual(scalar_requirements(item, "country"), [("eq", "Китай")])

    def test_real_paper_has_three_exact_quantities_and_distinct_specs(self):
        result = prepare_specifications(files=[fixture("paper.docx")])
        items = result["items"]
        self.assertEqual([i["quantity"] for i in items], [2500, 25, 50])
        self.assertEqual([i["unit"] for i in items], ["пачка"] * 3)
        self.assertEqual([scalar_requirements(i, "format") for i in items], [[("eq", "A4")], [("eq", "A3")], [("eq", "A4")]])
        self.assertEqual(scalar_requirements(items[0], "density_gsm"), [("between", [77, 83])])
        self.assertEqual(scalar_requirements(items[1], "density_gsm"), [("between", [77, 83])])
        self.assertEqual(scalar_requirements(items[2], "density_gsm"), [("eq", 65)])
        self.assertEqual(scalar_requirements(items[0], "sheets_per_pack"), [("between", [490, 510])])
        self.assertEqual(scalar_requirements(items[2], "sheets_per_pack"), [("eq", 500)])
        for item in items:
            self.assertNotIn("гарантийного обслуживания", item["description"])
            self.assertTrue(any("5 (пяти) рабочих дней" in c["text"] for c in item["conditions"]))
            self.assertTrue(any("разгрузке" in c["text"] for c in item["conditions"]))

    def test_real_cytokine_merged_rows_ranges_and_conflict_are_retained(self):
        result = prepare_specifications(files=[fixture("cytokines.docx")])
        items = result["items"]
        self.assertEqual([i["quantity"] for i in items], [2, 1, 1, 2, 1])
        self.assertEqual([scalar_requirements(i, "mass_ug") for i in items], [[("gte", 10)]] * 3 + [[("gte", 100)]] * 2)
        self.assertEqual(scalar_requirements(items[0], "ed50_ng_ml"), [("gt", 1), ("lt", 1.6)])
        self.assertEqual(scalar_requirements(items[4], "purity_percent"), [("gt", 98)])
        self.assertIn("rhIL-2", "\n".join(items[4]["warnings"]))
        self.assertIn("rhIL-2", items[4]["description"])
        self.assertNotIn("rhIL-7", items[1]["description"])
        self.assertTrue(any("не выше" in c["text"] and "-70" in c["text"] for c in items[0]["conditions"]))
        self.assertFalse(any("хранени" in r["label"].lower() for r in items[0]["requirements"]))

    def test_every_extracted_requirement_has_a_real_source_quote(self):
        for name in ("printer.docx", "paper.docx", "cytokines.docx"):
            result = prepare_specifications(files=[fixture(name)])
            source = re.sub(r"\s+", " ", result["documents"][0]["text"])
            for item in result["items"]:
                self.assertIn(re.sub(r"\s+", " ", item["source"]["quote"]), source)
                for r in item["requirements"]:
                    self.assertIn(re.sub(r"\s+", " ", r["quote"]), source, (name, r))
                    self.assertEqual(r["source"]["document_id"], result["documents"][0]["id"])

    def test_multiple_renamed_files_are_content_driven_and_ids_unique(self):
        files = [fixture("printer.docx"), fixture("paper.docx"), fixture("cytokines.docx")]
        for i, f in enumerate(files):
            f["name"] = f"input-{i}.docx"
        result = prepare_specifications(files=files)
        self.assertEqual(len(result["items"]), 9)
        self.assertEqual(len({i["id"] for i in result["items"]}), 9)
        self.assertEqual([i["quantity"] for i in result["items"]], [1, 2500, 25, 50, 2, 1, 1, 2, 1])
        self.assertGreater(len(result["documents"][1]["text"]), 4000)

    def test_duplicate_attachment_does_not_duplicate_positions(self):
        file = fixture("paper.docx")
        duplicate = {**file, "name": "same-content.docx"}
        result = prepare_specifications(files=[file, duplicate])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(len(result["documents"]), 1)
        self.assertIn("Повторное вложение", "\n".join(result["warnings"]))

    def test_pasted_word_table_keeps_separate_item_counts(self):
        # Word's plain-text clipboard loses cell boundaries but keeps row order.
        with ZipFile(FIXTURES / "paper.docx") as archive:
            from xml.etree import ElementTree as ET
            root = ET.fromstring(archive.read("word/document.xml"))
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        pasted = "\n".join("".join(t.text or "" for t in p.iter(w + "t")) for p in root.iter(w + "p"))
        result = prepare_specifications(pasted)
        self.assertEqual([i["quantity"] for i in result["items"]], [2500, 25, 50])
        self.assertEqual([scalar_requirements(i, "format") for i in result["items"]], [[("eq", "A4")], [("eq", "A3")], [("eq", "A4")]])
        self.assertEqual(re.sub(r"\s+", " ", result["documents"][0]["text"]), re.sub(r"\s+", " ", pasted.strip()))

    def test_real_truncated_paste_still_keeps_all_quantity_rows(self):
        from xml.etree import ElementTree as ET
        with ZipFile(FIXTURES / "paper.docx") as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        pasted = "\n".join("".join(t.text or "" for t in p.iter(w + "t")) for p in root.iter(w + "p"))[:4000]
        self.assertEqual([i["quantity"] for i in prepare_specifications(pasted)["items"]], [2500, 25, 50])

    def test_simple_text_never_uses_pack_contents_as_purchase_quantity(self):
        result = prepare_specifications("Бумага А4, 80 г/м², 500 листов в пачке")
        item = result["items"][0]
        self.assertIsNone(item["quantity"])
        self.assertIsNone(item["unit"])
        self.assertIn("Количество не задано", "\n".join(item["warnings"]))
        result = prepare_specifications("Бумага А4\nКоличество: 2500 пачек")
        self.assertEqual(result["items"][0]["quantity"], 2500)
        self.assertEqual(result["items"][0]["unit"], "пачка")

    def test_table_preserves_unknown_quantity_and_unit_for_review(self):
        rows = [["Номер позиции", "Наименование товара", "Единица измерения", "Количество товара"],
                ["1", "Спектрометр нового типа", "", "уточняется"]]
        result = prepare_specifications(files=[upload("new.docx", docx(table(rows)))])
        item = result["items"][0]
        self.assertIsNone(item["quantity"])
        self.assertIsNone(item["unit"])
        self.assertEqual(item["title"], "Спектрометр нового типа")
        self.assertEqual(len(item["warnings"]), 2)

    def test_context_note_and_original_file_both_retained(self):
        note = "Поставка должна быть до конца месяца."
        result = prepare_specifications(note, [fixture("paper.docx")])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["description"], note)
        self.assertEqual(result["documents"][0]["text"], note)
        for item in result["items"]:
            self.assertTrue(any(c["label"] == "Дополнение к ТЗ" and c["text"] == note for c in item["conditions"]))

    def test_txt_md_and_utf16_are_read_without_external_services(self):
        for name, raw in [("тз.txt", "Калибратор\nКоличество: 2 шт.".encode()),
                          ("тз.md", "Калибратор\nКоличество: 2 шт.".encode("utf-16")),
                          ("тз.txt", "Калибратор\nКоличество: 2 шт.".encode("cp1251"))]:
            result = prepare_specifications(files=[upload(name, raw)])
            self.assertEqual(result["items"][0]["quantity"], 2)
            self.assertEqual(result["items"][0]["unit"], "шт")


class SpecificationBoundaries(unittest.TestCase):
    def test_invalid_requests_and_file_limits(self):
        for description, files in [(None, []), ("", []), ("", "not a list"), ("", [fixture("paper.docx")] * (MAX_FILES + 1))]:
            with self.subTest(description=description, count=type(files).__name__):
                with self.assertRaises(ValueError):
                    prepare_specifications(description, files)
        for name in ("../a.docx", "a/b.docx", "a\\b.docx", "a\x00.docx", "a\n.docx", "a.exe"):
            with self.assertRaises(ValueError):
                prepare_specifications(files=[upload(name, b"hello")])
        for data in ("%%%", "", "a" * (((MAX_FILE_BYTES + 2) // 3) * 4 + 4)):
            with self.assertRaises(ValueError):
                prepare_specifications(files=[{"name": "a.docx", "data": data}])

    def test_zip_traversal_symlink_and_macros_are_rejected(self):
        for extra in ({"../outside.txt": "secret"}, {"/absolute.xml": "hello"}, {"word/vbaProject.bin": "macro"}):
            with self.assertRaises(ValueError):
                prepare_specifications(files=[upload("a.docx", docx("<w:p/>", extra))])
        raw = BytesIO()
        with ZipFile(raw, "w") as archive:
            link = ZipInfo("word/link")
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "/etc/passwd")
        with self.assertRaises(ValueError):
            prepare_specifications(files=[upload("a.docx", raw.getvalue())])

    def test_bad_archives_entities_and_compression_bombs_are_rejected(self):
        for raw in (b"not a zip", docx("<w:p/>", {"word/huge.xml": "a" * 1_000_000})):
            with self.assertRaises(ValueError):
                prepare_specifications(files=[upload("a.docx", raw)])
        for encoding in ("utf-8", "utf-16"):
            raw = BytesIO()
            with ZipFile(raw, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><x>&leak;</x>').encode(encoding))
            with self.assertRaises(ValueError):
                prepare_specifications(files=[upload("a.docx", raw.getvalue())])

    def test_text_binary_and_forged_pdf_are_rejected(self):
        for file in [upload("a.txt", b"text\x00binary"), upload("a.pdf", b"not pdf")]:
            with self.assertRaises(ValueError):
                prepare_specifications(files=[file])

    def test_edited_items_validate_unknowns_not_nan_or_duplicate_ids(self):
        item = {"id": "one", "title": "Бумага А4", "description": "Бумага А4", "quantity": None, "unit": None}
        self.assertIsNone(normalize_items([item])[0]["quantity"])
        for value in (0, -1, True, float("nan"), float("inf"), "unknown"):
            with self.assertRaises(ValueError):
                normalize_items([{**item, "quantity": value}])
        with self.assertRaises(ValueError):
            normalize_items([item, item])
        with self.assertRaises(ValueError):
            normalize_items([item] * (MAX_ITEMS + 1))
        for key, value in [("requirements", ["bad"]), ("requirements", [{"key": "x", "value": float("nan"), "op": "eq"}]),
                           ("conditions", ["bad"]), ("warnings", [{}]), ("source", "bad")]:
            with self.assertRaises(ValueError):
                normalize_items([{**item, key: value}])

    def test_normalization_distinguishes_omitted_from_explicit_empty_requirements(self):
        item = {"id": "paper", "title": "Бумага A4", "description": "Бумага A4 80 г/м², 500 листов в пачке",
                "quantity": 100, "unit": "пачка"}
        omitted = normalize_items([item])[0]
        self.assertNotIn("requirements", omitted)
        self.assertNotIn("requirements", item)
        self.assertNotIn("requirements", normalize_items([omitted])[0])

        edited = {**item, "requirements": []}
        normalized = normalize_items([edited])[0]
        self.assertIn("requirements", normalized)
        self.assertEqual(normalized["requirements"], [])
        self.assertEqual(normalize_items([normalized])[0]["requirements"], [])

    def test_prepared_document_requirements_survive_normalization(self):
        prepared = prepare_specifications(files=[fixture("paper.docx")])["items"]
        normalized = normalize_items(prepared)
        self.assertTrue(all(item["requirements"] for item in normalized))
        self.assertEqual([i["requirements"] for i in normalized], [i["requirements"] for i in prepared])

    def test_unknown_units_are_kept_in_structured_tables(self):
        rows = [["Номер позиции", "Наименование товара", "Единица измерения", "Количество товара"], ["1", "Набор реагентов", "флакон", "12"]]
        result = prepare_specifications(files=[upload("not-specific.docx", docx(table(rows)))])
        self.assertEqual(result["items"][0]["unit"], "флакон")
        self.assertEqual(result["items"][0]["quantity"], 12)


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is optional outside the deployment image")
class PdfInput(unittest.TestCase):
    def test_text_pdf_is_extracted_in_a_restricted_child(self):
        content = b"BT /F1 12 Tf 72 720 Td (Printer Model X) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                   b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        data = b"%PDF-1.4\n"
        offsets = [0]
        for i, value in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(i).encode() + b" 0 obj\n" + value + b"\nendobj\n"
        xref = len(data)
        data += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
        data += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
        result = prepare_specifications(files=[upload("example.pdf", data)])
        self.assertIn("Printer Model X", result["documents"][0]["text"])
        self.assertIsNone(result["items"][0]["quantity"])

    def test_encrypted_or_image_only_pdf_is_explicitly_unavailable(self):
        from pypdf import PdfWriter
        for encrypted in (False, True):
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            if encrypted:
                writer.encrypt("password")
            output = BytesIO()
            writer.write(output)
            with self.assertRaises(ValueError):
                prepare_specifications(files=[upload("scan.pdf", output.getvalue())])


if __name__ == "__main__":
    unittest.main()

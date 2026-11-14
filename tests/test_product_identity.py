"""Quoted product identity is independent from technical completeness.

Most fixtures are synthetic, with no market-price claim. The small positive
headline is a minimized public primary-page excerpt captured without a model:
https://3dpt.ru/product/3d-printer-creatbot-d600-pro, 2026-11-14T12:32:35Z.
Full capture SHA256: 0556fe6eff72e4d573b5adbd7f0dd740318100961ebe9819bc2221360687dd4d.
No owner job, contact details or complete private capture is included here.
"""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quotelens.engine import (analyze_offers, parse_request, positive_product_identity,
                              product_identity_assessment, _model, _attribute_match)
from quotelens.extraction import extract_product_offer
from quotelens.procurement import ItemStore, request_for_item, refresh_product_identity_view
from quotelens.selection import finalize
from quotelens.targeting import annotate_target


def printer_request(description="3D принтер CreatBot D600 PRO"):
    return {"product": description, "description": description, "category": "other",
            "quantity": 1, "unit": "шт", "missing_critical_fields": [],
            "requirements": [{"key": "nozzle_diameter", "label": "Диаметр сопла",
                              "op": "eq", "value": 0.4, "unit": "мм"}]}


def quoted_offer(title, *, identifier="one", complete=False):
    url = "https://" + identifier + ".example.invalid/product"
    offer = {"id": identifier, "title": title, "supplier": "Учебный продавец " + identifier,
             "url": url, "domain": identifier + ".example.invalid", "retrieved_at": "2026-11-14T00:00:00Z",
             "price": 100, "currency": "RUB", "unit": "шт", "tax": "included",
             "provenance": "synthetic", "attributes": {}, "evidence": {
                 "title": {"quote": title, "url": url},
                 "price": {"quote": "Учебная цена100рублей", "url": url},
                 "currency": {"quote": "RUB", "url": url},
                 "unit": {"quote": "За1шт", "url": url},
                 "tax": {"quote": "НДС включён", "url": url}}}
    if complete:
        offer["attributes"]["nozzle_diameter"] = 0.4
        offer["evidence"]["attributes.nozzle_diameter"] = {"quote": "Диаметр сопла0.4мм", "url": url}
    return offer


def printer_document(rows):
    """Small synthetic table; only property-label shapes reproduce the source."""
    title = "3D-принтер CreatBot D600 Pro"
    text = title + " " + " ".join(label + " " + value for label, value in rows)
    return {"title": title, "primary_title": title, "text": text,
            "html": '<article class="product"><h1>' + title + '</h1><table>' + ''.join(
                '<tr><td>' + label + '</td><td>' + value + '</td></tr>' for label, value in rows) + '</table></article>',
            "url": "https://qa.example.invalid/printer", "retrieved_at": "2026-11-14T00:00:00Z",
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


class ProductIdentityTests(unittest.TestCase):
    def test_known_wrong_objects_are_excluded_not_unknown(self):
        request = printer_request()
        for title in ("Станок гравировальный DIAOTU T1 Pro", "3D ручка Qiwer 703D",
                      "Лазерный гравировальный станок SCULPFUN C1",
                      "Сопло для 3D принтера CreatBot D600 PRO",
                      "Экран для 3D-принтера CreatBot D600 PRO"):
            with self.subTest(title=title):
                offer = quoted_offer(title)
                offer["manual_acceptances"] = {"attributes.manufacturer": {
                    "reason": "Принимаю только изготовителя", "revision": 1, "at": "2026-11-14T00:00:00Z"}}
                result = finalize(request, [offer])
                self.assertEqual(result["offers"][0]["status"], "excluded")
                self.assertEqual(result["offers"][0]["product_identity"]["status"], "mismatch")
                self.assertFalse(positive_product_identity(request, offer))
                self.assertIsNone(result["calculation"]["provisional"]["total"])

    def test_known_wrong_named_printer_model_is_a_contradiction(self):
        for title in ("3D-принтер Creality Ender3", "3D принтер CreatBot D600",
                      "3D принтер CreatBot D600 Pro2", "3D принтер CreatBot D600 Pro HS"):
            with self.subTest(title=title):
                result = finalize(printer_request(), [quoted_offer(title, complete=True)])
                self.assertEqual(result["offers"][0]["status"], "excluded")
                self.assertIn("Модель", result["offers"][0]["product_identity"]["reason"])

    def test_named_model_punctuation_and_cyrillic_aliases_keep_identity(self):
        for title in ("3D-принтер CreatBot D600-PRO", "3Д-принтер CreatBot D600-ПРО",
                      "CREATBOT D600 PRO 3D printer", "3D принтер с лазерным гравером CreatBot D600 PRO"):
            with self.subTest(title=title):
                offer = quoted_offer(title)
                self.assertEqual(product_identity_assessment(printer_request(), offer)["status"], "matches")
                self.assertEqual(finalize(printer_request(), [offer])["offers"][0]["status"], "uncertain")

    def test_broad_or_explicit_equivalent_request_does_not_reject_another_brand(self):
        offer = quoted_offer("3D-принтер Creality Ender3")
        for subject in ("3D принтер", "3D принтер CreatBot D600 PRO или эквивалент"):
            with self.subTest(subject=subject):
                request = printer_request(subject)
                self.assertTrue(positive_product_identity(request, offer))
                self.assertEqual(finalize(request, [offer])["offers"][0]["status"], "uncertain")
        # An expressly retained structured manufacturer is still a hard field.
        request = printer_request("3D принтер CreatBot D600 PRO или эквивалент")
        request["requirements"] = [{"key": "manufacturer", "label": "Производитель", "op": "eq", "value": "CreatBot"}]
        offer["attributes"]["manufacturer"] = "Creality"
        offer["evidence"]["attributes.manufacturer"] = {"quote": "Производитель Creality", "url": offer["url"]}
        self.assertEqual(finalize(request, [offer])["offers"][0]["status"], "excluded")

    def test_synonym_without_named_model_or_unquoted_title_stays_unknown(self):
        for offer in (quoted_offer("Аддитивная установка"), quoted_offer("3D-принтер")):
            self.assertEqual(product_identity_assessment(printer_request(), offer)["status"], "unconfirmed")
            self.assertEqual(finalize(printer_request(), [offer])["offers"][0]["status"], "uncertain")
        offer = quoted_offer("Станок гравировальный DIAOTU T1 Pro")
        del offer["evidence"]["title"]
        self.assertEqual(product_identity_assessment(printer_request(), offer)["status"], "unconfirmed")
        self.assertEqual(product_identity_assessment({}, {})["status"], "unconfirmed")

    def test_real_matching_primary_headline_extracts_pro_variant_without_inventing_specs(self):
        title = "3D-принтер CreatBot D600 Pro"
        text = title
        document = {"title": title, "primary_title": title, "text": text,
                    "html": '<article class="product"><h1>' + title + '</h1></article>',
                    "url": "https://3dpt.ru/product/3d-printer-creatbot-d600-pro",
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "retrieved_at": "2026-11-14T12:32:35Z", "method": "minimized_primary_excerpt"}
        request = printer_request()
        offer = extract_product_offer(document, request)
        self.assertIsNotNone(offer)
        self.assertEqual(_model(offer["attributes"]["model"]), "d600pro")
        self.assertTrue(positive_product_identity(request, offer))
        self.assertNotIn("nozzle_diameter", offer["attributes"])
        self.assertIsNone(offer["price"])
        self.assertEqual(finalize(request, [offer])["offers"][0]["status"], "uncertain")

    def test_paper_identity_and_missing_values_are_unchanged(self):
        request = parse_request("Бумага офисная A4, 80 г/м2, 500 листов в пачке")
        offer = quoted_offer("Бумага A4 для принтера")
        self.assertTrue(positive_product_identity(request, offer))
        result = analyze_offers(request, [offer])
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertNotIn("density_gsm", result["offers"][0]["attributes"])

    def test_primary_printer_aliases_keep_micro_units_and_uncertain_lists(self):
        request = printer_request()
        request["requirements"] = [
            {"key": "xy_um", "label": "Точность позиционирования X/Y", "value": 5.08, "unit": "μm", "op": "eq"},
            {"key": "xy_mm", "label": "Точность позиционирования X/Y", "value": 0.00508, "unit": "мм", "op": "eq"},
            {"key": "z_um", "label": "Точность позиционирования Z", "value": 1.25, "unit": "μm", "op": "eq"},
            {"key": "interfaces", "label": "Интерфейсы подключения", "value": "USB - наличие", "op": "eq"},
            {"key": "dimensions", "label": "Габариты принтера без упаковки (ШхГхВ)", "value": "915x845x1085 мм", "unit": "мм", "op": "eq"},
            {"key": "materials", "label": "Поддерживаемые пластики", "value": "PLA, ABS, TPU и другие", "op": "eq"},
            {"key": "nozzles", "label": "Диаметр сопел", "value": "0.3/0.4/0.6 мм", "unit": "мм", "op": "eq"},
            {"key": "plate", "label": "Материал рабочей платформы", "value": "Алюминий или стекло", "op": "eq"}]
        document = printer_document([
            ("Точность по оси XY", "5.08 микрон"), ("Точность по оси Z", "1.25 микрон"),
            ("Интерфейс", "USB"), ("Габариты устройства", "915х845х1085 мм"),
            ("Расходные материалы", "PLA, ABS"), ("Диаметр сопла", "0.4 мм (опционально0.3мм)"),
            ("Печатная платформа", "Стекло")])
        offer = extract_product_offer(document, request)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["attributes"]["xy_um"], 5.08)
        self.assertAlmostEqual(offer["attributes"]["xy_mm"], 0.00508)
        self.assertEqual(offer["attributes"]["z_um"], 1.25)
        proof = offer["evidence"]["attributes.xy_mm"]
        self.assertIn("5.08 микрон", proof["quote"])
        self.assertEqual(proof["raw_value"], "5.08 микрон")
        self.assertEqual(proof["source_unit"], "микрон")
        self.assertEqual(proof["normalized_unit"], "мм")
        for requirement in request["requirements"][:5]:
            self.assertIs(_attribute_match(offer, requirement), True)
        for requirement in request["requirements"][5:7]:
            self.assertIn(requirement["key"], offer["attributes"])
            self.assertIsNone(_attribute_match(offer, requirement))
        self.assertIs(_attribute_match(offer, request["requirements"][7]), True)
        self.assertEqual(finalize(request, [offer])["offers"][0]["status"], "uncertain")

    def test_precision_conflict_missing_units_and_build_envelope_are_not_guessed(self):
        request = printer_request()
        request["requirements"] = [
            {"key": "xy", "label": "Точность позиционирования X/Y", "value": 0.00508, "unit": "мм", "op": "eq"},
            {"key": "build", "label": "Печать одним экструдером", "value": "600х600х600 мм", "unit": "мм", "op": "eq"}]
        document = printer_document([("Точность по оси XY", "5.08"), ("Габариты устройства", "915х845х1085 мм")])
        offer = extract_product_offer(document, request)
        self.assertNotIn("build", offer["attributes"])
        self.assertIsNone(_attribute_match(offer, request["requirements"][0]))
        document = printer_document([("Точность по оси XY", "5.08 микрон"), ("Точность по оси XY", "10.16 микрон")])
        self.assertNotIn("xy", extract_product_offer(document, request)["attributes"])

    def test_dryer_prose_is_bound_to_its_section_and_keeps_explicit_settings(self):
        request = printer_request()
        requirement = {"key": "dryer", "label": "Максимальный нагрев отсека для сушки пластика", "value": 65, "unit": "град", "op": "eq"}
        request["requirements"] = [requirement]
        description = "Отсек для сушки пластика Температура в камере может достигать 45 и 65 °C."
        document = printer_document([])
        document["html"] = document["html"].replace('</article>', '<section><h2>Отсек для сушки пластика</h2><p>Температура в камере может достигать45 и65 °C.</p></section></article>')
        # Visible capture spacing remains authoritative for source quotes.
        document["html"] = document["html"].replace('достигать45 и65', 'достигать 45 и 65')
        document["text"] += " " + description
        offer = extract_product_offer(document, request)
        self.assertEqual(offer["attributes"]["dryer"], 65)
        self.assertIn("45 и 65", offer["evidence"]["attributes.dryer"]["quote"])
        self.assertEqual(offer["evidence"]["attributes.dryer"]["normalization"], "maximum_of_explicit_values")
        self.assertIs(_attribute_match(offer, requirement), True)
        document["text"] = document["text"].replace("Отсек для сушки пластика", "Печатная камера")
        document["html"] = document["html"].replace("Отсек для сушки пластика", "Печатная камера")
        self.assertNotIn("dryer", extract_product_offer(document, request)["attributes"])

    def test_wrong_warm_hints_leave_named_fresh_discovery_available(self):
        item = {"id": "printer", "title": "3D принтер CreatBot D600 PRO",
                "description": "3D принтер CreatBot D600 PRO", "quantity": 1, "unit": "шт", "requirements": []}
        request = request_for_item(item)
        rows = [{"title": title, "url": "https://qa.example.invalid/" + str(n)}
                for n, title in enumerate(("Станок гравировальный DIAOTU T1 Pro", "3D ручка Qiwer 703D"))]
        owner = SimpleNamespace(store=SimpleNamespace(discovery_urls=lambda *args, **kwargs: rows))
        self.assertEqual(ItemStore(owner, item).discovery_urls(request), [])
        self.assertIn("CREATBOT D600 PRO", " ".join(request["search_terms"]).upper())
        from quotelens.agent import Research
        research = object.__new__(Research)
        research.request, research.seen_queries = request, set()
        research.unavailable_sites, research.site_failures = {}, {}
        query = research.next_discovery_query()
        self.assertIn("CREATBOT D600 PRO", query.upper())
        self.assertNotIn("DIAOTU", query.upper())
        structured = {**item, "title": "3D принтер", "requirements": [
            {"key": "manufacturer", "label": "Производитель", "value": "CreatBot", "op": "eq"},
            {"key": "model", "label": "Модель", "value": "D600 PRO", "op": "eq"}]}
        terms = " ".join(request_for_item(structured)["search_terms"]).upper()
        self.assertIn("CREATBOT", terms)
        self.assertIn("D600 PRO", terms)

    def test_saved_derived_view_removes_stale_ready_and_provisional_totals_without_mutation(self):
        request = printer_request()
        offers = [quoted_offer("3D принтер CreatBot D600", identifier=str(n), complete=True) for n in range(3)]
        # Simulate the former faulty identity gate, preserving the real source
        # titles. The regression checks replay of a stale saved derivation.
        with patch('quotelens.engine.product_identity_assessment', return_value={"status": "matches"}):
            saved = annotate_target(finalize(request, offers), enforce=True)
        saved["search"] = {"search_policy": {"mode": "target"}, "started_at": "2026-11-01T00:00:00Z"}
        before = deepcopy(saved)
        self.assertEqual(saved["calculation"]["status"], "ready")
        refreshed = refresh_product_identity_view(saved)
        self.assertEqual(saved, before)
        self.assertTrue(all(o["status"] == "excluded" for o in refreshed["offers"]))
        self.assertEqual(refreshed["calculation"]["status"], "insufficient_data")
        self.assertIsNone(refreshed["calculation"]["provisional"]["total"])
        self.assertFalse(refreshed["target_group"]["ready"])
        self.assertEqual(refreshed["base_result"], before["base_result"])
        self.assertEqual(refreshed["search"], before["search"])


if __name__ == '__main__':
    unittest.main()

"""Conditional totals must never bypass strict evidence and exclusion rules."""
from copy import deepcopy
import base64
from pathlib import Path
import unittest

from quotelens.engine import analyze_offers, parse_request, positive_product_identity, _requirement_match
from quotelens.selection import finalize


def observation(identifier, price, *, tax="unknown", unit="шт", model=None):
    url = f"https://{identifier}.example/product/"
    proof = lambda quote: {"quote": quote, "url": url}
    return {"id": identifier, "title": "Осциллограф " + (model or identifier), "supplier": identifier,
            "url": url, "retrieved_at": "2026-11-12T12:00:00Z", "provenance": "synthetic",
            "price": price, "currency": "RUB", "unit": unit, "tax": tax,
            "attributes": {"model": model or identifier, "channels": 4, "bandwidth_mhz": 100},
            "evidence": {"price": proof(str(price) + " RUB"), "currency": proof(str(price) + " RUB"),
                         "unit": proof("за 1 шт."), "attributes.channels": proof("4 канала"),
                         "attributes.bandwidth_mhz": proof("Полоса пропускания 100 МГц")}}


def sheet_purchase(*, pack_size=500, price="1.23", quantity=1000, tax="included"):
    """Explicit QA prices: one pack is divided into its confirmed sheet count."""
    request = parse_request(f"Бумага А4 80 г/м2, пачка {pack_size} листов. Цена за лист", quantity)
    offers = []
    for identifier in ("paper-a", "paper-b", "paper-c"):
        url = f"https://{identifier}.example/product/"
        proof = lambda quote: {"quote": quote, "url": url}
        offers.append({"id": identifier, "title": "Бумага А4 " + identifier, "supplier": identifier,
                       "url": url, "retrieved_at": "2026-11-12T12:00:00Z", "provenance": "synthetic",
                       "price": price, "currency": "RUB", "unit": "пачка", "pack_size": pack_size, "tax": tax,
                       "attributes": {"format": "A4", "density_gsm": 80, "sheets_per_pack": pack_size},
                       "evidence": {"price": proof(str(price) + " RUB"), "currency": proof(str(price) + " RUB"),
                                    "unit": proof("За 1 пачку"), "pack_size": proof(f"В пачке {pack_size} листов"),
                                    "tax": proof("с НДС"), "attributes.format": proof("Формат A4"),
                                    "attributes.density_gsm": proof("Плотность 80 г/м2"),
                                    "attributes.sheets_per_pack": proof(f"В пачке {pack_size} листов")}})
    return request, offers


class ConditionalCalculationTests(unittest.TestCase):
    def test_unknown_tax_gets_arithmetic_without_ready_total(self):
        request = parse_request("Осциллограф 4 канала полоса 100 МГц", 7)
        result = analyze_offers(request, [observation("a", 10), observation("b", 20), observation("c", 30)])
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertIsNone(result["calculation"]["total"])
        estimate = result["calculation"]["provisional"]
        self.assertEqual(estimate["unit_price"], 20)
        self.assertEqual(estimate["total"], 140)
        self.assertEqual(estimate["offer_ids"], ["a", "b", "c"])
        self.assertTrue(any("НДС" in line for line in estimate["warnings"]))

    def test_incomplete_broad_request_stays_explicit_and_not_ready(self):
        result = analyze_offers(parse_request("Осциллограф", 2), [observation("a", 100)])
        self.assertIsNone(result["calculation"]["total"])
        self.assertEqual(result["calculation"]["provisional"]["total"], 200)
        self.assertTrue(any("Модели могут быть несопоставимы" in line for line in result["calculation"]["provisional"]["warnings"]))

    def test_unknown_sale_unit_or_unproved_conversion_has_no_total(self):
        for offer in [observation("a", 100, unit=None), observation("a", 100, unit="коробка")]:
            with self.subTest(unit=offer["unit"]):
                offer.pop("pack_size", None)
                result = analyze_offers(parse_request("Осциллограф 4 канала 100 МГц", 2), [offer])
                self.assertIsNone(result["calculation"]["provisional"]["total"])
        offer = observation("a", 100)
        del offer["evidence"]["unit"]
        self.assertIsNone(analyze_offers(parse_request("Осциллограф", 2), [offer])["calculation"]["provisional"]["total"])

    def test_mismatch_manual_exclusion_and_duplicates_never_return(self):
        request = parse_request("Осциллограф 4 канала 100 МГц", 2)
        a, b, c = observation("a", 100), observation("b", 10000), observation("c", 400)
        b["attributes"]["channels"] = 2
        duplicate = deepcopy(a)
        duplicate.update(id="mirror", url="https://mirror.example/product/")
        result = finalize(request, [a, b, c, duplicate], excluded_ids=["c"])
        self.assertEqual(result["calculation"]["provisional"]["offer_ids"], ["a"])
        self.assertEqual(result["calculation"]["provisional"]["total"], 200)
        reset = finalize(request, [a, b, c, duplicate])
        self.assertEqual(reset["calculation"]["provisional"]["total"], 500)
        b["attributes"]["channels"] = 4
        b["relevance"] = {"verdict": "mismatch", "reason": "Другая категория"}
        result = finalize(request, [a, b])
        self.assertEqual(result["calculation"]["provisional"]["offer_ids"], ["a"])

    def test_decimal_total_uses_unrounded_mean(self):
        result = analyze_offers(parse_request("Осциллограф", 7), [observation("a", 0.1), observation("b", 0.2), observation("c", 0.2)])
        self.assertAlmostEqual(result["calculation"]["provisional"]["unit_price"], 1 / 6, places=15)
        self.assertEqual(result["calculation"]["provisional"]["total"], 1.17)

    def test_input_not_mutated(self):
        request, offers = parse_request("Осциллограф", 2), [observation("a", 100)]
        before = deepcopy((request, offers))
        analyze_offers(request, offers)
        self.assertEqual((request, offers), before)

    def test_missing_delivery_terms_prevent_ready_but_do_not_erase_observed_arithmetic(self):
        request = parse_request("Осциллограф 4 канала 100 МГц", 2)
        request["conditions"] = [{"key": "delivery", "label": "Доставка", "text": "Доставка с разгрузкой включена в стоимость"}]
        offers = [observation(identifier, price, tax="included") for identifier, price in [("a", 100), ("b", 200), ("c", 300)]]
        for offer in offers:
            offer["evidence"]["tax"] = {"quote": "НДС включён", "url": offer["url"]}
        result = analyze_offers(request, offers)
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertIsNone(result["calculation"]["total"])
        self.assertEqual(result["calculation"]["provisional"]["total"], 400)
        self.assertTrue(any("условия ТЗ" in text for text in result["calculation"]["provisional"]["warnings"]))
        self.assertTrue(all(offer["condition_checks"][0]["status"] == "unconfirmed" for offer in result["offers"]))


class PaperToleranceTests(unittest.TestCase):
    def test_nominal_value_and_tolerance_are_not_confused(self):
        request = parse_request("Бумага А4. Количество листов в пачке: 500 (±2%). Масса листа бумаги площадью 1 м2: 80 ± 3 г/м2", 2500)
        requirements = {r["key"]: r for r in request["requirements"]}
        self.assertEqual(requirements["density_gsm"]["value"], [77, 83])
        self.assertEqual(requirements["sheets_per_pack"]["value"], [490, 510])
        self.assertEqual(requirements["density_gsm"]["op"], "between")
        self.assertEqual(request["missing_critical_fields"], [])
        self.assertTrue(_requirement_match(80, [77, 83], "between", "density_gsm"))
        self.assertFalse(_requirement_match(3, [77, 83], "between", "density_gsm"))

    def test_cyrillic_and_latin_paper_format_compare_equally(self):
        self.assertTrue(_requirement_match("а4", "A4", "eq", "format"))
        self.assertFalse(_requirement_match("А3", "A4", "eq", "format"))


class GenericProductIdentityTests(unittest.TestCase):
    def setUp(self):
        from quotelens.spec_ingest import prepare_specifications
        from quotelens.procurement import request_for_item
        path = Path(__file__).parent / "fixtures/specifications/printer.docx"
        parsed = prepare_specifications(files=[{"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}])
        self.request = request_for_item(parsed["items"][0])
        self.assertGreaterEqual(len(self.request["requirements"]), 20)

    def quoted_observation(self, title):
        offer = observation("identity-qa", 100000)
        offer["title"] = title
        offer["evidence"]["title"] = {"quote": title, "url": offer["url"]}
        return offer

    def test_unknown_specs_are_not_identity_evidence_for_an_oscilloscope_price(self):
        offer = self.quoted_observation("Осциллограф rigol-qa")
        self.assertFalse(positive_product_identity(self.request, offer))
        result = finalize(self.request, [offer])
        self.assertIsNone(result["calculation"]["total"])
        self.assertIsNone(result["calculation"]["provisional"]["total"])
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(result["offers"][0]["status"], "uncertain")

    def test_matching_named_printer_can_have_a_conditional_price(self):
        offer = self.quoted_observation("3D принтер CreatBot D600 PRO")
        offer["attributes"] = {"model": "D600 PRO"}
        offer["evidence"]["attributes.model"] = {"quote": "D600 PRO", "url": offer["url"]}
        self.assertTrue(positive_product_identity(self.request, offer))
        result = finalize(self.request, [offer])
        self.assertIsNone(result["calculation"]["total"])
        self.assertEqual(result["calculation"]["provisional"]["total"], 100000)
        self.assertTrue(any("характеристики" in warning for warning in result["calculation"]["provisional"]["warnings"]))

    def test_ambiguous_model_and_matching_accessory_remain_review_only(self):
        for title in ["Лабораторное оборудование", "3D принтер", "Кабель для CreatBot D600 PRO", "3D принтер D6000 PRO"]:
            with self.subTest(title=title):
                offer = self.quoted_observation(title)
                self.assertFalse(positive_product_identity(self.request, offer))
                self.assertIsNone(finalize(self.request, [offer])["calculation"]["provisional"]["total"])

    def test_quoted_model_is_required_and_assessment_cannot_override_wrong_named_identity(self):
        offer = observation("identity-qa", 100000)
        offer["title"] = "CreatBot D600 PRO"
        self.assertFalse(positive_product_identity(self.request, offer))
        offer["evidence"]["attributes.model"] = {"quote": "D600 PRO", "url": offer["url"]}
        offer["attributes"]["model"] = "D600 PRO"
        self.assertTrue(positive_product_identity(self.request, offer))
        offer["title"] = "Осциллограф rigol-qa"
        offer["relevance"] = {"verdict": "match", "reason": "Предположение модели"}
        self.assertFalse(positive_product_identity(self.request, offer))


class FractionalUnitPrecisionTests(unittest.TestCase):
    def test_confirmed_pack_to_sheet_prices_remain_nonzero_in_api(self):
        request, offers = sheet_purchase()
        result = finalize(request, offers)
        self.assertEqual(result["calculation"]["status"], "ready")
        self.assertEqual(result["calculation"]["unit_price"], 0.00246)
        self.assertEqual(result["calculation"]["mean"], 0.00246)
        self.assertEqual(result["calculation"]["total"], 2.46)
        self.assertEqual([offer["normalized_price"] for offer in result["offers"]], [0.00246] * 3)

    def test_conditional_normalized_prices_keep_the_same_precision(self):
        request, offers = sheet_purchase(tax="unknown")
        result = finalize(request, offers)
        self.assertIsNone(result["calculation"]["total"])
        estimate = result["calculation"]["provisional"]
        self.assertEqual(estimate["unit_price"], 0.00246)
        self.assertEqual(estimate["mean"], 0.00246)
        self.assertEqual(estimate["total"], 2.46)
        self.assertEqual(list(estimate["normalized_prices"].values()), [0.00246] * 3)

    def test_smaller_than_six_decimal_places_does_not_become_zero(self):
        request, offers = sheet_purchase(pack_size=500000000)
        result = finalize(request, offers)
        self.assertEqual(result["calculation"]["unit_price"], 0.00000000246)
        self.assertTrue(all(offer["normalized_price"] > 0 for offer in result["offers"]))


if __name__ == "__main__":
    unittest.main()

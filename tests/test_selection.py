"""Evidence and recalculation checks using explicitly fabricated QA fixtures."""
from copy import deepcopy
import hashlib
import unittest

from quotelens.engine import parse_request
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize, validate_candidate, validate_request


def qa_fixture():
    text = ("QA Чайник TestKettle-1. Мощность 2000 Вт. Объём 1.7 л. "
            "Цена 1 200,50 руб. за штуку с НДС. Продавец ООО QA Тест. ИНН 1234567890.")
    document = {"id": "qa-doc", "url": "https://qa.example/product/kettle", "title": "QA Чайник TestKettle-1",
                "text": text, "html": "<h1>QA Чайник TestKettle-1</h1>", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "retrieved_at": "2026-01-01T00:00:00Z", "method": "http"}
    candidate = {"title": document["title"], "price": 1200.5, "currency": "RUB", "unit": "шт", "tax": "included",
                 "supplier": "ООО QA Тест", "attributes": {"power_w": 2000, "volume_l": 1.7, "model": "TestKettle-1", "supplier_inn": "1234567890"},
                 "evidence": {"price": {"quote": "Цена 1 200,50 руб."}, "unit": {"quote": "за штуку"},
                              "tax": {"quote": "с НДС"}, "supplier": {"quote": "Продавец ООО QA Тест"},
                              "attributes.power_w": {"quote": "Мощность 2000 Вт"}, "attributes.volume_l": {"quote": "Объём 1.7 л"},
                              "attributes.model": {"quote": "TestKettle-1"}, "attributes.supplier_inn": {"quote": "ИНН 1234567890"}}}
    request = {"description": "Чайник, мощность 2000 Вт, объём 1.7 л, единица штука", "category": "kettle",
               "product": "Чайник", "unit": "шт", "quantity": 2, "missing_critical_fields": [],
               "requirements": [{"key": "power_w", "label": "Мощность", "op": "eq", "value": 2000, "unit": "Вт"},
                                {"key": "volume_l", "label": "Объём", "op": "eq", "value": 1.7, "unit": "л"}]}
    return request, candidate, document


class RequestValidationTests(unittest.TestCase):
    def test_known_category_and_missing_critical_fields_are_authoritative(self):
        base = parse_request("Осциллограф 4 канала")
        proposal = {"category": "kettle", "product": "Осциллограф", "unit": "шт", "requirements": [], "missing_critical_fields": []}
        self.assertEqual(validate_request(base, proposal), base)

    def test_unknown_category_without_model_stays_uncertain(self):
        base = parse_request("Ноутбук с 16 ГБ RAM")
        self.assertTrue(validate_request(base, None)["missing_critical_fields"])

    def test_dynamic_requirement_requires_exact_quote_value_operator_and_unit(self):
        base = parse_request("Ноутбук с RAM не менее 16 ГБ, единица штука", 3)
        proposal = {"category": "laptop", "product": "Ноутбук", "unit": "шт", "requirements": [
            {"key": "ram_gb", "label": "RAM", "value": 16, "op": "gte", "unit": "ГБ", "quote": "RAM не менее 16 ГБ"}],
            "search_terms": ["ноутбук 16 ГБ купить"]}
        result = validate_request(base, proposal)
        self.assertEqual(result["category"], "laptop")
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(result["missing_critical_fields"], [])
        self.assertEqual(result["requirements"][0]["quote"], "RAM не менее 16 ГБ")
        for key, value in [("quote", "RAM не менее 32 ГБ"), ("value", 32), ("op", "eq"), ("unit", "МГц")]:
            bad = deepcopy(proposal)
            bad["requirements"][0][key] = value
            self.assertTrue(validate_request(base, bad)["missing_critical_fields"], key)

    def test_dynamic_request_does_not_guess_a_missing_unit_or_drop_rejected_constraints(self):
        base = parse_request("Ноутбук RAM 16 ГБ, SSD 512 ГБ")
        proposal = {"category": "laptop", "product": "Ноутбук", "unit": "шт", "requirements": [
            {"key": "ram_gb", "label": "RAM", "value": 16, "quote": "RAM 16 ГБ"},
            {"key": "ssd_gb", "label": "SSD", "value": 1024, "quote": "SSD 512 ГБ"}]}
        result = validate_request(base, proposal)
        self.assertIn("явная единица потребности", result["missing_critical_fields"])
        self.assertTrue(any("SSD" in value for value in result["missing_critical_fields"]))

    def test_omitted_explicit_ssd_blocks_readiness_and_cannot_hide_in_a_broad_quote(self):
        base = parse_request("Ноутбук RAM 16 ГБ, SSD 512 ГБ, единица штука", 2)
        proposal = {"category": "laptop", "product": "Ноутбук", "unit": "шт", "requirements": [
            {"key": "ram_gb", "label": "RAM", "value": 16, "quote": "RAM 16 ГБ"}]}
        request = validate_request(base, proposal)
        self.assertTrue(any("SSD 512" in value for value in request["missing_critical_fields"]))
        proposal["requirements"][0]["quote"] = base["description"]
        self.assertTrue(validate_request(base, proposal)["missing_critical_fields"])
        proposal["requirements"][0]["quote"] = "RAM 16 ГБ"
        proposal["requirements"].append({"key": "ssd_gb", "label": "SSD", "value": 512, "quote": "SSD 512 ГБ"})
        complete = validate_request(base, proposal)
        self.assertEqual(complete["missing_critical_fields"], [])
        self.assertEqual(len(complete["requirements"]), 2)

    def test_same_number_in_an_uncovered_spec_is_not_silently_covered_by_ram(self):
        base = parse_request("Ноутбук RAM 16 ГБ, SSD 16 ГБ, единица штука", 2)
        proposal = {"category": "laptop", "product": "Ноутбук", "unit": "шт", "requirements": [
            {"key": "ram_gb", "label": "RAM", "value": 16, "quote": "RAM 16 ГБ"}]}
        self.assertTrue(any("SSD" in item for item in validate_request(base, proposal)["missing_critical_fields"]))


class CandidateValidationTests(unittest.TestCase):
    def test_souz_category_heading_cannot_inherit_first_product_tile_price(self):
        request = parse_request("Осциллограф", 1)
        document = {"url": "https://qa.example/catalog/oscilloscopes/", "title": "Осциллографы", "primary_title": "Осциллографы",
                    "text": "Осциллографы Фильтр Сортировать по цене. 34 865 ₽ АКИП-4115/2А - цифровой осциллограф.",
                    "html": '<h1>Осциллографы</h1><a href="/product/akip-4115-2a/">АКИП-4115/2А - цифровой осциллограф</a>'}
        candidate = {"title": "Осциллографы", "price": 34865, "evidence": {"price": {"quote": "34 865 ₽"}}}
        self.assertIsNone(validate_candidate(candidate, document, request))

    def test_radio_category_with_false_product_schema_cannot_be_an_uncertain_offer(self):
        document = {"url": "https://qa.example/catalog/oscilloscopes/", "title": "Осциллографы", "primary_title": "Осциллографы",
                    "text": "Осциллографы 1-2 из 2 товара Сортировка: USB-осциллограф DSO-6102BE. WFS210, осциллограф цифровой.",
                    "html": '<h1>Осциллографы</h1><script type="application/ld+json">{"@type":"Product","name":"Осциллографы","offers":{"lowPrice":24400}}</script>'}
        self.assertIsNone(validate_candidate({"title": "Осциллографы", "price": None}, document, parse_request("Осциллограф")))

    def test_true_product_primary_h1_and_catalog_path_keep_price_and_prior_guards(self):
        request, candidate, document = qa_fixture()
        document.update(url="https://qa.example/catalog/kettles/testkettle-1/", primary_title=document["title"], title="Каталог чайников QA")
        document["text"] += " Похожие товары TestKettle-2. Цена 9 000 руб. за штуку."
        document["html"] += '<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"Product","name":"TestKettle-2"}]}</script>'
        offer = validate_candidate(candidate, document, request)
        self.assertEqual(offer["price"], 1200.5)
        self.assertEqual(offer["unit"], "шт")
        self.assertEqual(offer["attributes"]["model"], "TestKettle-1")
        candidate["price"] = 2000
        candidate["evidence"]["price"]["quote"] = "Мощность 2000 Вт"
        self.assertIsNone(validate_candidate(candidate, document, request)["price"])

    def test_incomplete_true_product_is_retained_without_an_invented_price(self):
        request, candidate, document = qa_fixture()
        candidate.update(price=None)
        offer = validate_candidate(candidate, document, request)
        self.assertIsNotNone(offer)
        self.assertIsNone(offer["price"])
        self.assertEqual(finalize(request, [offer])["offers"][0]["status"], "uncertain")

    def test_valid_quote_fields_keep_document_hash_and_stable_id(self):
        request, candidate, document = qa_fixture()
        before = deepcopy((candidate, document, request))
        offer = validate_candidate(candidate, document, request)
        self.assertEqual(offer["price"], 1200.5)
        self.assertEqual(offer["currency"], "RUB")
        self.assertEqual(offer["attributes"]["power_w"], 2000)
        self.assertEqual(offer["document_id"], "qa-doc")
        self.assertEqual(offer["source_hash"], document["sha256"])
        self.assertEqual(offer["evidence"]["price"]["document_id"], "qa-doc")
        self.assertEqual(validate_candidate({**candidate, "id": "invented-id"}, document, request)["id"], offer["id"])
        self.assertEqual((candidate, document, request), before)
        self.assertEqual(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"], 2401)

    def test_unbacked_price_tax_specs_identity_are_removed_and_stay_uncertain(self):
        request, candidate, document = qa_fixture()
        candidate.update(price=900, tax="excluded")
        candidate["attributes"]["power_w"] = 9000
        candidate["attributes"]["supplier_inn"] = "9876543210"
        offer = validate_candidate(candidate, document, request)
        self.assertIsNone(offer["price"])
        self.assertEqual(offer["tax"], "unknown")
        self.assertNotIn("power_w", offer["attributes"])
        self.assertNotIn("supplier_inn", offer["attributes"])
        result = finalize(request, [offer], {"min_suppliers": 1})
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertIsNone(result["calculation"]["total"])

    def test_quotes_allow_whitespace_normalization_but_not_changed_words(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("Мощность 2000 Вт", "Мощность\n\t2000\u00a0Вт")
        self.assertEqual(validate_candidate(candidate, document, request)["attributes"]["power_w"], 2000)
        candidate["evidence"]["attributes.power_w"]["quote"] = "Power 2000 W"
        self.assertNotIn("power_w", validate_candidate(candidate, document, request)["attributes"])

    def test_price_number_cannot_be_reused_as_a_different_attribute(self):
        request, candidate, document = qa_fixture()
        candidate["attributes"]["power_w"] = 1200.5
        candidate["evidence"]["attributes.power_w"]["quote"] = "Цена 1 200,50 руб."
        self.assertNotIn("power_w", validate_candidate(candidate, document, request)["attributes"])

    def test_stock_count_does_not_prove_price_sale_unit(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("за штуку", "на складе 10 шт.")
        candidate["evidence"]["unit"]["quote"] = "на складе 10 шт."
        offer = validate_candidate(candidate, document, request)
        self.assertIsNone(offer["unit"])
        result = finalize(request, [offer], {"min_suppliers": 1})
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertIsNone(result["calculation"]["total"])

    def test_explicit_compact_sale_units_and_thin_space_money_remain_accepted(self):
        for amount, displayed in [(57300, "57 300"), (69065, "69\u2009065"), (69065, "69\u202f065")]:
            with self.subTest(displayed=displayed):
                request, candidate, document = qa_fixture()
                document["text"] = document["text"].replace("1 200,50 руб. за штуку", displayed + " руб. за 1шт")
                candidate.update(price=amount)
                candidate["evidence"]["price"]["quote"] = displayed + " руб. за 1шт"
                candidate["evidence"]["unit"]["quote"] = "за 1шт"
                offer = validate_candidate(candidate, document, request)
                self.assertEqual(offer["price"], amount)
                self.assertEqual(offer["unit"], "шт")
                self.assertEqual(offer["evidence"]["price"]["quote"], displayed + " руб. за 1шт")
                self.assertEqual(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"], amount * 2)

    def test_non_price_number_in_a_quote_with_a_real_price_cannot_be_the_price(self):
        request, candidate, document = qa_fixture()
        candidate["price"] = 2000
        candidate["evidence"]["price"]["quote"] = "Мощность 2000 Вт. Объём 1.7 л. Цена 1 200,50 руб."
        offer = validate_candidate(candidate, document, request)
        self.assertIsNone(offer["price"])
        self.assertIsNone(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"])

    def test_short_price_quote_cannot_hide_another_model_in_its_source_block(self):
        for quote in ["Цена для TestKettle-2 1 200,50 руб.", "1 200,50 руб."]:
            with self.subTest(quote=quote):
                request, candidate, document = qa_fixture()
                document["text"] = document["text"].replace("Цена 1 200,50 руб.", "Цена для TestKettle-2 1 200,50 руб. Цена TestKettle-1 9 000 руб.")
                candidate["evidence"]["price"]["quote"] = quote
                offer = validate_candidate(candidate, document, request)
                self.assertIsNone(offer["price"])

    def test_matching_model_price_is_a_positive_anchor(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("Цена 1 200,50 руб.", "Цена TestKettle-1 1 200,50 руб.")
        candidate["evidence"]["price"]["quote"] = "1 200,50 руб."
        offer = validate_candidate(candidate, document, request)
        self.assertEqual(offer["price"], 1200.5)
        self.assertEqual(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"], 2401)

    def test_quantity_tier_cannot_hide_in_short_quote_but_applicable_tier_works(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("Цена 1 200,50 руб. за штуку с НДС.",
            "При заказе от 100 штук: Цена 1 200,50 руб. за штуку с НДС. При заказе до 99 штук: Цена 9 000 руб. за штуку с НДС.")
        candidate["evidence"]["price"]["quote"] = "Цена 1 200,50 руб."
        self.assertIsNone(validate_candidate(candidate, document, request)["price"])
        request["quantity"] = 100
        offer = validate_candidate(candidate, document, request)
        self.assertEqual(offer["price"], 1200.5)
        self.assertEqual(offer["unit"], "шт")
        self.assertEqual(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"], 120050)
        request["quantity"] = 2
        candidate["price"] = 9000
        candidate["evidence"]["price"]["quote"] = "Цена 9 000 руб."
        self.assertEqual(validate_candidate(candidate, document, request)["price"], 9000)

    def test_category_minimum_price_and_recommended_product_are_not_offers(self):
        request, candidate, document = qa_fixture()
        listing = deepcopy(document)
        listing["title"] = "Каталог чайников"
        self.assertIsNone(validate_candidate(candidate, listing, request))
        candidate["evidence"]["price"]["quote"] = "Цена от 1 200,50 руб."
        document["text"] = document["text"].replace("Цена 1 200,50 руб.", "Цена от 1 200,50 руб.")
        self.assertIsNone(validate_candidate(candidate, document, request)["price"])
        request, candidate, document = qa_fixture()
        document["text"] = "QA Другой чайник. Цена 5000 руб. Похожие товары " + document["text"]
        document["title"] = "QA Другой чайник"
        self.assertIsNone(validate_candidate(candidate, document, request))

    def test_related_product_cannot_supply_a_primary_products_attribute(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("Мощность 2000 Вт.", "") + " Похожие товары QA Модель B. Мощность 2000 Вт."
        offer = validate_candidate(candidate, document, request)
        self.assertNotIn("power_w", offer["attributes"])

    def test_tax_exclusion_never_turns_into_inclusion(self):
        request, candidate, document = qa_fixture()
        document["text"] = document["text"].replace("с НДС", "без НДС")
        candidate["evidence"]["tax"]["quote"] = "без НДС"
        self.assertEqual(validate_candidate(candidate, document, request)["tax"], "unknown")

    def test_seller_group_and_image_cannot_be_invented(self):
        request, candidate, document = qa_fixture()
        candidate["attributes"]["supplier_group"] = "ООО QA Тест"
        candidate["evidence"]["attributes.supplier_group"] = {"quote": "ООО QA Тест"}
        candidate["image_url"] = "https://qa.example/invented-image.jpg"
        candidate["evidence"]["image_url"] = {"quote": candidate["title"]}
        offer = validate_candidate(candidate, document, request)
        self.assertNotIn("supplier_group", offer["attributes"])
        self.assertNotIn("image_url", offer)
        document["html"] += '<img src="https://qa.example/invented-image.jpg">'
        self.assertEqual(validate_candidate(candidate, document, request)["image_url"], candidate["image_url"])

    def test_model_relevance_is_retained_as_an_assessment_not_source_evidence(self):
        request, candidate, document = qa_fixture()
        candidate["relevance"] = {"verdict": "mismatch", "reason": "QA: иной вид товара"}
        offer = validate_candidate(candidate, document, request)
        self.assertEqual(offer["relevance"]["basis"], "model_assessment")
        result = finalize(request, [offer], {"min_suppliers": 1})
        self.assertEqual(result["offers"][0]["status"], "excluded")
        self.assertIn("Оценка модели", result["offers"][0]["reasons"][0])
        self.assertEqual(result["calculation"]["n"], 0)


class FinalizationTests(unittest.TestCase):
    def fixture(self):
        data = load_scenario("synthetic-comparable")
        return data["request"], data["offers"][:3]

    def test_manual_exclusion_recalculates_before_mean_and_reset_replays_original(self):
        request, offers = self.fixture()
        before = deepcopy((request, offers))
        initial = finalize(request, offers)
        excluded = finalize(request, offers, excluded_ids=[offers[0]["id"]])
        self.assertEqual(excluded["calculation"]["n"], 2)
        self.assertIsNone(excluded["calculation"]["total"])
        self.assertTrue(excluded["offers"][0]["manual_excluded"])
        self.assertEqual(excluded["offers"][0]["status"], "excluded")
        reset = finalize(**excluded["base_result"], excluded_ids=[])
        self.assertEqual(reset["calculation"], initial["calculation"])
        self.assertEqual((request, offers), before)
        self.assertEqual(excluded["base_result"]["offers"], offers)

    def test_excluded_first_duplicate_does_not_suppress_the_remaining_original(self):
        request, offers = self.fixture()
        duplicate = deepcopy(offers[0])
        duplicate["id"] = "qa-duplicate"
        initial = finalize(request, [offers[0], duplicate] + offers[1:])
        self.assertEqual(initial["offers"][1]["status"], "excluded")
        reviewed = finalize(request, [offers[0], duplicate] + offers[1:], excluded_ids=[offers[0]["id"]])
        self.assertEqual(reviewed["offers"][1]["status"], "included")
        self.assertEqual(reviewed["calculation"]["n"], 3)
        self.assertEqual(reviewed["metrics"]["duplicates"], 0)

    def test_reset_cannot_force_unknown_evidence_into_calculation(self):
        request, offers = self.fixture()
        offers[0]["tax"] = "unknown"
        reviewed = finalize(request, offers, excluded_ids=[offers[0]["id"]])
        reset = finalize(**reviewed["base_result"], excluded_ids=[])
        self.assertEqual(reset["offers"][0]["status"], "uncertain")
        self.assertEqual(reset["calculation"]["n"], 2)

    def test_model_match_cannot_override_missing_tax_and_uncertain_never_enters_mean(self):
        request, offers = self.fixture()
        offers[0].update(tax="unknown", relevance={"verdict": "match", "reason": "QA functional match"})
        offers[1]["relevance"] = {"verdict": "uncertain", "reason": "QA service scope unknown"}
        result = finalize(request, offers)
        self.assertEqual(result["calculation"]["n"], 1)
        self.assertEqual([offer["status"] for offer in result["offers"]], ["uncertain", "uncertain", "included"])

    def test_clustering_and_metrics_include_explicit_evidence_denominator(self):
        request, offers = self.fixture()
        offers[1]["supplier"] = offers[0]["supplier"]
        result = finalize(request, offers)
        self.assertEqual(len(result["groups"]["sellers"]), 2)
        self.assertEqual(result["metrics"]["offers_total"], 3)
        self.assertEqual(result["metrics"]["evidence_fields_required"], 18)
        self.assertLessEqual(result["metrics"]["evidence_fields_verified"], 18)
        self.assertIsNone(finalize(request, [])["metrics"]["evidence_coverage"])


if __name__ == "__main__":
    unittest.main()

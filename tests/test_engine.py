"""Public behavioural checks for the deterministic engine (stdlib unittest)."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from quotelens.engine import analyze_offers, parse_request
from quotelens.scenarios import list_scenarios, load_scenario


def fixture():
    data = load_scenario("synthetic-comparable")
    return data["request"], data["offers"][:3]


class RequestTests(unittest.TestCase):
    def test_common_oscilloscope_typo_keeps_description_and_missing_specs(self):
        request = parse_request("осцилограф")
        self.assertEqual(request["description"], "осцилограф")
        self.assertEqual(request["category"], "oscilloscope")
        self.assertEqual(request["requirements"], [])
        self.assertEqual(request["missing_critical_fields"], ["channels", "bandwidth_mhz"])
        self.assertEqual(request["search_terms"], ["осциллограф"])

    def test_extracts_explicit_hard_constraints(self):
        request = parse_request("Осциллограф 4 канала, полоса не менее 100 МГц", 7)
        requirements = {x["key"]: x for x in request["requirements"]}
        self.assertEqual(request["category"], "oscilloscope")
        self.assertEqual(request["quantity"], 7)
        self.assertEqual(requirements["channels"]["value"], 4)
        self.assertEqual(requirements["channels"]["op"], "eq")
        self.assertEqual(requirements["bandwidth_mhz"]["op"], "gte")

    def test_does_not_fill_missing_hard_constraints(self):
        request = parse_request("Нужен осциллограф")
        self.assertEqual(request["requirements"], [])
        self.assertIn("channels", request["missing_critical_fields"])
        _, offers = fixture()
        result = analyze_offers(request, offers)
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertTrue(all(x["status"] == "uncertain" for x in result["offers"]))

    def test_sampling_frequency_is_not_bandwidth_regression(self):
        request = parse_request("Цифровой осциллограф 4 канала, частота дискретизации 1 ГГц, полоса пропускания не менее 100 МГц")
        requirements = {x["key"]: x for x in request["requirements"]}
        self.assertEqual(requirements["bandwidth_mhz"]["value"], 100)
        self.assertEqual(requirements["bandwidth_mhz"]["op"], "gte")

    def test_only_sampling_frequency_does_not_supply_missing_bandwidth(self):
        request = parse_request("Осциллограф 4 канала, частота дискретизации 1 ГГц")
        self.assertIn("bandwidth_mhz", request["missing_critical_fields"])
        self.assertFalse(any(x["key"] == "bandwidth_mhz" for x in request["requirements"]))

    def test_ambiguous_frequency_alternatives_are_not_guessed(self):
        for description in ["Осциллограф 4 канала, 100 МГц или 200 МГц",
                            "Осциллограф 4 канала, полоса 100 МГц или 200 МГц",
                            "Осциллограф 4 канала, дискретизация 1 ГГц и полоса 100 МГц или 200 МГц",
                            "Осциллограф 4 канала, полоса 100 МГц, полоса 200 МГц"]:
            with self.subTest(description=description):
                request = parse_request(description)
                self.assertIn("bandwidth_mhz", request["missing_critical_fields"])

    def test_single_unlabelled_frequency_remains_supported_shorthand(self):
        request = parse_request("Осциллограф 4 канала, не менее 0.1 ГГц")
        bandwidth = next(x for x in request["requirements"] if x["key"] == "bandwidth_mhz")
        self.assertEqual(bandwidth["value"], 100)
        self.assertEqual(bandwidth["op"], "gte")

    def test_paper_and_basic_service_are_distinct(self):
        paper = parse_request("Бумага А4, 80 г/м², пачка 500 листов", 40)
        self.assertEqual(paper["category"], "paper")
        self.assertEqual({x["key"]: x["value"] for x in paper["requirements"]},
                         {"format": "A4", "density_gsm": 80, "sheets_per_pack": 500})
        service = parse_request("Уборка помещений за м²", 100)
        self.assertEqual(service["category"], "service")
        self.assertEqual(service["unit"], "м²")

    def test_unrelated_description_is_not_an_oscilloscope(self):
        request = parse_request("Ремонт обуви")
        self.assertEqual(request["category"], "other")
        self.assertEqual(request["product"], "Ремонт обуви")
        self.assertTrue(request["missing_critical_fields"])

    def test_invalid_quantity_is_not_replaced_with_one(self):
        for value in [0, -1, float("nan"), float("inf"), True, "неизвестно"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_request("Осциллограф 4 канала 100 МГц", value)


class ComparabilityTests(unittest.TestCase):
    def test_hard_specs_dedupe_model_conflict_and_listing(self):
        data = load_scenario("synthetic-comparable")
        result = analyze_offers(data["request"], data["offers"])
        statuses = {x["id"]: x for x in result["offers"]}
        self.assertEqual(result["calculation"]["n"], 3)
        for suffix in ["two-channels", "low-band", "a-mirror", "mismatch", "listing"]:
            self.assertEqual(statuses["synthetic-" + suffix]["status"], "excluded")
            self.assertTrue(statuses["synthetic-" + suffix]["reasons"])
        self.assertEqual(statuses["synthetic-a-mirror"]["duplicate_of"], "synthetic-a")

    def test_unknown_required_attribute_is_uncertain_not_zero(self):
        request, offers = fixture()
        offers[0]["attributes"]["channels"] = None
        result = analyze_offers(request, offers)
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertIsNone(result["offers"][0]["attributes"]["channels"])
        self.assertEqual(result["calculation"]["n"], 2)
        self.assertIsNone(result["calculation"]["total"])

    def test_minimum_is_a_hard_gate_not_a_scoring_preference(self):
        request, offers = fixture()
        offers[0]["attributes"]["bandwidth_mhz"] = 99.999
        offers[1]["attributes"]["bandwidth_mhz"] = 100
        offers[2]["attributes"]["bandwidth_mhz"] = 200
        result = analyze_offers(request, offers)
        self.assertEqual([x["status"] for x in result["offers"]], ["excluded", "included", "included"])

    def test_source_evidence_missing_prevents_acceptance(self):
        request, offers = fixture()
        del offers[0]["evidence"]["attributes.bandwidth_mhz"]
        del offers[1]["evidence"]["price"]
        result = analyze_offers(request, offers)
        self.assertEqual([x["status"] for x in result["offers"]], ["uncertain", "uncertain", "included"])

    def test_unknown_tax_and_excluded_tax_are_not_assumed_included(self):
        request, offers = fixture()
        offers[0]["tax"] = "unknown"
        offers[1]["tax"] = "excluded"
        result = analyze_offers(request, offers)
        self.assertEqual([x["status"] for x in result["offers"]], ["uncertain", "uncertain", "included"])
        self.assertEqual(result["calculation"]["status"], "insufficient_data")

    def test_foreign_currency_is_not_silently_converted(self):
        request, offers = fixture()
        offers[0]["currency"] = "USD"
        result = analyze_offers(request, offers)
        self.assertEqual(result["offers"][0]["status"], "uncertain")

    def test_bad_prices_are_not_observations(self):
        for bad in [None, 0, -1, float("nan"), "по запросу"]:
            request, offers = fixture()
            offers[0]["price"] = bad
            result = analyze_offers(request, offers)
            self.assertEqual(result["offers"][0]["status"], "uncertain")
            self.assertIsNone(result["offers"][0]["normalized_price"])

    def test_different_domains_do_not_multiply_one_merchant(self):
        request, offers = fixture()
        offers[0]["supplier"] = "ООО «Один продавец»"
        offers[1]["supplier"] = "Один продавец"
        offers[2]["supplier"] = "Другой продавец"
        result = analyze_offers(request, offers)
        self.assertEqual(result["calculation"]["n"], 3)  # Three distinct models remain offers.
        self.assertEqual(result["calculation"]["independent_suppliers"], 2)
        self.assertEqual(result["calculation"]["status"], "insufficient_data")

    def test_same_model_and_seller_deduplicate_across_domains(self):
        request, offers = fixture()
        offers[1]["supplier"] = offers[0]["supplier"]
        offers[1]["attributes"]["model"] = offers[0]["attributes"]["model"]
        result = analyze_offers(request, offers)
        self.assertEqual(result["offers"][1]["status"], "excluded")
        self.assertEqual(result["offers"][1]["duplicate_of"], offers[0]["id"])

    def test_unusable_record_does_not_hide_a_later_valid_record(self):
        request, offers = fixture()
        first = deepcopy(offers[0])
        first["id"] = "incomplete-first"
        first["price"] = None
        result = analyze_offers(request, [first] + offers)
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertEqual(result["offers"][1]["status"], "included")

    def test_known_affiliate_group_is_one_supplier(self):
        request, offers = fixture()
        for offer in offers:
            offer["attributes"]["supplier_group"] = "общая подтверждённая группа"
        result = analyze_offers(request, offers)
        self.assertEqual(result["calculation"]["independent_suppliers"], 1)
        self.assertEqual(result["calculation"]["status"], "insufficient_data")

    def test_source_model_mismatch_is_rejected(self):
        request, offers = fixture()
        offers[0]["attributes"]["model"] = "АКИП-4131/1В"
        offers[0]["evidence"]["attributes.model"]["quote"] = "ADS-2031"
        result = analyze_offers(request, offers)
        self.assertEqual(result["offers"][0]["status"], "excluded")

    def test_inputs_are_not_mutated(self):
        request, offers = fixture()
        before = deepcopy((request, offers))
        analyze_offers(request, offers)
        self.assertEqual((request, offers), before)


class CalculationTests(unittest.TestCase):
    def test_hand_computed_mean_sample_deviation_and_cv(self):
        request, offers = fixture()
        request["quantity"] = 5
        for offer, price in zip(offers, [10, 20, 30]):
            offer["price"] = price
        calculation = analyze_offers(request, offers)["calculation"]
        self.assertEqual(calculation["unit_price"], 20)
        self.assertEqual(calculation["total"], 100)
        self.assertEqual(calculation["stddev"], 10)  # Sample, not population.
        self.assertEqual(calculation["cv"], 50)  # Percent.

    def test_one_offer_cv_is_undefined_even_if_profile_allows_one(self):
        request, offers = fixture()
        result = analyze_offers(request, offers[:1], {"min_suppliers": 1})
        self.assertEqual(result["calculation"]["status"], "ready")
        self.assertIsNone(result["calculation"]["stddev"])
        self.assertIsNone(result["calculation"]["cv"])

    def test_insufficient_offers_keep_evidence_not_a_valid_total(self):
        data = load_scenario("synthetic-one")
        result = analyze_offers(data["request"], data["offers"])
        self.assertEqual(result["calculation"]["n"], 1)
        self.assertEqual(result["calculation"]["mean"], 100)
        self.assertIsNone(result["calculation"]["unit_price"])
        self.assertIsNone(result["calculation"]["total"])

    def test_packs_are_normalized_with_explicit_counts(self):
        data = load_scenario("synthetic-paper")
        result = analyze_offers(data["request"], data["offers"])
        included = [x for x in result["offers"] if x["status"] == "included"]
        self.assertEqual([x["normalized_price"] for x in included], [300, 300, 320])
        self.assertEqual(result["calculation"]["total"], 30666.67)
        self.assertEqual(result["offers"][-1]["status"], "uncertain")

    def test_pack_coefficient_requires_evidence(self):
        data = load_scenario("synthetic-paper")
        del data["offers"][1]["evidence"]["pack_size"]
        result = analyze_offers(data["request"], data["offers"])
        self.assertEqual(result["offers"][1]["status"], "uncertain")

    def test_incompatible_units_do_not_make_a_total(self):
        request, offers = fixture()
        offers[0]["unit"] = "кг"
        result = analyze_offers(request, offers)
        self.assertEqual(result["offers"][0]["status"], "uncertain")

    def test_fractional_service_quantity_and_decimal_rounding(self):
        data = load_scenario("synthetic-service")
        data["request"]["quantity"] = 2.5
        for offer in data["offers"]:
            offer["price"] = "0.10"
        result = analyze_offers(data["request"], data["offers"])
        self.assertEqual(result["calculation"]["total"], 0.25)

    def test_sensitivity_never_fabricates_sufficient_reduced_sample(self):
        request, offers = fixture()
        result = analyze_offers(request, offers)
        self.assertEqual(len(result["sensitivity"]), 3)
        self.assertTrue(all(x["status"] == "insufficient_data" and x["total"] is None and x["delta_percent"] is None for x in result["sensitivity"]))

    def test_iqr_diagnoses_but_never_discards_a_high_price(self):
        data = load_scenario("synthetic-outlier")
        result = analyze_offers(data["request"], data["offers"])
        self.assertEqual(result["calculation"]["n"], 7)
        self.assertEqual(result["offers"][-1]["status"], "included")
        self.assertTrue(any("IQR:" in x and "synthetic-iqr-7" in x for x in result["warnings"]))

    def test_delivery_unknown_is_not_zero_or_an_added_fee(self):
        request, offers = fixture()
        result = analyze_offers(request, offers)
        self.assertTrue(all(x["delivery"] is None for x in result["offers"]))
        self.assertTrue(any("неизвестны" in x for x in result["warnings"]))

    def test_no_offers_has_no_average_or_total(self):
        request, _ = fixture()
        calculation = analyze_offers(request, [])["calculation"]
        self.assertEqual(calculation["n"], 0)
        self.assertIsNone(calculation["mean"])
        self.assertIsNone(calculation["total"])


class ScenarioEvidenceTests(unittest.TestCase):
    def test_real_snapshot_has_real_sources_dates_and_exact_model_filter(self):
        data = load_scenario("real-oscilloscopes")
        self.assertEqual(data["kind"], "real_snapshot")
        self.assertEqual(len(data["offers"]), 4)
        self.assertTrue(all(x["provenance"] == "snapshot" and "example.invalid" not in x["url"] for x in data["offers"]))
        result = analyze_offers(data["request"], data["offers"])
        self.assertEqual(result["calculation"]["n"], 3)
        self.assertEqual(result["calculation"]["unit_price"], 69065)
        self.assertEqual(result["offers"][-1]["status"], "excluded")
        self.assertTrue(all(x.get("unit_inference") for x in data["offers"]))
        self.assertTrue(any("Единица цены" in warning and "не явное указание" in warning for warning in result["warnings"]))

    def test_real_quotes_have_compact_capture_provenance(self):
        path = Path(__file__).resolve().parent.parent / "data" / "captured-pages.json"
        captures = json.loads(path.read_text(encoding="utf-8"))
        lookup = {x["url"]: x for x in captures}
        for offer in load_scenario("real-oscilloscopes")["offers"]:
            captured = lookup[offer["url"]]
            self.assertEqual(captured["retrieved_at"], offer["retrieved_at"])
            self.assertEqual(len(captured["sha256"]), 64)
            self.assertIn(offer["evidence"]["price"]["quote"], captured["evidence_excerpts"])

    def test_synthetic_scenarios_are_visibly_labelled_and_isolated(self):
        for item in list_scenarios():
            if item["kind"] == "synthetic":
                self.assertIn("Синтетика", item["title"])
                data = load_scenario(item["id"])
                self.assertIsNone(data["captured_at"])
                self.assertTrue(all(x["provenance"] == "synthetic" and "example.invalid" in x["url"] for x in data["offers"]))
                result = analyze_offers(data["request"], data["offers"])
                json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_loading_does_not_share_mutable_scenario_state(self):
        first = load_scenario("synthetic-one")
        first["offers"][0]["price"] = -100
        self.assertEqual(load_scenario("synthetic-one")["offers"][0]["price"], 100)


if __name__ == "__main__":
    unittest.main()

"""Local evidence fixtures exercise the product profile and narrow decisions."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from quotelens.agent import BudgetStop, Research
from quotelens.engine import analyze_offers, parse_request
from quotelens.extraction import extract_product_offer
from quotelens.review import replay_line
from quotelens.procurement import aggregate_results, normalize_search_policy
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import Store
from quotelens.targeting import annotate_target
from test_supplier import CONTACTS, captured


def comparable():
    sample = load_scenario("synthetic-comparable")
    return copy.deepcopy(sample["request"]), copy.deepcopy(sample["offers"][:3])


def accept():
    return {"reason": "Принимаю указанное отклонение", "revision": 2, "at": "2026-11-13T15:00:00Z"}


class TargetProfileTests(unittest.TestCase):
    def test_target_defaults_are_fixed_three_and_ten_minutes(self):
        self.assertEqual(normalize_search_policy({"mode": "target"}), {"mode": "target", "max_offers": 3, "max_seconds": 600})
        self.assertEqual(normalize_search_policy({"mode": "target", "max_seconds": 3600})["max_seconds"], 3600)
        self.assertEqual(normalize_search_policy({"mode": "offers"})["max_offers"], 100)
        self.assertEqual(normalize_search_policy({"mode": "time"})["max_seconds"], 3600)
        with self.assertRaises(ValueError):
            normalize_search_policy({"mode": "target", "max_offers": 4})

    def test_all_comparable_values_remain_in_cv_instead_of_selecting_close_three(self):
        request, offers = comparable()
        for offer in offers:
            offer["price"] = 100
        extra = copy.deepcopy(offers[-1])
        extra.update(id="outlier", supplier="Fourth supplier", url="https://fourth.test/product", price=1000)
        extra["attributes"].pop("supplier_inn", None)
        extra["evidence"].pop("attributes.supplier_inn", None)
        offers.append(extra)
        result = annotate_target(finalize(request, offers), enforce=True)
        group = result["target_group"]
        self.assertEqual(group["count"], 4)
        self.assertEqual(len(group["offer_ids"]), 4)
        self.assertEqual([x["price"] for x in group["selected_values"]], [100, 100, 100, 1000])
        self.assertGreater(group["cv"], 33)
        self.assertFalse(group["ready"])
        self.assertIsNone(result["calculation"]["total"])
        self.assertEqual(result["calculation"]["candidate_cv"], group["cv"])
        self.assertEqual(finalize(request, offers)["calculation"]["status"], "ready")

    def test_cv_threshold_is_inclusive_and_uses_sample_deviation(self):
        request, offers = comparable()
        for offer, price in zip(offers, [67, 100, 133]):
            offer["price"] = price
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertEqual(result["target_group"]["cv"], 33)
        self.assertTrue(result["target_group"]["ready"])
        offers[-1]["price"] = 134
        self.assertFalse(annotate_target(finalize(request, offers), enforce=True)["target_group"]["ready"])

    def test_missing_proof_conditions_duplicates_and_price_unknown_do_not_count(self):
        request, offers = comparable()
        offers[1]["evidence"].pop("attributes.channels", None)
        offers[2]["price"] = None
        duplicate = copy.deepcopy(offers[0]); duplicate["id"] = "duplicate"
        result = annotate_target(finalize(request, offers + [duplicate]), enforce=True)
        self.assertEqual(result["target_group"]["count"], 1)
        self.assertFalse(result["target_group"]["ready"])
        request, offers = comparable()
        request["conditions"] = [{"key": "warranty", "label": "Гарантия", "value": 12, "op": "gte"}]
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertEqual(result["target_group"]["count"], 0)

    def test_accepted_technical_mismatch_and_unknown_keep_raw_facts_and_checks(self):
        request, offers = comparable()
        offers[0]["attributes"]["channels"] = 2
        offers[1]["attributes"].pop("channels")
        offers[1]["evidence"].pop("attributes.channels", None)
        for offer in offers[:2]:
            offer["manual_acceptances"] = {"attributes.channels": accept()}
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertTrue(result["target_group"]["ready"])
        self.assertEqual(result["target_group"]["manual_count"], 2)
        self.assertEqual(result["offers"][0]["attributes"]["channels"], 2)
        self.assertNotIn("channels", result["offers"][1]["attributes"])
        self.assertEqual(result["offers"][0]["requirement_checks"][0]["status"], "mismatch")
        self.assertTrue(result["offers"][0]["requirement_checks"][0]["acceptance"])

    def test_condition_acceptance_waives_only_that_condition(self):
        request, offers = comparable()
        request["conditions"] = [{"key": "warranty", "label": "Гарантия", "value": 12, "op": "gte"},
                                 {"key": "delivery", "label": "Доставка", "value": 5, "op": "lte"}]
        for offer in offers:
            offer["manual_acceptances"] = {"conditions.warranty": accept()}
        result = finalize(request, offers)
        self.assertTrue(all(o["status"] == "uncertain" for o in result["offers"]))
        self.assertEqual(result["offers"][0]["condition_checks"][0]["status"], "unconfirmed")
        self.assertTrue(result["offers"][0]["condition_checks"][0]["acceptance"])
        self.assertNotIn("acceptance", result["offers"][0]["condition_checks"][1])

    def test_acceptance_cannot_waive_commercial_fields_or_source_conflicts(self):
        request, offers = comparable()
        for field, mutate in (("price", lambda o: o.update(price=None)),
                              ("unit", lambda o: o.update(unit=None)),
                              ("tax", lambda o: o.update(tax="unknown")),
                              ("attributes.channels", lambda o: o.update(source_issues=["Конфликт модели и цены"]))):
            with self.subTest(field=field):
                offer = copy.deepcopy(offers[0]); mutate(offer)
                offer["manual_acceptances"] = {field: accept()}
                self.assertNotEqual(analyze_offers(request, [offer])["offers"][0]["status"], "included")

    def test_conflicting_supplier_identity_never_counts_in_target(self):
        request, offers = comparable()
        offers[0]["supplier_details"] = {"verification": {"status": "conflict"}, "evidence": {"inn": {"conflicts": [{"value": "7700000001"}, {"value": "7700000002"}]}}}
        offers[0]["manual_acceptances"] = {"attributes.channels": accept()}
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertEqual(result["target_group"]["count"], 2)
        self.assertEqual(result["offers"][0]["status"], "uncertain")
        self.assertFalse(result["target_group"]["ready"])

    def test_captured_marketplace_platforms_without_sellers_never_make_ready_group(self):
        request = parse_request("Осциллограф 4 канала полоса пропускания 100 МГц")
        urls = ["https://www.ozon.ru/product/qa-scope-100/", "https://market.yandex.ru/product/qa-scope-100/",
                "https://www.wildberries.ru/product/qa-scope-100/"]
        offers = [extract_product_offer(captured(CONTACTS, url), request) for url in urls]
        source = copy.deepcopy(offers)
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertEqual([o["status"] for o in result["offers"]], ["uncertain"] * 3)
        self.assertEqual(result["target_group"]["count"], 0)
        self.assertEqual(result["target_group"]["independent_suppliers"], 0)
        self.assertFalse(result["target_group"]["ready"])
        self.assertIsNone(result["calculation"]["total"])
        for offer in result["offers"]:
            self.assertTrue(offer["reason_supplier"])
            self.assertEqual(offer["supplier_details"]["verification"]["status"], "conflict")
            self.assertTrue(all(check["status"] == "matches" for check in offer["requirement_checks"]))
            self.assertFalse(offer.get("source_issues"))
        self.assertEqual(offers, source)
        self.assertEqual(result["base_result"]["offers"], source)

    def test_explicit_manual_seller_confirmation_is_visible_and_preserves_platform_capture(self):
        request = parse_request("Осциллограф 4 канала полоса пропускания 100 МГц")
        urls = ["https://www.ozon.ru/product/qa-scope-100/", "https://market.yandex.ru/product/qa-scope-100/",
                "https://www.wildberries.ru/product/qa-scope-100/"]
        offers = [extract_product_offer(captured(CONTACTS, url), request) for url in urls]
        line = finalize(request, offers)
        overrides = {offer["id"]: {"supplier": {"kind": "manual", "value": f"QA подтверждённый продавец {i}",
            "original_value": offer["supplier"], **accept()}} for i, offer in enumerate(offers)}
        result = annotate_target(replay_line(line, overrides, []), enforce=True)
        self.assertTrue(result["target_group"]["ready"])
        self.assertEqual(result["target_group"]["manual_count"], 3)
        self.assertEqual(result["base_result"]["offers"], offers)
        for offer in result["offers"]:
            self.assertIn("ручным подтверждением", offer["reason_supplier"])
            self.assertEqual(offer["supplier_details"]["verification"]["status"], "conflict")
            self.assertNotIn("supplier", offer["evidence"])

    def test_direct_shop_missing_inn_is_not_a_marketplace_seller_blocker(self):
        request = parse_request("Осциллограф 4 канала полоса пропускания 100 МГц")
        offers = [extract_product_offer(captured(url=f"https://direct-shop-{i}.example/product/scope-100/"), request) for i in range(3)]
        result = annotate_target(finalize(request, offers), enforce=True)
        self.assertTrue(result["target_group"]["ready"])
        self.assertEqual(result["target_group"]["manual_count"], 0)
        self.assertTrue(all(o["supplier_details"]["inn"] is None for o in result["offers"]))

    def test_aggregate_target_is_per_item_and_missing_item_never_becomes_ready(self):
        request, offers = comparable()
        item = {"id": "a", "title": "QA", "description": request["description"], "quantity": 1, "unit": request["unit"]}
        other = {**item, "id": "b"}
        result = aggregate_results({"description": "QA", "search_policy": {"mode": "target", "max_offers": 3, "max_seconds": 600}},
                                   [item, other], {"a": finalize(request, offers)})
        self.assertEqual(result["search"]["coverage"]["items_complete"], 1)
        self.assertEqual(result["search"]["coverage"]["target_matches"], 3)
        self.assertFalse(result["items"][1]["target_group"]["ready"])
        self.assertIsNone(result["calculation"]["total"])


class ActualSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "cost.sqlite3")
        self.store.create_job({"id": "cost", "status": "running", "events": []}, {})
        self.research = Research(self.store, "cost", {"description": "Осциллограф 4 канала 100 МГц", "quantity": 1}, threading.Event(), lambda *a: None)

    def test_valid_actual_charge_releases_overestimate_without_refunding_calls(self):
        with patch.dict("os.environ", {"APP_ACTUAL_COST_SETTLEMENT": "1"}):
            self.research.reserve_call([{"role": "user", "content": "QA"}])
            self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": .0001234})
        self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), 124)
        self.assertEqual(self.store.quota_usage("model_tokens", period="all"), 120)
        self.assertEqual(self.store.quota_usage("model_calls", period="all"), 1)

    def test_missing_or_invalid_cost_keeps_full_hold_when_actual_mode_enabled(self):
        for cost in (None, float("nan"), -1, True):
            with self.subTest(cost=cost), patch.dict("os.environ", {"APP_ACTUAL_COST_SETTLEMENT": "1"}):
                self.research.reserve_call([{"role": "user", "content": "QA"}])
                before = self.store.quota_usage("model_micro_usd", period="all")
                usage = {"prompt_tokens": 100, "completion_tokens": 20}
                if cost is not None: usage["cost"] = cost
                self.research.settle_call(usage)
                self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), before)
                self.assertIsNotNone(self.research.pending_reservation)
                self.research.pending_reservation = None

    def test_charge_above_reserve_retains_hold_and_blocks_next_call(self):
        with patch.dict("os.environ", {"APP_ACTUAL_COST_SETTLEMENT": "1"}):
            self.research.reserve_call([{"role": "user", "content": "QA"}])
            before = self.store.quota_usage("model_micro_usd", period="all")
            self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "cost": before / 1_000_000 + .1})
            self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), before)
            with self.assertRaises(BudgetStop):
                self.research.reserve_call([{"role": "user", "content": "QA"}])

    def test_default_preserves_estimate_floor(self):
        with patch.dict("os.environ", {"APP_ACTUAL_COST_SETTLEMENT": "0"}):
            self.research.reserve_call([{"role": "user", "content": "QA"}])
            self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "cost": .0001})
        self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), 200)


if __name__ == "__main__":
    unittest.main()

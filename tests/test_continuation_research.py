"""Continuation reuses immutable local captures and advances only new work."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from quotelens.agent import BudgetStop, PolicyResearch
from quotelens.procurement import ProcurementResearch, aggregate_results
from quotelens.storage import Store
from test_search_policy import FinishPlanner, SyntheticSources
from test_targeting import accept, comparable


class CompleteSources(SyntheticSources):
    def open(self, url, browser=False):
        document = super().open(url, browser=browser)
        document["text"] = document["text"].replace("Число каналов:", "Количество каналов")
        document["html"] = document["html"].replace("<p>Число каналов: 4 Полоса пропускания 100 МГц</p>",
            "<table><tr><th>Количество каналов</th><td>4</td></tr><tr><th>Полоса пропускания</th><td>100 МГц</td></tr></table>")
        return document


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "continuation.sqlite3")
        self.store.create_job({"id": "qa", "status": "running", "events": []}, {})
        self.cancel = threading.Event()

    def payload(self, mode="target", count=3, items=1):
        return {"description": "Локальная синтетическая проверка", "quantity": 1, "mode": "live", "limits": {"min_suppliers": 3},
                "search_policy": {"mode": mode, "max_offers": count, "max_seconds": 600},
                "items": [{"id": f"item-{i}", "title": "Осциллограф QA", "description": "Осциллограф 4 канала полоса пропускания 100 МГц", "quantity": 1, "unit": "шт"} for i in range(items)]}

    def run_research(self, payload, source, planner=None):
        return ProcurementResearch(self.store, "qa", payload, self.cancel, lambda *a: None,
                                   provider=planner or FinishPlanner(), acquisition_factory=lambda _: source).run()

    def resumed_payload(self, prior, mode="target"):
        payload = self.payload(mode=mode)
        payload["_resume_result"] = prior
        payload["_resume_documents"] = [{"document_id": row["id"], "url": row["url"]} for row in self.store.list_documents("qa")]
        return payload

    def test_target_requires_three_per_position_and_publishes_readable_partial(self):
        sources = {}
        def factory(item):
            sources[item["id"]] = CompleteSources(prefix=item["id"], count=5)
            return sources[item["id"]]
        with patch.object(self.store, "save_result", wraps=self.store.save_result) as save:
            result = ProcurementResearch(self.store, "qa", self.payload(items=2), self.cancel, lambda *a: None,
                       provider=FinishPlanner(), acquisition_factory=factory).run()
        self.assertEqual(result["search"]["stop"]["code"], "target_complete")
        self.assertEqual(result["search"]["coverage"]["items_complete"], 2)
        self.assertEqual(result["search"]["coverage"]["target_matches"], 6)
        self.assertTrue(all(line["target_group"]["count"] == 3 for line in result["items"]))
        self.assertTrue(any(call.args[1].get("search", {}).get("stop", {}).get("code") == "running" and call.args[1]["offers"] for call in save.call_args_list))

    def test_complete_position_stops_while_another_position_has_missing_proof(self):
        sources = {}
        def factory(item):
            source = CompleteSources(prefix=item["id"], count=4) if item["id"] == "item-0" else SyntheticSources(prefix=item["id"], count=4)
            sources[item["id"]] = source
            return source
        result = ProcurementResearch(self.store, "qa", self.payload(items=2), self.cancel, lambda *a: None,
                   provider=FinishPlanner(), acquisition_factory=factory).run()
        self.assertEqual(len(sources["item-0"].opened), 3)
        self.assertEqual(len(sources["item-1"].opened), 4)
        self.assertTrue(result["items"][0]["target_group"]["ready"])
        self.assertFalse(result["items"][1]["target_group"]["ready"])
        self.assertEqual(result["search"]["stop"]["code"], "source_exhausted")

    def test_seeded_partial_counters_are_incremental_before_first_turn(self):
        prior = self.run_research(self.payload(mode="offers", count=2, items=2), CompleteSources(count=4))
        payload = self.payload(items=2)
        payload["_resume_result"] = prior
        research = ProcurementResearch(self.store, "qa", payload, self.cancel, lambda *a: None,
                   provider=FinishPlanner(), acquisition_factory=lambda _: CompleteSources(count=0))
        result = research.publish()
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)
        self.assertEqual(result["search"]["budget"]["actual_usage"]["cost"], 0)
        self.assertEqual(result["search"]["budget"]["pages_used"], 0)
        self.assertEqual(len(result["offers"]), len(prior["offers"]))

    def test_continuation_keeps_captures_and_takes_old_frontier_without_refetch(self):
        first_source = CompleteSources(count=5)
        prior = self.run_research(self.payload(mode="offers", count=2), first_source)
        old_offers = copy.deepcopy(prior["items"][0]["base_result"]["offers"])
        source = CompleteSources(count=5); planner = FinishPlanner()
        result = self.run_research(self.resumed_payload(prior), source, planner)
        self.assertEqual(len(result["offers"]), 3)
        self.assertEqual(result["search"]["stop"]["code"], "target_complete")
        self.assertFalse(set(first_source.opened) & set(source.opened))
        self.assertEqual(len(source.opened), 1)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(result["search"]["budget"]["pages_used"], 1)
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)
        self.assertEqual(result["items"][0]["base_result"]["offers"][:2], old_offers)
        self.assertTrue(all(self.store.get_document("qa", o["document_id"]) for o in old_offers))

    def test_no_new_results_never_discards_source_values_or_manual_decisions(self):
        prior = self.run_research(self.payload(mode="offers", count=2), CompleteSources(count=2))
        line = prior["items"][0]
        line["base_result"]["offers"][0]["attributes"]["channels"] = 2
        line["offers"][0]["attributes"]["channels"] = 2
        line["offers"][0]["manual_acceptances"] = {"attributes.channels": accept()}
        source = CompleteSources(count=0)
        planner = FinishPlanner(); planner.configured = False
        result = self.run_research(self.resumed_payload(prior), source, planner)
        first = result["items"][0]["offers"][0]
        self.assertEqual(len(result["offers"]), 2)
        self.assertEqual(first["attributes"]["channels"], 2)
        self.assertEqual(first["manual_acceptances"]["attributes.channels"], accept())
        self.assertEqual(result["items"][0]["base_result"]["offers"][0]["attributes"]["channels"], 2)
        self.assertNotIn("manual_acceptances", result["items"][0]["base_result"]["offers"][0])
        self.assertEqual(source.opened, [])
        self.assertEqual(result["search"]["budget"]["pages_used"], 0)
        old_queries = set(line["search"]["continuation_state"]["seen_queries"])
        self.assertFalse(old_queries & {q.casefold() for q in source.queries})

    def test_explicit_continuation_of_ready_group_still_searches_more(self):
        first_source = CompleteSources(count=5)
        prior = self.run_research(self.payload(), first_source)
        source = CompleteSources(count=5)
        result = self.run_research(self.resumed_payload(prior), source)
        self.assertTrue(source.opened or source.queries)
        self.assertEqual(len(result["offers"]), 4)

    def test_exhausted_continuation_does_not_pay_for_identical_planning_state(self):
        prior = self.run_research(self.payload(mode="time"), CompleteSources(count=2))
        source = CompleteSources(count=2); planner = FinishPlanner()
        result = self.run_research(self.resumed_payload(prior, mode="time"), source, planner)
        self.assertEqual(result["search"]["stop"]["code"], "source_exhausted")
        self.assertEqual(planner.calls, 0)
        self.assertEqual(source.opened, [])
        self.assertEqual(source.queries, [])
        self.assertEqual(len(result["offers"]), 2)

    def test_legacy_without_state_restores_only_matching_and_global_durable_queries(self):
        payload = self.payload(items=2)
        for index, item in enumerate(payload["items"]):
            item["title"] += f"-{index}"
        prior = aggregate_results(payload, payload["items"], {})
        for line in prior["items"]:
            self.assertNotIn("continuation_state", line.get("search", {}))
        queries = [line["request"]["search_terms"][0] + " купить цена" for line in prior["items"]]
        payload["_resume_result"] = prior
        payload["_resume_queries"] = [{"item_id": f"item-{i}", "query": query.upper()} for i, query in enumerate(queries)]
        payload["_resume_queries"].append({"item_id": None, "query": "ГЛОБАЛЬНЫЙ ЗАПРОС"})
        sources = {}
        def factory(item):
            sources[item["id"]] = CompleteSources(count=0)
            return sources[item["id"]]
        planner = FinishPlanner(); planner.configured = False
        research = ProcurementResearch(self.store, "qa", payload, self.cancel, lambda *a: None,
                   provider=planner, acquisition_factory=factory)
        for i, child in enumerate(research.researchers):
            self.assertIn(queries[i].casefold(), child.seen_queries)
            self.assertNotIn(queries[1-i].casefold(), child.seen_queries)
            self.assertIn("глобальный запрос", child.seen_queries)
            self.assertNotEqual(child.next_discovery_query().casefold(), queries[i].casefold())
            self.assertIn("error", child.execute_tool("search_web", {"query": queries[i]}))
        result = research.run()
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(planner.calls, 0)
        for i, source in enumerate(sources.values()):
            self.assertTrue(source.queries)
            self.assertNotIn(queries[i].casefold(), {q.casefold() for q in source.queries})

    def test_legacy_captures_restore_matching_frontier_and_mark_all_captured_urls_first(self):
        payload = self.payload(items=2)
        payload["_resume_result"] = aggregate_results(payload, payload["items"], {})
        visited = "https://captured.test/old-card"
        a, b, shared = "https://new-a.test/card", "https://new-b.test/card", "https://shared.test/card"
        plain_a, plain_shared = "https://new-a.test/other", "https://shared.test/other"
        payload["_resume_documents"] = [
            {"document_id": "listing-a", "url": "https://catalog-a.test/list", "page_kind": "listing", "item_ids": ["item-0"],
             "product_links": [{"url": visited}, {"url": a}], "links": [{"url": plain_a}]},
            {"document_id": "listing-b", "url": "https://catalog-b.test/list", "page_kind": "listing", "item_ids": ["item-1"],
             "product_links": [{"url": b}], "links": []},
            {"document_id": "global-list", "url": "https://global.test/list", "item_ids": [],
             "product_links": [{"url": shared}], "links": [{"url": plain_shared}]},
            {"document_id": "captured-last", "url": visited, "item_ids": ["item-1"], "product_links": [], "links": []}]
        research = ProcurementResearch(self.store, "qa", payload, self.cancel, lambda *a: None,
                   provider=FinishPlanner(), acquisition_factory=lambda _: CompleteSources(count=0))
        expected = [{a, plain_a, shared, plain_shared}, {b, shared, plain_shared}]
        for child, urls in zip(research.researchers, expected):
            restored = {link["url"] for queue in child.frontier_items().values() for link in queue}
            self.assertEqual(restored, urls)
            self.assertIn(child.page_key(visited), child.opened_urls)
            self.assertNotIn(visited, restored)
        self.assertNotIn("listing-b", research.researchers[0].document_ids)
        self.assertNotIn("listing-a", research.researchers[1].document_ids)
        self.assertTrue(all("global-list" in child.document_ids for child in research.researchers))

    def test_legacy_without_state_uses_captured_product_links_without_refetch_or_planner(self):
        first = CompleteSources(count=3)
        prior = self.run_research(self.payload(mode="offers", count=2), first)
        prior["items"][0]["search"].pop("continuation_state")
        payload = self.resumed_payload(prior)
        new_url = next(hit["url"] for hit in first.hits if hit["url"] not in first.opened)
        old_url = first.opened[0]
        listing = {"url": "https://catalog.test/list", "title": "Сохранённый каталог QA", "text": "Каталог QA", "method": "synthetic-QA", "retrieved_at": "2026-11-13T00:00:00Z",
                   "product_links": [{"url": old_url}, {"url": new_url}], "links": []}
        docid = self.store.add_document("qa", listing)
        payload["_resume_documents"].insert(0, {**listing, "document_id": docid, "page_kind": "listing", "item_ids": ["item-0"]})
        payload["_resume_queries"] = [{"item_id": "item-0", "query": query.upper()} for query in first.queries]
        source = CompleteSources(count=3); planner = FinishPlanner()
        result = self.run_research(payload, source, planner)
        self.assertEqual(source.opened, [new_url])
        self.assertEqual(source.queries, [])
        self.assertEqual(planner.calls, 0)
        self.assertEqual(result["search"]["budget"]["pages_used"], 1)
        self.assertEqual(result["search"]["stop"]["code"], "target_complete")
        self.assertEqual(len(result["offers"]), 3)
        self.assertTrue(all(self.store.get_document("qa", o["document_id"]) for o in prior["items"][0]["base_result"]["offers"]))

    def test_internally_caught_stop_is_not_reported_as_exhausted_sources(self):
        for code in ("time_budget", "cancelled", "global_budget", "cost_budget", "token_budget", "context_budget",
                     "model_budget", "page_budget", "offer_target", "provider_usage_error", "provider_error", "provider_unavailable"):
            with self.subTest(code=code):
                def stopped_stream(child):
                    child.enqueue_links([{"url": "https://unread.test/product", "title": "Осциллограф QA"}])
                    try:
                        raise BudgetStop(code)
                    except BudgetStop as exc:
                        child.stop_code = str(exc)
                    child.finished = True
                    yield from ()
                source, planner = CompleteSources(count=0), FinishPlanner()
                with patch.object(PolicyResearch, "stream", stopped_stream):
                    result = self.run_research(self.payload(), source, planner)
                self.assertEqual(result["search"]["stop"]["code"], code)
                self.assertNotEqual(result["search"]["stop"]["label"], "Доступные найденные ссылки и варианты запросов обработаны")
                self.assertEqual(result["items"][0]["search"]["stop"]["code"], code)
                self.assertEqual(result["items"][0]["search"]["frontier_remaining"], 1)
                self.assertFalse(result["items"][0]["target_group"]["ready"])
                self.assertEqual(source.opened, [])
                self.assertEqual(source.queries, [])
                self.assertEqual(planner.calls, 0)

    def test_internal_cancellation_and_deadline_take_priority_over_other_leaf_errors(self):
        for codes, expected in ((["provider_error", "time_budget"], "time_budget"),
                                (["time_budget", "cancelled"], "cancelled"),
                                (["source_exhausted", "cost_budget"], "cost_budget")):
            with self.subTest(codes=codes):
                def stopped_stream(child):
                    child.stop_code = codes[int(child.item_id.rsplit("-", 1)[1])]
                    child.finished = True
                    yield from ()
                with patch.object(PolicyResearch, "stream", stopped_stream):
                    result = self.run_research(self.payload(items=2), CompleteSources(count=0))
                self.assertEqual(result["search"]["stop"]["code"], expected)
                self.assertEqual([line["search"]["stop"]["code"] for line in result["items"]], codes)

    def test_completed_target_takes_priority_over_internally_caught_deadline(self):
        def completed_stream(child):
            child.request, child.offers = comparable()
            child.stop_code, child.finished = "time_budget", True
            yield from ()
        with patch.object(PolicyResearch, "stream", completed_stream):
            result = self.run_research(self.payload(), CompleteSources(count=0))
        self.assertTrue(result["items"][0]["target_group"]["ready"])
        self.assertEqual(result["search"]["coverage"]["items_complete"], 1)
        self.assertEqual(result["search"]["stop"]["code"], "target_complete")

    def test_bad_cv_continues_and_preserves_every_candidate(self):
        class SpreadSources(CompleteSources):
            def open(self, url, browser=False):
                document = super().open(url, browser=browser)
                price = 1000000 if url.endswith("1002") else 10000
                document["text"] = document["text"].replace("10000 руб.", f"{price} руб.")
                document["html"] = document["html"].replace("10000 руб.", f"{price} руб.")
                return document
        source = SpreadSources(count=4)
        result = self.run_research(self.payload(), source)
        group = result["items"][0]["target_group"]
        self.assertEqual(len(source.opened), 4)
        self.assertEqual(group["count"], 4)
        self.assertFalse(group["ready"])
        self.assertGreater(group["cv"], 33)
        self.assertEqual(result["search"]["stop"]["code"], "source_exhausted")


if __name__ == "__main__":
    unittest.main()

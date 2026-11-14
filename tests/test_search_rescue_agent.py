"""Isolated agent-loop regressions. Synthetic source/model doubles, no paid calls."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from quotelens.acquisition import AcquisitionError
from quotelens.agent import BudgetStop, MODEL_CONTEXT_BYTES, PolicyResearch, Research
from quotelens.procurement import JobBudget, request_for_item
from quotelens.storage import Store


def document(url="https://shop.test/product"):
    title = "Осциллограф QA100"
    main = title + " Цена1000руб. за шт. с НДС Количество каналов:4 Полоса пропускания:100МГц В корзину"
    return {"url": url, "title": title, "primary_title": title, "text": main,
            "html": '<main class="product-detail"><h1>' + title + '</h1><p class="price">Цена1000руб. за шт. с НДС</p><p>Количество каналов:4</p><p>Полоса пропускания:100МГц</p><button>В корзину</button></main>',
            "retrieved_at": "2026-11-14T00:00:00Z", "method": "synthetic"}


class Sources:
    timeout = 10

    def __init__(self):
        self.opened, self.queries = [], []

    def open(self, url, browser=False):
        self.opened.append((url, browser))
        return document(url)

    def search(self, query, limit=6):
        self.queries.append(query)
        return {"query": query, "status": "empty", "hits": []}


class Provider:
    configured, base, model = True, "https://qa.invalid", "synthetic-no-model"

    def complete(self, messages, tools, **kwargs):
        self.last_tools = tools
        raise RuntimeError("STOP_AFTER_FIRST_PLANNING_CALL")


class RescueAgentTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.create_job({"id": "rescue", "status": "running", "events": []}, {})
        self.cancel = threading.Event()

    def research(self, *, policy=False, sources=None, **kwargs):
        payload = {"description": "Осциллограф4канала100МГц", "quantity": 1, **kwargs}
        self.sources = sources or Sources()
        self.provider = Provider()
        if not policy:
            return Research(self.store, "rescue", payload, self.cancel, lambda *a: None,
                            provider=self.provider, acquisition=self.sources)
        item = {"id": "line", "title": "Осциллограф", "description": payload["description"],
                "quantity": 1, "unit": "шт", "requirements": []}
        payload.update(item_id="line", structured_request=request_for_item(item))
        budget = JobBudget({"mode": "target", "max_offers": 3, "max_seconds": 60}, self.cancel)
        return PolicyResearch(self.store, "rescue", payload, self.cancel, lambda *a: None,
                              provider=self.provider, acquisition=self.sources, budget=budget)

    def test_authoritative_configuration_is_noop_and_tool_is_hidden(self):
        research = self.research(policy=True)
        before = copy.deepcopy(research.request)
        reply = research.execute_tool("configure_request", {"proposal": {"requirements": "malformed"}})
        self.assertTrue(reply["unchanged"])
        self.assertEqual(research.request, before)
        with self.assertRaisesRegex(RuntimeError, "STOP_AFTER_FIRST_PLANNING_CALL"):
            list(research.stream())
        self.assertNotIn("configure_request", [t["function"]["name"] for t in self.provider.last_tools])

    def test_all24_requirement_values_survive_compaction_without_source_bloat(self):
        research = self.research(policy=True)
        research.request["requirements"] = [
            {"key": "field_" + str(n), "label": "Поле" + str(n), "value": n, "op": "eq",
             "source": {"quote": "Исходный документ " * 1000}, "quote": "Исходный документ " * 1000}
            for n in range(24)]
        original = copy.deepcopy(research.request)
        messages = [{"role": "system", "content": "QA"}, {"role": "user", "content": "QA"}]
        compact = research.compact_messages(messages)
        request = json.loads(compact[1]["content"])["request"]
        self.assertEqual([r["value"] for r in request["requirements"]], list(range(24)))
        self.assertTrue(request["requirements_authoritative"])
        self.assertTrue(all("source" not in r for r in request["requirements"]))
        self.assertEqual(len(Research.model_output(list(range(24)))), 24)
        self.assertLessEqual(research.context_bytes(compact), MODEL_CONTEXT_BYTES)
        self.assertEqual(research.request, original)

    def test_warm_refresh_is_two_http_reads_not_nine_browser_reads(self):
        research = self.research(policy=True)
        links = [{"url": "https://shop" + str(n) + ".test/product", "title": "Осциллограф QA100"} for n in range(9)]
        with patch.object(self.store, "discovery_urls", return_value=links):
            list(research._refresh_steps())
        self.assertEqual(len(self.sources.opened), 2)
        self.assertTrue(all(browser is False for _, browser in self.sources.opened))

    def test_primary_candidate_precedes_seller_terms_and_editorial(self):
        research = self.research()
        research.enqueue_links([{"url": "https://a.test/payment", "title": "Оплата"},
                                {"url": "https://b.test/blog/review", "title": "Осциллограф QA100"},
                                {"url": "https://c.test/product", "title": "Осциллограф QA100"}], product=True)
        research.seller_term_targets[research.page_key("https://a.test/payment")] = "primary"
        self.assertEqual(research.next_frontier()["url"], "https://c.test/product")

    def test_unproductive_reads_trigger_fresh_query_and_eis_is_honest_priority(self):
        research = self.research(policy=True)
        first = research.next_discovery_query()
        self.assertIn("site:zakupki.gov.ru", first)
        research.execute_tool("search_web", {"query": first})
        self.assertTrue(research.eis_priority["attempted"])
        self.assertFalse(research.eis_priority["connector"])
        research.pages, research.last_priced_page = 2, 0
        self.assertTrue(research.should_diversify())
        self.assertNotIn("site:zakupki.gov.ru", research.next_discovery_query(diverse=True))
        self.assertFalse(research.execute_tool("finish", {"reason": "too early"})["finished"])
        self.assertFalse(research.finish_requested)

    def test_read_find_exposes_real_late_document_window(self):
        research = self.research()
        text = "Навигация " * 5000 + "Цена123000рублей НДС включён"
        research.documents["late"] = {"document_id": "late", "text": text, "title": "QA",
                                      "url": "https://shop.test/product"}
        reply = research.execute_tool("read_document", {"document_id": "late", "find": "Цена123000", "length": 1000})
        self.assertIn("Цена123000", reply["text"])
        self.assertGreater(reply["offset"], 40000)
        self.assertEqual(reply["text"], text[reply["offset"]:reply["offset"] + 1000])

    def test_article_routes_to_discovery_without_creating_offer(self):
        research = self.research()
        article = document()
        research.documents["article"] = article
        with patch("quotelens.agent.classify_page", return_value={"kind": "article", "reason": "synthetic editorial", "product_links": []}):
            reply = research.execute_tool("record_offer", {"document_id": "article", "offer": {"price": 123}})
        self.assertEqual(reply["page_kind"], "article")
        self.assertEqual(research.offers, [])
        self.assertIn("article", research.discovery_pages)

    def test_transient_network_error_keeps_one_retry_and_no_first_failure_siteban(self):
        class Failing(Sources):
            def open(self, url, browser=False):
                self.opened.append((url, browser))
                exc = AcquisitionError("Synthetic DNS timeout")
                exc.code, exc.retryable = "dns_resolution", True
                raise exc
        sources = Failing()
        url = "https://shop.test/product"
        research = self.research(sources=sources, source_urls=[url])
        for n in range(2):
            with self.assertRaises(AcquisitionError):
                research.execute_tool("open_page", {"url": url})
            if n == 0:
                self.assertNotIn("shop.test", research.unavailable_sites)
                self.assertNotIn("-site:shop.test", research.next_discovery_query(diverse=True))
        response = research.execute_tool("open_page", {"url": url})
        self.assertEqual(response["status"], "unavailable")
        self.assertEqual(len(sources.opened), 2)
        self.assertEqual(research.acquisition_error_output(AcquisitionError("safe"))["error"], "safe")

    def test_thirty_model_calls_are_shared_across_all_items(self):
        research = self.research(policy=True)
        research.budget.model_calls = 30
        with self.assertRaisesRegex(BudgetStop, "model_budget"):
            research.budget.before_reserve(100, 100)

    def test_continuation_reclassifies_old_article_without_deleting_capture(self):
        primary = document("https://shop.test/blog/article")
        primary["document_id"] = "captured-article"
        prior = {"request": {"description": "Осциллограф", "category": "oscilloscope", "product": "Осциллограф",
                             "quantity": 1, "unit": "шт", "requirements": []},
                 "offers": [{"id": "old", "title": primary["title"], "url": primary["url"],
                             "document_id": primary["document_id"], "price": None}]}
        original = copy.deepcopy(prior)
        with patch("quotelens.agent.classify_page", return_value={"kind": "article", "reason": "editorial", "product_links": []}):
            research = self.research(policy=True, _resume_item=prior, _resume_documents=[primary])
        self.assertEqual(research.offers, [])
        self.assertIn("captured-article", research.discovery_pages)
        self.assertIn("captured-article", research.document_ids)
        self.assertEqual(prior, original)

    def fact_refresh_fixture(self):
        research = self.research()
        primary = document()
        primary["document_id"] = "primary"
        fact = document("https://facts.test/product")
        fact["document_id"] = "fact"
        research.documents.update(primary=primary, fact=fact)
        research.fact_document_ids.extend(["primary", "fact"])
        previous = {"id": "offer", "title": primary["title"], "document_id": "primary",
                    "domain": "shop.test", "url": primary["url"], "attributes": {"power": 1200},
                    "evidence": {"attributes.power": {"quote": "Power1200", "url": primary["url"]}},
                    "validation": {"scope": "single_product", "extraction": "deterministic_primary_product"}}
        research.offers = [previous]
        return research, previous

    def test_late_fact_conflict_persists_even_when_values_and_quotes_unchanged(self):
        research, previous = self.fact_refresh_fixture()
        candidate = copy.deepcopy(previous)
        issue = "Характеристика «power» различается в документах одной модели; требуется проверка версии или комплектации."
        candidate["source_issues"] = [issue]
        candidate["validation"]["fact_conflicts"] = [
            {"field": "attributes.power", "sources": [{"document_id": "fact", "quote": "Power500"}]}]
        candidate["fact_documents"] = [{"document_id": "fact", "sha256": "new-hash"}]
        with patch("quotelens.agent.validate_candidate", return_value=candidate), patch.object(research, "persist_offer") as persist:
            research.refresh_fact_offers("fact")
        persist.assert_called_once()
        saved = persist.call_args.args[0]
        self.assertEqual(saved["attributes"]["power"], 1200)
        self.assertEqual(saved["source_issues"], [issue])
        self.assertTrue(saved["validation"]["fact_conflicts"])
        self.assertEqual(saved["fact_documents"][0]["document_id"], "fact")

    def test_stale_cross_document_warning_clears_but_raw_capture_issue_survives_merge(self):
        research, previous = self.fact_refresh_fixture()
        issue = "Характеристика «power» различается в документах одной модели; требуется проверка версии или комплектации."
        raw = "Неоднозначная комплектация в первичной карточке."
        previous["source_issues"] = [raw, issue]
        previous["validation"]["fact_conflicts"] = [{"field": "attributes.power", "sources": [{"document_id": "fact"}]}]
        previous["validation"]["fact_uncertainties"] = [{"field": "attributes.materials", "reason": "older interpretation"}]
        previous["fact_documents"] = [{"document_id": "fact"}]
        candidate = copy.deepcopy(previous)
        candidate.pop("source_issues")
        candidate.pop("fact_documents")
        candidate["validation"].pop("fact_conflicts")
        candidate["validation"].pop("fact_uncertainties")
        with patch("quotelens.agent.validate_candidate", return_value=candidate):
            research.refresh_fact_offers("fact")
        saved = research.offers[0]
        self.assertEqual(saved["source_issues"], [raw])
        self.assertFalse(saved["validation"].get("fact_conflicts"))
        self.assertFalse(saved["validation"].get("fact_uncertainties"))
        self.assertEqual(saved["fact_documents"], [])
        self.assertEqual(saved["validation"]["extraction"], "deterministic_primary_product")


if __name__ == "__main__":
    unittest.main()

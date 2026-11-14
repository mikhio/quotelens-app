"""Local QA sources and model doubles; these are not market observations."""
import copy
import json
import tempfile
import threading
import unittest
from urllib.parse import quote, urlsplit
from pathlib import Path
from unittest.mock import patch

from quotelens.agent import MODEL_CONTEXT_BYTES, Research
from quotelens.acquisition import AcquisitionError
from quotelens.providers import ModelProvider, ProviderError
from quotelens.storage import Store


def call(name, **arguments):
    return {"id": name + str(len(json.dumps(arguments))), "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class ScriptedProvider:
    configured, base, model = True, "https://qa.invalid", "explicit-qa-double"

    def __init__(self, turns):
        self.turns, self.messages = list(turns), []

    def complete(self, messages, tools, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if not self.turns:
            raise AssertionError("Unexpected paid/model turn in QA")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return {"message": {"role": "assistant", "content": None, "tool_calls": turn},
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                "finish_reason": "tool_calls"}


class CapturedSources:
    """A tiny deterministic public-source double with deliberately large pages."""

    def __init__(self, before_open=None):
        self.before_open, self.opened = before_open, []
        self.hits = [{"url": f"https://shop-{site}.test/qa-{number}",
                      "title": f"Осциллограф QA-{number}{site}", "snippet": "Explicit QA fixture"}
                     for site in "abc" for number in (100, 200)]

    def search(self, query, limit=6):
        return {"hits": self.hits[:limit], "status": "ok", "query": query}

    def open(self, url, browser=False):
        if self.before_open:
            self.before_open(url)
        self.opened.append(url)
        number = url.rsplit("-", 1)[-1]
        title = "Осциллограф QA-" + number
        price = 10000 + int(number)
        line = f"Цена: {price} руб. за шт. с НДС"
        main = f"{title} {line} 4 канала Полоса пропускания 100 МГц В корзину"
        # Massive navigation and recommendations must stay in saved documents,
        # and must not grow the next paid planning context.
        noise = "Служебное меню QA " * 2000
        text = noise + main + " Похожие товары " + noise
        html = (f"<header>{noise}</header><main class='product-detail'><h1>{title}</h1>"
                f"<div class='price'>{line}</div><p>4 канала Полоса пропускания 100 МГц</p>"
                f"<button>В корзину</button></main><section>Похожие товары {noise}</section>")
        return {"url": url, "title": title, "primary_title": title, "text": text,
                "html": html, "links": [], "retrieved_at": "2026-11-12T00:00:00+00:00", "method": "qa"}


class SearchReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "qa.sqlite3")
        self.store.create_job({"id": "qa", "status": "running", "events": []}, {})
        self.cancel = threading.Event()
        self.payload = {"description": "Осциллограф", "quantity": 1}

    def research(self, turns, acquisition=None, **payload):
        return Research(self.store, "qa", {**self.payload, **payload}, self.cancel, lambda *args: None,
                        provider=ScriptedProvider(turns), acquisition=acquisition or CapturedSources())

    def test_early_finish_yields_six_durable_priced_products_from_three_sites(self):
        def assert_previous_committed(url):
            saved = self.store.get_job("qa").get("result", {}).get("offers", [])
            self.assertEqual(len(saved), len(sources.opened), "A new fetch began before its predecessor was committed")

        sources = CapturedSources(assert_previous_committed)
        research = self.research([[call("search_web", query="Осциллограф купить цена")], [call("finish", reason="QA early finish")]], sources)
        result = research.run()
        self.assertEqual(len(result["offers"]), 6)
        self.assertTrue(all(offer["price"] and offer["evidence"].get("price") for offer in result["offers"]))
        self.assertEqual(result["search"]["coverage"]["priced_domains"], 3)
        self.assertEqual(result["search"]["reused_discovery_urls"], 0)
        self.assertEqual(result["search"]["prices_refetched"], 0)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 6)
        self.assertEqual([url.split("/")[2] for url in sources.opened[:3]], ["shop-a.test", "shop-b.test", "shop-c.test"])
        self.assertNotEqual(result["calculation"]["status"], "ready", "Bare product name cannot supply missing procurement specs")
        for messages in research.provider.messages:
            self.assertLessEqual(research.context_bytes(messages), MODEL_CONTEXT_BYTES)

    def test_provider_failure_recovers_existing_frontier_without_more_paid_calls(self):
        research = self.research([[call("search_web", query="Осциллограф цена")], ProviderError("QA unavailable")])
        result = research.run()
        self.assertEqual(result["search"]["stop"]["code"], "provider_error")
        self.assertEqual(len(result["offers"]), 6)
        self.assertEqual(len(research.provider.messages), 2)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 6)

    def test_model_call_cap_recovers_links_and_still_accounts_for_the_only_call(self):
        research = self.research([[call("search_web", query="Осциллограф цена")]], limits={"max_model_calls": 1})
        result = research.run()
        self.assertEqual(result["search"]["stop"]["code"], "model_budget")
        self.assertEqual(len(result["offers"]), 6)
        self.assertEqual(self.store.quota_usage("model_calls", period="all"), 1)
        self.assertEqual(research.actual_usage["total_tokens"], 120)

    def test_cancel_during_read_commits_that_page_and_never_opens_another(self):
        sources = CapturedSources(lambda url: self.cancel.set())
        research = self.research([[call("search_web", query="Осциллограф цена")]], sources)
        result = research.run()
        self.assertEqual(result["search"]["stop"]["code"], "cancelled")
        self.assertEqual(len(sources.opened), 1)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 1)

    def test_page_cap_reports_partial_coverage_and_keeps_every_acquired_offer(self):
        research = self.research([[call("search_web", query="Осциллограф цена")], [call("finish", reason="QA")]],
                                 limits={"max_pages": 4})
        result = research.run()
        self.assertEqual(result["search"]["stop"]["code"], "page_budget")
        self.assertEqual(result["search"]["budget"]["pages_used"], 4)
        self.assertEqual(len(result["offers"]), 4)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 4)

    def test_progress_publishes_bounded_source_verdicts_after_durable_offer_save(self):
        research = self.research([])
        url = "https://shop-a.test/qa-100"
        research.discovered.add(url)
        research.execute_tool("open_page", {"url": url})
        progress = self.store.get_job("qa")["search_progress"]
        self.assertEqual(progress["offers_count"], 1)
        self.assertEqual(len(progress["sources"]), 1)
        self.assertTrue(progress["sources"][0]["offer_id"])
        self.assertEqual(progress["sources"][0]["status"], "visited")
        self.assertLess(len(json.dumps(progress).encode()), 4000)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 1)

    def test_priced_accessories_and_unknown_required_specs_do_not_satisfy_coverage(self):
        research = self.research([])
        observations = [
            {"id": "probe", "title": "Щуп для осциллографа QA-100", "domain": "shop-a.test", "price": 500,
             "evidence": {"price": {"quote": "500 руб."}}, "attributes": {}},
            {"id": "case", "title": "Кейс: осциллограф QA-100", "domain": "shop-b.test", "price": 600,
             "evidence": {"price": {"quote": "600 руб."}}, "attributes": {}},
            {"id": "product", "title": "Осциллограф QA-100", "domain": "shop-c.test", "price": 10000,
             "evidence": {"price": {"quote": "10000 руб."}}, "attributes": {}}]
        research.offers = observations
        research.enqueue_links([{"url": "https://shop-a.test/probe", "title": observations[0]["title"]},
                                {"url": "https://shop-b.test/case", "title": observations[1]["title"]}])
        self.assertFalse(research.queued, "Known accessory links must not consume the automatic discovery frontier")
        self.assertEqual(research.coverage()["offers"], 3)
        self.assertEqual(research.coverage()["priced_offers"], 1)
        self.assertEqual(research.coverage()["priced_domains"], 1)
        research.request["requirements"] = [{"key": "channels", "op": "gte", "value": 4}]
        self.assertEqual(research.coverage()["priced_offers"], 0)
        observations[2]["attributes"]["channels"] = 2
        self.assertEqual(research.coverage()["priced_offers"], 0)
        observations[2]["attributes"]["channels"] = 4
        self.assertEqual(research.coverage()["priced_offers"], 1)

    def test_scoped_frontier_deduplicates_canonical_links_and_balances_sites(self):
        sources = CapturedSources()
        research = self.research([], sources, source_scope={"mode": "domains", "domains": ["shop-a.test", "shop-b.test"]})
        research.enqueue_links(sources.hits + [{"url": sources.hits[0]["url"] + "#price"}, {"url": sources.hits[0]["url"] + "/"}])
        self.assertEqual(len(research.queued), 4)
        research.drain_frontier()
        self.assertEqual(len(sources.opened), 4)
        self.assertEqual({url.split("/")[2] for url in sources.opened}, {"shop-a.test", "shop-b.test"})
        self.assertNotEqual(sources.opened[0].split("/")[2], sources.opened[1].split("/")[2])

    def test_complete_turn_compaction_bounds_repeated_large_arguments_and_page_outputs(self):
        research = self.research([])
        base = [{"role": "system", "content": "QA"}, {"role": "user", "content": "Осциллограф"}]
        messages = copy.deepcopy(base)
        for turn in range(9):
            calls = [{"id": f"qa-{turn}-{page}", "type": "function", "function": {
                "name": "record_offer" if turn < 7 else "open_page",
                "arguments": json.dumps({"document_id": str(turn), "offer": {"irrelevant_large_argument": "Я" * 22000}})}}
                for page in range(5)]
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            for item in calls:
                messages.append({"role": "tool", "tool_call_id": item["id"], "content": json.dumps({
                    "document_id": item["id"], "text": "Я" * 50000,
                    "links": [{"url": "https://shop-a.test/qa-100", "title": "Я" * 4000}] * 35})})
        compact = research.compact_messages(messages)
        self.assertLessEqual(research.context_bytes(compact), MODEL_CONTEXT_BYTES)
        self.assertIn("verified_offers", compact[2]["content"])
        self.assertFalse(any(item.get("role") == "tool" for item in compact), "Huge turns must be removed whole")
        # A normal multi-open turn retains pairings and at most two excerpts.
        calls = [{"id": f"small-{i}", "type": "function", "function": {"name": "open_page", "arguments": "{}"}} for i in range(5)]
        messages = base + [{"role": "assistant", "content": None, "tool_calls": calls}]
        messages += [{"role": "tool", "tool_call_id": item["id"], "content": json.dumps({"document_id": item["id"], "text": "QA " * 10000})} for item in calls]
        compact = research.compact_messages(messages)
        tools = [item for item in compact if item.get("role") == "tool"]
        self.assertEqual({item["tool_call_id"] for item in tools}, {item["id"] for item in calls})
        self.assertEqual(sum("text" in json.loads(item["content"]) for item in tools), 2)
        self.assertLessEqual(research.context_bytes(compact), MODEL_CONTEXT_BYTES)

    def test_maximum_description_and_long_input_links_cannot_bloat_context(self):
        research = self.research([], description="Осциллограф " + "Я" * 3980,
            source_urls=["https://shop-a.test/" + str(i) + "x" * 4000 for i in range(10)])
        research.request["requirements"] = [{"key": "qa_" + str(i), "op": "eq", "value": "Я" * 3900} for i in range(30)]
        messages = [{"role": "system", "content": "QA"}, {"role": "user", "content": json.dumps(research.payload)}]
        compact = research.compact_messages(messages)
        self.assertLessEqual(research.context_bytes(compact), MODEL_CONTEXT_BYTES)
        self.assertIn(research.payload["description"], compact[1]["content"])
        self.assertEqual(len(research.request["requirements"][0]["value"]), 3900, "Context summaries must not alter actual requirements")

    def test_provider_rejects_oversize_arguments_instead_of_slicing_json(self):
        with patch.dict("os.environ", {"MODEL_API_BASE": "https://qa.invalid", "MODEL_NAME": "qa", "MODEL_API_KEY": "qa-not-a-secret"}):
            provider = ModelProvider()
        response = {"choices": [{"message": {"tool_calls": [call("record_offer", evidence="Я" * 26000)]}}]}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return json.dumps(response, ensure_ascii=False).encode()[:limit]

        with patch.object(provider.opener, "open", return_value=FakeResponse()):
            with self.assertRaisesRegex(ProviderError, "лимит аргументов"):
                provider.complete([{"role": "user", "content": "QA"}], [])

    def test_live_failure_trace_does_not_spend_pages_on_blocked_domains_or_search_wrappers(self):
        # Replay the URL/domain ordering of fresh job 0502fb5d. Source bodies
        # below are explicitly QA doubles; no public source is contacted.
        product = "https://supereyes.ru/catalog/oscilloscopes_digital/cifrovoj_oscillograf_rigol_ds1102e_2_kanala_h_100_mgc/"
        ozon_product = "https://www.ozon.ru/product/tsifrovoy-ostsillograf-hantek-dso5102p-2-kanala-100-mgts-789554747/"
        wrap = lambda target: "https://duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&rut=qa"
        urls = [
            "https://www.ozon.ru/category/ostsillograf-tsifrovoy/",
            "https://www.vseinstrumenti.ru/category/ostsillografy-8222/",
            "https://supereyes.ru/catalog/oscilloscopes_digital/",
            "https://supereyes.ru/catalog/oscilloscopes_digital/hantek_dso2c10/",
            "https://supereyes.ru/catalog/oscilloscopes_digital/rigol_ds1202z_e/",
            "https://www.wildberries.ru/catalog/268671869/detail.aspx", product,
            "https://www.chipdip.ru/catalog-show/ostsillografy-tsifrovye-1005",
            wrap(ozon_product), "https://market.yandex.ru/search?text=qa100",
            "https://www.ozon.ru/category/ostsillografy-31854/",
            "https://www.vseinstrumenti.ru/category/tsifrovye-ostsillografy-164847/",
            wrap("https://www.ozon.ru/product/qa-second-model/"),
            "https://market.yandex.ru/search?text=qa200", ozon_product, wrap(product)]

        class TraceSources(CapturedSources):
            def open(self, url, browser=False):
                self.opened.append(url)
                if urlsplit(url).hostname != "supereyes.ru":
                    raise AcquisitionError("Robots.txt источника запрещает автоматическое чтение этой страницы.")
                doc = CapturedSources().open("https://shop-a.test/qa-100")
                doc["url"] = url
                if url.endswith("/oscilloscopes_digital/"):
                    doc.update(primary_title="Каталог осциллографов", title="Каталог осциллографов", is_listing=True)
                return doc

        sources = TraceSources()
        research = self.research([], sources, source_urls=urls, limits={"max_queries": 0})
        for url in urls:
            try:
                research.execute_tool("open_page", {"url": url})
            except AcquisitionError:
                pass
        self.assertEqual(research.pages, 9, "16 old attempts contain only 9 useful first attempts after transport dedupe/site guards")
        self.assertEqual(len(sources.opened), 9)
        self.assertFalse(any("duckduckgo.com" in url for url in sources.opened))
        self.assertEqual(len(research.offers), 3)
        for url in urls:
            if "ozon.ru" in url or "vseinstrumenti.ru" in url or "market.yandex.ru" in url:
                research.execute_tool("open_page", {"url": url, "browser": True})
        self.assertEqual(research.pages, 9, "browser=true cannot bypass a remembered source access failure")

    def test_search_html_navigation_cannot_feed_the_product_frontier(self):
        class SearchSources(CapturedSources):
            def search(self, query, limit=6):
                wrapped = "https://duckduckgo.com/l/?uddg=" + quote("https://unselected-qa.test/qa-999", safe="")
                return {"hits": self.hits[:1], "status": "ok", "document": {
                    "url": "https://lite.duckduckgo.com/lite/?q=qa", "method": "search",
                    "title": "QA search", "text": "QA search Осциллограф QA-999",
                    "links": [{"url": wrapped, "title": "Осциллограф QA-999"},
                              {"url": "https://duckduckgo.com/?q=next", "title": "Осциллограф QA-888"}]}}

        research = self.research([], SearchSources())
        research.execute_tool("search_web", {"query": "Осциллограф цена"})
        self.assertEqual(len(research.queued), 1)
        self.assertEqual(set(research.frontier_items()), {"shop-a.test"})
        output = research.execute_tool("open_page", {"url": "https://duckduckgo.com/?q=next"})
        self.assertIn("error", output)
        self.assertEqual(research.pages, 0)

    def test_diversifying_search_runs_before_draining_the_single_productive_site(self):
        class DiverseSources(CapturedSources):
            def __init__(self):
                super().__init__()
                self.queries = []
            def search(self, query, limit=6):
                self.queries.append(query)
                if len(self.queries) == 1:
                    hits = self.hits[:2] + [{"url": "https://blocked-one.test/catalog", "title": "Осциллографы"},
                                           {"url": "https://blocked-two.test/catalog", "title": "Осциллографы"}]
                else:
                    hits = self.hits[2:]
                return {"hits": hits, "status": "ok", "query": query}
            def open(self, url, browser=False):
                if "blocked-" in url:
                    self.opened.append(url)
                    raise AcquisitionError("Источник вернул HTTP 403.")
                return super().open(url, browser=browser)

        sources = DiverseSources()
        research = self.research([[call("search_web", query="Осциллограф купить цена")], [call("finish", reason="QA")]], sources)
        result = research.run()
        self.assertEqual(result["search"]["coverage"]["priced_offers"], 6)
        self.assertEqual(result["search"]["coverage"]["priced_domains"], 3)
        self.assertEqual(result["search"]["budget"]["pages_used"], 8)
        self.assertEqual(len(sources.queries), 2)
        self.assertIn("купить цена", sources.queries[1])
        self.assertIn("-site:shop-a.test", sources.queries[1])
        self.assertIn("-site:blocked-one.test", sources.queries[1])
        self.assertIn("-site:blocked-two.test", sources.queries[1])
        self.assertNotIn("100", sources.queries[1], "Missing bandwidth/channels must not be invented as discovery constraints")
        first_second_site = next(i for i, url in enumerate(sources.opened) if "shop-b.test" in url or "shop-c.test" in url)
        second_first_site = sources.opened.index("https://shop-a.test/qa-200")
        self.assertLess(first_second_site, second_first_site)

    def test_failed_url_cannot_retry_in_browser_but_one_404_does_not_ban_the_whole_site(self):
        class BrokenLink(CapturedSources):
            def open(self, url, browser=False):
                if url.endswith("/missing"):
                    self.opened.append(url)
                    raise AcquisitionError("Источник вернул HTTP 404.")
                return super().open(url, browser=browser)

        sources = BrokenLink()
        research = self.research([], sources, source_urls=["https://shop-a.test/missing", "https://shop-a.test/qa-100"])
        with self.assertRaises(AcquisitionError):
            research.execute_tool("open_page", {"url": "https://shop-a.test/missing"})
        self.assertIn("error", research.execute_tool("open_page", {"url": "https://shop-a.test/missing", "browser": True}))
        self.assertEqual(research.pages, 1)
        research.execute_tool("open_page", {"url": "https://shop-a.test/qa-100"})
        self.assertEqual(research.pages, 2)
        self.assertNotIn("shop-a.test", research.unavailable_sites)
        self.assertEqual(len(research.offers), 1)

    def test_finish_with_one_priced_site_and_empty_frontier_uses_remaining_query_budget(self):
        research = self.research([], source_urls=["https://shop-a.test/qa-100"])
        research.execute_tool("open_page", {"url": "https://shop-a.test/qa-100"})
        research.queries = 2
        self.assertFalse(research.frontier_items())
        output = research.execute_tool("finish", {"reason": "QA one site is enough"})
        self.assertFalse(output["finished"])
        self.assertTrue(research.finish_requested)
        self.assertFalse(research.finished)

    def test_recovery_without_queries_keeps_two_durable_offers_from_the_successful_site(self):
        sources = CapturedSources()
        research = self.research([], sources, source_urls=["https://shop-a.test/qa-100", "https://shop-a.test/qa-200"])
        research.execute_tool("open_page", {"url": "https://shop-a.test/qa-100"})
        self.assertTrue(research.should_diversify())
        with patch.object(sources, "search", side_effect=AssertionError("Recovery must never start a query")) as search:
            self.assertEqual(research.drain_frontier(allow_queries=False), 1)
            search.assert_not_called()
        self.assertEqual(research.pages, 2)
        self.assertEqual(len(research.offers), 2)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 2)
        self.assertEqual(research.queries, 0)
        self.assertEqual(research.calls, 0)
        self.assertEqual(research.provider.messages, [])
        self.assertEqual(self.store.quota_usage("model_calls", period="all"), 0)

    def test_new_browser_url_obeys_diversity_gate_but_previous_http_url_can_render(self):
        sources = CapturedSources()
        research = self.research([], sources, source_urls=["https://shop-a.test/qa-100", "https://shop-a.test/qa-200"])
        research.execute_tool("open_page", {"url": "https://shop-a.test/qa-100"})
        self.assertTrue(research.should_diversify())
        blocked = research.execute_tool("open_page", {"url": "https://shop-a.test/qa-200", "browser": True})
        self.assertIn("error", blocked)
        self.assertEqual(research.pages, 1)
        self.assertEqual(len(sources.opened), 1)
        rendered = research.execute_tool("open_page", {"url": "https://shop-a.test/qa-100", "browser": True})
        self.assertNotIn("error", rendered)
        self.assertEqual(research.pages, 2)
        self.assertEqual(len(research.offers), 1)
        self.assertEqual(research.queries, 0)
        self.assertEqual(research.calls, 0)

    def test_known_urls_are_freshly_captured_and_can_finish_without_a_model_call(self):
        sources = CapturedSources()
        rows = [{**hit, "title": "Осциллограф OLD-999", "source_job_id": "old-live-job",
                 "price": 1, "quantity": 999, "attributes": {"channels": 999, "stale_only": "must not copy"}}
                for hit in sources.hits]
        research = self.research([], sources, quantity=3)
        with patch.object(self.store, "discovery_urls", return_value=rows, create=True) as discovery:
            with patch.object(sources, "search", side_effect=AssertionError("No query during fresh URL refresh")) as search:
                result = research.run()
                search.assert_not_called()
        discovery.assert_called_once_with(research.request, limit=9)
        self.assertEqual(result["search"]["stop"]["code"], "discovery_refreshed")
        self.assertEqual(len(result["offers"]), 6)
        self.assertEqual(result["search"]["coverage"]["priced_domains"], 3)
        self.assertEqual(result["search"]["reused_discovery_urls"], 6)
        self.assertEqual(result["search"]["prices_refetched"], 6)
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)
        self.assertEqual(result["search"]["budget"]["queries_used"], 0)
        self.assertEqual(research.provider.messages, [])
        self.assertEqual(result["request"]["quantity"], 3)
        self.assertEqual({offer["price"] for offer in result["offers"]}, {10100, 10200})
        for offer in result["offers"]:
            self.assertNotIn("OLD", offer["title"])
            self.assertNotEqual(offer["attributes"].get("channels"), 999)
            self.assertEqual(offer["attributes"]["bandwidth_mhz"], 100)
            self.assertNotIn("stale_only", offer["attributes"])
            document = self.store.get_document("qa", offer["evidence"]["price"]["document_id"])
            self.assertIsNotNone(document)
            self.assertIn(offer["evidence"]["price"]["quote"], document["text"])
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 6)

    def test_known_url_refresh_obeys_current_scope_and_page_cap(self):
        sources = CapturedSources()
        research = self.research([], sources, source_scope={"mode": "domains", "domains": ["shop-b.test"]},
                                 limits={"max_pages": 1})
        research.provider.configured = False
        with patch.object(self.store, "discovery_urls", return_value=[{**hit, "source_job_id": "old"} for hit in sources.hits], create=True) as discovery:
            result = research.run()
        discovery.assert_called_once_with(research.request, limit=1)
        self.assertEqual(sources.opened, ["https://shop-b.test/qa-100"])
        self.assertEqual(result["search"]["budget"]["pages_used"], 1)
        self.assertEqual(result["search"]["reused_discovery_urls"], 1)
        self.assertEqual(result["search"]["prices_refetched"], 1)
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)

    def test_cancelled_known_url_refresh_never_reads_history_or_a_source(self):
        sources = CapturedSources()
        research = self.research([], sources)
        self.cancel.set()
        with patch.object(self.store, "discovery_urls", return_value=sources.hits, create=True) as discovery:
            result = research.run()
            discovery.assert_not_called()
        self.assertEqual(result["search"]["stop"]["code"], "cancelled")
        self.assertEqual(sources.opened, [])
        self.assertEqual(result["offers"], [])

    def test_cancel_during_known_url_read_keeps_only_that_fresh_offer(self):
        sources = CapturedSources(lambda url: self.cancel.set())
        research = self.research([], sources)
        with patch.object(self.store, "discovery_urls", return_value=sources.hits, create=True):
            result = research.run()
        self.assertEqual(result["search"]["stop"]["code"], "cancelled")
        self.assertEqual(len(sources.opened), 1)
        self.assertEqual(result["search"]["prices_refetched"], 1)
        self.assertEqual(len(self.store.get_job("qa")["result"]["offers"]), 1)
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()

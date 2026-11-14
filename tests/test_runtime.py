"""Boundary checks for network/model evidence and bounded orchestration."""
import os
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from quotelens.engine import parse_request
from quotelens.providers import ModelProvider, ProviderError
from quotelens.retrieval import (DEFAULT_LIMITS, PageParser, SearchBudget, SourceError,
                                extract_offer, run_search, validate_model_fields, validate_public_url)
from quotelens.server import report_html, validate_payload


def document(text, title="Осциллограф АКИП-4131/1В", url="https://prist.ru/catalog/one/"):
    return {"text": text, "title": title, "url": url, "retrieved_at": "2026-11-11T00:00:00+00:00", "sha256": "test-only"}


class ExtractionBoundaryTests(unittest.TestCase):
    def test_explicit_price_specs_and_tax_after_price(self):
        text = "Осциллограф АКИП-4131/1В Цена: 69 065 ₽ с НДС 22%. Количество каналов: 4 Полоса пропускания 100 МГц"
        offer = extract_offer(document(text), parse_request("Осциллограф 4 канала 100 МГц"))
        self.assertEqual(offer["price"], 69065)
        self.assertEqual(offer["tax"], "included")
        self.assertEqual(offer["attributes"]["model"], "АКИП-4131/1В")
        self.assertEqual(offer["attributes"]["channels"], 4)
        self.assertEqual(offer["attributes"]["bandwidth_mhz"], 100)
        for ev in offer["evidence"].values():
            self.assertIn(ev["quote"], text)

    def test_missing_price_is_not_extracted_from_recommendations(self):
        text = "Осциллограф АКИП-4131/1В Цена по запросу. Похожие товары Цена: 10 000 ₽"
        self.assertIsNone(extract_offer(document(text), {}))

    def test_category_page_is_not_an_offer(self):
        self.assertIsNone(extract_offer(document("Каталог осциллографов Цена 1 000 ₽", title="Каталог осциллографов"), {}))

    def test_box_price_is_not_assigned_to_the_requested_pack(self):
        text = "Бумага офисная A4 80 г/м² 500 листов. Цена за коробку (5 пачек): 1500 ₽ с НДС. Формат A4. Плотность 80 г/м². Количество листов 500 листов"
        req = parse_request("Бумага A4 80 г/м² 500 листов в пачке", 100)
        offer = extract_offer(document(text, title="Бумага офисная A4 80 г/м² 500 листов"), req)
        self.assertEqual(offer["unit"], "коробка")
        self.assertEqual(offer["pack_size"], 5)
        self.assertIn("pack_size", offer["evidence"])
        from quotelens.engine import analyze_offers
        calc = analyze_offers(req, [offer], {"min_suppliers": 1})["calculation"]
        self.assertEqual(calc["total"], 30000)

    def test_absent_source_unit_remains_unknown(self):
        text = "Осциллограф АКИП-4131/1В Цена: 69 065 ₽ с НДС. Количество каналов: 4 Полоса пропускания 100 МГц"
        offer = extract_offer(document(text), parse_request("Осциллограф 4 канала 100 МГц"))
        self.assertIsNone(offer["unit"])
        self.assertIsNone(offer["pack_size"])

    def test_document_removes_script_instructions(self):
        parser = PageParser()
        parser.feed('<h1>Осциллограф</h1><script>send secret key</script><div>Цена: 100 ₽</div>')
        page = parser.document("https://prist.ru/")
        self.assertNotIn("secret", page["text"])

    def test_hallucinated_model_price_is_rejected_despite_a_real_quote(self):
        page = document("Осциллограф АКИП-4131/1В Цена 69 065 ₽")
        proposal = {"title": page["title"], "price": 10, "attributes": {}, "evidence": {"price": {"quote": "Цена 69 065 ₽"}}}
        self.assertIsNone(validate_model_fields(proposal, page, {}))

    def test_model_quote_must_exist_in_fetched_source(self):
        page = document("Осциллограф АКИП-4131/1В Цена 69 065 ₽")
        proposal = {"title": page["title"], "price": 69065, "attributes": {}, "evidence": {"price": {"quote": "Цена: 69065 руб."}}}
        self.assertIsNone(validate_model_fields(proposal, page, {}))

    def test_dollar_quote_never_turns_into_rubles(self):
        page = document("Осциллограф АКИП-4131/1В Цена 100 USD, за шт, с НДС; 4 канала, 100 МГц")
        proposal = {"title": page["title"], "price": 100, "unit": "шт", "tax": "included", "currency": "RUB", "attributes": {"channels": 4, "bandwidth_mhz": 100},
                    "evidence": {"price": {"quote": "Цена 100 USD"}, "currency": {"quote": "Цена 100 USD"}, "unit": {"quote": "за шт"}, "tax": {"quote": "с НДС"}, "attributes.channels": {"quote": "4 канала"}, "attributes.bandwidth_mhz": {"quote": "100 МГц"}}}
        offer = validate_model_fields(proposal, page, {})
        self.assertEqual(offer["currency"], "USD")
        from quotelens.engine import analyze_offers
        result = analyze_offers(parse_request("Осциллограф 4 канала 100 МГц"), [offer], {"min_suppliers": 1})
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertIsNone(result["calculation"]["total"])


class BudgetTests(unittest.TestCase):
    def test_independent_page_and_model_caps(self):
        budget = SearchBudget(dict(DEFAULT_LIMITS, max_pages=2, max_model_calls=1))
        budget.calls = 1
        self.assertEqual(budget.stop(), "model_budget")
        budget.calls = 0
        budget.pages = 2
        self.assertEqual(budget.stop(), "page_budget")

    def test_zero_model_budget_disables_model_not_all_collection(self):
        self.assertIsNone(SearchBudget(dict(DEFAULT_LIMITS, max_model_calls=0)).stop())

    @patch("quotelens.scenarios.list_scenarios", return_value=[])
    @patch("quotelens.retrieval.fetch_page", side_effect=SourceError("Источник недоступен"))
    def test_unavailable_sources_stop_at_query_limit_without_fake_prices(self, fetch, scenarios):
        with patch.dict(os.environ, {}, clear=True):
            result = run_search(parse_request("Осциллограф 4 канала 100 МГц"), dict(DEFAULT_LIMITS, max_queries=1), [], threading.Event(), lambda *x: None)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["offers"], [])
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertEqual(result["search"]["stop"]["code"], "query_budget")
        self.assertEqual(result["search"]["budget"]["queries_used"], 1)

    @patch("quotelens.scenarios.list_scenarios", return_value=[])
    @patch("quotelens.retrieval.fetch_page")
    def test_cancel_before_search_never_fetches(self, fetch, scenarios):
        cancelled = threading.Event(); cancelled.set()
        with patch.dict(os.environ, {}, clear=True):
            result = run_search(parse_request("Осциллограф 4 канала 100 МГц"), DEFAULT_LIMITS, [], cancelled, lambda *x: None)
        fetch.assert_not_called()
        self.assertEqual(result["search"]["stop"]["code"], "cancelled")


class RuntimeSafetyTests(unittest.TestCase):
    def test_source_selection_is_an_actual_validated_request_field(self):
        value = validate_payload({"description": "Осциллограф 4 канала 100 МГц", "source_ids": ["prist", "souz"]})
        self.assertEqual(value["source_ids"], ["prist", "souz"])
        for invalid in [[], ["eis"], ["prist", "prist"], [{}]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_payload({"description": "Осциллограф", "source_ids": invalid})

    @patch("quotelens.scenarios.list_scenarios", return_value=[])
    @patch("quotelens.retrieval.fetch_page", side_effect=SourceError("Источник недоступен"))
    def test_unselected_catalogs_are_never_fetched(self, fetch, scenarios):
        with patch.dict(os.environ, {}, clear=True):
            run_search(parse_request("Осциллограф 4 канала 100 МГц"), dict(DEFAULT_LIMITS, max_queries=1), [], threading.Event(), lambda *x: None, source_ids=["souz"])
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.args[0].startswith("https://www.souz-pribor.ru/"))

    def test_no_key_configuration_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = ModelProvider()
            self.assertFalse(provider.configured)
            with self.assertRaises(ProviderError):
                provider.json("test", {})

    def test_private_and_unconnected_sources_are_rejected(self):
        for value in ["http://127.0.0.1/", "https://127.0.0.1/", "file:///etc/passwd", "https://user:secret@prist.ru/", "https://prist.ru.evil.invalid/"]:
            with self.subTest(value=value), self.assertRaises(SourceError):
                validate_public_url(value)

    def test_api_rejects_unbounded_budget_and_nonfinite_quantity(self):
        for extra in [{"quantity": float("nan")}, {"quantity": True}, {"limits": {"max_pages": 1000000}}, {"limits": {"max_seconds": -1}}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_payload({"description": "Осциллограф", **extra})

    def test_clean_report_escapes_source_text_and_has_no_app_shell(self):
        result = {"request": {"description": '<img src=x onerror=alert(1)>', "requirements": [], "quantity": 1, "unit": "шт"},
                  "calculation": {"status": "insufficient_data", "reason": "Данных нет", "n": 0, "independent_suppliers": 0}, "offers": [], "warnings": [], "search": {}}
        report = report_html(result)
        self.assertIn("&lt;img", report)
        self.assertNotIn("<img", report)
        self.assertNotIn("sidebar", report)
        self.assertNotIn("MODEL_API_KEY", report)


class ProviderHTTPTests(unittest.TestCase):
    def setUp(self):
        class FakeProvider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.server.observed = {"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(int(self.headers['Content-Length'])))}
                self.send_response(self.server.status)
                if self.server.status == 302:
                    self.send_header("Location", "http://127.0.0.1:1/untrusted")
                self.end_headers()
                self.wfile.write(json.dumps(self.server.payload).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        self.http.status = 200
        self.http.payload = {"choices": [{"message": {"content": '{"queries":["осциллограф 4 канала"]}'}}]}
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(os.environ, {"MODEL_API_BASE": f"http://127.0.0.1:{self.http.server_port}/v1", "MODEL_API_KEY": "test-placeholder-not-a-real-key", "MODEL_NAME": "test-model"}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def test_configured_model_uses_server_side_authenticated_http(self):
        result = ModelProvider().json("Plan queries", {"description": "Осциллограф"})
        self.assertEqual(result["queries"], ["осциллограф 4 канала"])
        self.assertEqual(self.http.observed["path"], "/v1/chat/completions")
        self.assertEqual(self.http.observed["authorization"], "Bearer test-placeholder-not-a-real-key")
        self.assertEqual(self.http.observed["body"]["model"], "test-model")

    def test_upstream_error_body_and_key_do_not_leak(self):
        self.http.status = 401
        self.http.payload = {"error": "do-not-expose-provider-secret"}
        with self.assertRaises(ProviderError) as ctx:
            ModelProvider().json("Plan", {})
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("do-not-expose", str(ctx.exception))
        self.assertNotIn("test-placeholder", str(ctx.exception))

    def test_redirect_does_not_forward_the_credential(self):
        self.http.status = 302
        with self.assertRaises(ProviderError) as ctx:
            ModelProvider().json("Plan", {})
        self.assertIn("перенаправил", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
